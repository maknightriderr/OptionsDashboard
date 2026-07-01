"""
Offline unit tests for the pieces that do not require the broker network:
crypto round-trip, SQLite persistence, scrip-master parsing / strike selection,
and websocket-packet normalisation.

Run from the project root:  pytest -q
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from collectors.instruments import InstrumentRepository, _parse_expiry
from collectors.market_data import normalise_option_tick, normalise_spot_tick
from database.models import Instrument, OptionTick, OptionType
from database.sqlite_db import SQLiteDatabase
from utils import crypto


# --------------------------------------------------------------------------- #
# crypto
# --------------------------------------------------------------------------- #
def test_crypto_roundtrip() -> None:
    key = crypto.generate_key()
    secret = "1234-PIN-secret"
    assert crypto.decrypt(crypto.encrypt(secret, key), key) == secret


def test_crypto_wrong_key_raises() -> None:
    token = crypto.encrypt("x", crypto.generate_key())
    with pytest.raises(ValueError):
        crypto.decrypt(token, crypto.generate_key())


# --------------------------------------------------------------------------- #
# database
# --------------------------------------------------------------------------- #
def test_sqlite_insert_and_fetch(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect()
    db.init_schema()
    ticks = [
        OptionTick(
            token="111", name="NIFTY", strike=24500, option_type=OptionType.CALL,
            expiry="2025-01-30", ltp=120.5, volume=1000, oi=50000, bid=120.0, ask=121.0,
        ),
        OptionTick(
            token="112", name="NIFTY", strike=24500, option_type=OptionType.PUT,
            expiry="2025-01-30", ltp=98.0, volume=800, oi=42000,
        ),
    ]
    assert db.insert_option_ticks(ticks) == 2
    rows = db.fetch_recent_option_ticks("NIFTY", limit=10)
    assert len(rows) == 2
    assert {r["token"] for r in rows} == {"111", "112"}
    db.close()


def test_sqlite_wal_enabled(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect()
    mode = db._require_conn().execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


# --------------------------------------------------------------------------- #
# instruments
# --------------------------------------------------------------------------- #
@pytest.fixture()
def scrip_master_file(tmp_path: Path) -> str:
    """A minimal synthetic scrip master: NIFTY spot + a strike ladder, 2 expiries."""
    rows: list[dict] = [
        {"token": "26000", "symbol": "NIFTY", "name": "NIFTY",
         "expiry": "", "strike": "-1.000000", "lotsize": "1",
         "instrumenttype": "", "exch_seg": "NSE"},
    ]
    # Two expiries; near = 30JAN2025, far = 27FEB2025. Strikes 24000..25000 step 100.
    for expiry in ("30JAN2025", "27FEB2025"):
        for strike in range(24000, 25001, 100):
            for opt in ("CE", "PE"):
                rows.append({
                    "token": f"{strike}{opt}{expiry[:5]}",
                    "symbol": f"NIFTY{expiry[:5]}{strike}{opt}",
                    "name": "NIFTY",
                    "expiry": expiry,
                    "strike": f"{strike * 100}.000000",
                    "lotsize": "75",
                    "instrumenttype": "OPTIDX",
                    "exch_seg": "NFO",
                })
    path = tmp_path / "scrip.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def test_parse_expiry() -> None:
    assert _parse_expiry("30JAN2025") == date(2025, 1, 30)


def test_resolve_spot_token(scrip_master_file: str) -> None:
    repo = InstrumentRepository.from_file(scrip_master_file)
    assert repo.resolve_spot_token("NIFTY") == "26000"


def test_nearest_expiry(scrip_master_file: str) -> None:
    repo = InstrumentRepository.from_file(scrip_master_file)
    # As of a date before both expiries, nearest is 30JAN2025.
    assert repo.nearest_expiry("NIFTY", on=date(2025, 1, 1)) == date(2025, 1, 30)
    # After Jan expiry, nearest rolls to Feb.
    assert repo.nearest_expiry("NIFTY", on=date(2025, 2, 1)) == date(2025, 2, 27)


def test_select_strikes_around_atm(scrip_master_file: str) -> None:
    repo = InstrumentRepository.from_file(scrip_master_file)
    # Spot 24510 -> ATM rounds to 24500; +/- 2 strikes (step 100) = 24300..24700.
    instruments = repo.select_option_instruments(
        "NIFTY", spot=24510, strikes_around_atm=2, expiry=date(2025, 1, 30)
    )
    strikes = sorted({i.strike for i in instruments})
    assert strikes == [24300, 24400, 24500, 24600, 24700]
    # Both CE and PE present for each strike -> 5 strikes * 2 = 10 contracts.
    assert len(instruments) == 10
    assert all(isinstance(i, Instrument) for i in instruments)


# --------------------------------------------------------------------------- #
# tick normalisation
# --------------------------------------------------------------------------- #
def test_normalise_spot_tick_paise_to_rupees() -> None:
    raw = {"token": "26000", "last_traded_price": 2451075}  # paise
    tick = normalise_spot_tick(raw, "NIFTY")
    assert tick.ltp == pytest.approx(24510.75)
    assert tick.name == "NIFTY"


def test_normalise_option_tick_full_packet() -> None:
    instrument = Instrument(
        token="111", symbol="NIFTY30JAN2524500CE", name="NIFTY",
        strike=24500, option_type=OptionType.CALL, expiry="2025-01-30", lot_size=75,
    )
    raw = {
        "token": "111",
        "last_traded_price": 12050,            # 120.50
        "volume_trade_for_the_day": 1500,
        "open_interest": 60000,
        "best_5_buy_data": [{"price": 12000, "quantity": 75}],
        "best_5_sell_data": [{"price": 12100, "quantity": 150}],
    }
    tick = normalise_option_tick(raw, instrument)
    assert tick.ltp == pytest.approx(120.50)
    assert tick.oi == 60000
    assert tick.bid == pytest.approx(120.00)
    assert tick.ask == pytest.approx(121.00)
    assert tick.option_type is OptionType.CALL
