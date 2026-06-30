"""
Offline tests for Phase 2: option-chain shaping (pivot, PCR, max pain, ATM)
and the new read-side database queries (latest-per-contract, latest spot,
available indices, read-only enforcement).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dashboard.data import (
    build_chain_dataframe,
    compute_max_pain,
    compute_pcr,
    find_atm_strike,
    summarise_chain,
)
from database.models import OptionTick, OptionType, SpotTick
from database.sqlite_db import SQLiteDatabase


def _row(strike: float, opt: str, oi: int, vol: int, ltp: float, oi_chg: int = 0) -> dict:
    return {
        "strike": strike, "option_type": opt, "oi": oi, "oi_change": oi_chg,
        "volume": vol, "ltp": ltp, "ts": "2025-01-30T05:00:00+00:00",
    }


# --------------------------------------------------------------------------- #
# chain shaping
# --------------------------------------------------------------------------- #
def test_build_chain_pivots_ce_and_pe() -> None:
    rows = [
        _row(24500, "CE", oi=100, vol=10, ltp=120.0),
        _row(24500, "PE", oi=200, vol=20, ltp=98.0),
        _row(24600, "CE", oi=50, vol=5, ltp=80.0),
        _row(24600, "PE", oi=400, vol=40, ltp=140.0),
    ]
    chain = build_chain_dataframe(rows)
    assert list(chain["STRIKE"]) == [24500, 24600]
    r0 = chain.iloc[0]
    assert r0["CE_OI"] == 100 and r0["CE_LTP"] == 120.0
    assert r0["PE_OI"] == 200 and r0["PE_LTP"] == 98.0


def test_build_chain_empty() -> None:
    chain = build_chain_dataframe([])
    assert chain.empty
    assert "STRIKE" in chain.columns  # stable shell for the UI


def test_compute_pcr() -> None:
    chain = build_chain_dataframe([
        _row(100, "CE", oi=200, vol=0, ltp=1),
        _row(100, "PE", oi=300, vol=0, ltp=1),
    ])
    assert compute_pcr(chain) == pytest.approx(1.5)


def test_find_atm_strike() -> None:
    chain = build_chain_dataframe([
        _row(24400, "CE", 1, 0, 1), _row(24500, "CE", 1, 0, 1), _row(24600, "CE", 1, 0, 1),
    ])
    assert find_atm_strike(24470, chain) == 24500


def test_compute_max_pain_known_case() -> None:
    # Constructed so the analytic max-pain strike is 110 (see project tests doc).
    rows = [
        _row(100, "CE", oi=0, vol=0, ltp=1),   _row(100, "PE", oi=300, vol=0, ltp=1),
        _row(110, "CE", oi=100, vol=0, ltp=1), _row(110, "PE", oi=100, vol=0, ltp=1),
        _row(120, "CE", oi=300, vol=0, ltp=1), _row(120, "PE", oi=0, vol=0, ltp=1),
    ]
    chain = build_chain_dataframe(rows)
    assert compute_max_pain(chain) == 110


def test_summarise_chain_bundles_metrics() -> None:
    chain = build_chain_dataframe([
        _row(100, "CE", oi=200, vol=0, ltp=1),
        _row(100, "PE", oi=300, vol=0, ltp=1),
    ])
    summary = summarise_chain(chain, spot=101.0, last_update="2025-01-30T05:00:00+00:00")
    assert summary.total_ce_oi == 200
    assert summary.total_pe_oi == 300
    assert summary.pcr == pytest.approx(1.5)
    assert summary.atm_strike == 100
    assert summary.contracts == 2


# --------------------------------------------------------------------------- #
# read-side database queries
# --------------------------------------------------------------------------- #
def _seed(db: SQLiteDatabase) -> None:
    # Two ticks for the same CE contract; the later one must win.
    db.insert_option_ticks([
        OptionTick(token="111", name="NIFTY", strike=24500, option_type=OptionType.CALL,
                   expiry="2025-01-30", ltp=100.0, oi=10000),
    ])
    db.insert_option_ticks([
        OptionTick(token="111", name="NIFTY", strike=24500, option_type=OptionType.CALL,
                   expiry="2025-01-30", ltp=125.0, oi=15000),
        OptionTick(token="112", name="NIFTY", strike=24500, option_type=OptionType.PUT,
                   expiry="2025-01-30", ltp=90.0, oi=12000),
    ])
    db.insert_spot(SpotTick(name="NIFTY", token="26000", ltp=24510.0))


def test_fetch_latest_option_chain_picks_newest(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema(); _seed(db)
    chain = db.fetch_latest_option_chain("NIFTY")
    by_token = {r["token"]: r for r in chain}
    assert len(chain) == 2
    assert by_token["111"]["ltp"] == 125.0   # newest CE tick, not 100.0
    assert by_token["111"]["oi"] == 15000
    db.close()


def test_fetch_latest_spot_and_indices(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema(); _seed(db)
    spot = db.fetch_latest_spot("NIFTY")
    assert spot is not None and spot["ltp"] == 24510.0
    assert list(db.fetch_available_indices()) == ["NIFTY"]
    assert db.fetch_latest_spot("BANKNIFTY") is None
    db.close()


def test_read_only_mode_blocks_writes(tmp_path: Path) -> None:
    path = str(tmp_path / "t.db")
    writer = SQLiteDatabase(path)
    writer.connect(); writer.init_schema(); _seed(writer); writer.close()

    reader = SQLiteDatabase(path, read_only=True)
    reader.connect()
    # Reads work...
    assert list(reader.fetch_available_indices()) == ["NIFTY"]
    # ...writes are rejected by PRAGMA query_only.
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        reader.insert_spot(SpotTick(name="X", token="1", ltp=1.0))
    reader.close()
