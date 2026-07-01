"""
Offline tests for Phase 3: indicators (S/R, OI build-up, OI flow), the signal
engine (bullish / bearish / no-signal paths, scoring bounds, R-multiple trade
frame), and signal persistence + as-of chain reconstruction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analytics.indicators import (
    BuildupType,
    build_chain_dataframe,
    classify_buildup,
    oi_change_breakdown,
    support_resistance,
)
from database.sqlite_db import SQLiteDatabase
from signals.engine import SignalConfig, SignalEngine
from signals.models import Direction, Signal, SignalKind


def _row(strike: float, opt: str, oi: int, ltp: float = 1.0, vol: int = 0) -> dict:
    return {"strike": strike, "option_type": opt, "oi": oi, "oi_change": 0,
            "volume": vol, "ltp": ltp, "ts": "2025-01-30T05:00:00+00:00"}


def _chain(spec: list[tuple[float, int, int]]):
    """spec: list of (strike, ce_oi, pe_oi)."""
    rows = []
    for strike, ce, pe in spec:
        rows.append(_row(strike, "CE", ce))
        rows.append(_row(strike, "PE", pe))
    return build_chain_dataframe(rows)


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def test_support_resistance_walls() -> None:
    chain = _chain([(24000, 1000, 9000), (24400, 1000, 1000), (24800, 7000, 1000)])
    sr = support_resistance(chain)
    assert sr.support == 24000      # biggest put OI
    assert sr.resistance == 24800   # biggest call OI


@pytest.mark.parametrize("dp,doi,expected", [
    (1.0, 1.0, BuildupType.LONG_BUILDUP),
    (-1.0, 1.0, BuildupType.SHORT_BUILDUP),
    (1.0, -1.0, BuildupType.SHORT_COVERING),
    (-1.0, -1.0, BuildupType.LONG_UNWINDING),
    (0.0, 0.0, BuildupType.NEUTRAL),
])
def test_classify_buildup(dp, doi, expected) -> None:
    assert classify_buildup(dp, doi) is expected


def test_oi_change_breakdown_bias() -> None:
    prev = _chain([(24000, 1000, 1000), (24100, 1000, 1000)])
    curr = _chain([(24000, 1000, 3000), (24100, 1000, 2000)])  # PE OI grew, CE flat
    flow = oi_change_breakdown(curr, prev)
    assert flow.pe_oi_change == 3000
    assert flow.ce_oi_change == 0
    assert flow.bias == pytest.approx(1.0)  # entirely put-side growth -> bullish lean


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
def test_engine_bullish_scenario() -> None:
    # Heavy put OI (PCR high), strong put-support just below spot.
    spec = [(k, 1000, 1500) for k in range(24000, 24900, 100)]
    spec[0] = (24000, 1000, 8000)     # support wall at 24000
    spec[-1] = (24800, 6000, 1000)    # resistance wall at 24800
    chain = build_chain_dataframe(
        [r for (s, ce, pe) in spec for r in (_row(s, "CE", ce), _row(s, "PE", pe))]
    )
    signals = SignalEngine().evaluate("NIFTY", spot=24050, current=chain)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction is Direction.BULLISH
    assert sig.confidence >= 45
    # Trade frame: long, stop below entry, ascending targets at 1R/2R/3R spacing.
    assert sig.stop_loss < sig.entry < sig.target1 < sig.target2 < sig.target3
    d1 = sig.target1 - sig.entry
    assert d1 > 0
    assert sig.target2 - sig.entry == pytest.approx(2 * d1, rel=0.02)
    assert sig.target3 - sig.entry == pytest.approx(3 * d1, rel=0.02)


def test_engine_bearish_scenario() -> None:
    # Heavy call OI overall (PCR low), a call-OI resistance wall just above spot,
    # put-OI support far below. Spot 24080 sits right under the 24100 wall.
    spec = [(k, 2000, 800) for k in range(23000, 24600, 100)]   # CE >> PE -> low PCR
    spec = [(24100, 8000, 800) if k == 24100 else (k, ce, pe)
            for (k, ce, pe) in spec]                            # resistance wall
    chain = build_chain_dataframe(
        [r for (s, ce, pe) in spec for r in (_row(s, "CE", ce), _row(s, "PE", pe))]
    )
    signals = SignalEngine().evaluate("NIFTY", spot=24080, current=chain)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction is Direction.BEARISH
    assert sig.stop_loss > sig.entry > sig.target1 > sig.target2 > sig.target3


def test_engine_no_signal_when_balanced() -> None:
    # Flat, symmetric OI, spot mid-range and away from walls -> nothing fires.
    chain = _chain([(k, 1000, 1000) for k in range(24000, 24900, 100)])
    signals = SignalEngine().evaluate("NIFTY", spot=24400, current=chain)
    assert signals == []


def test_engine_scores_within_bounds() -> None:
    spec = [(k, 1000, 1500) for k in range(24000, 24900, 100)]
    spec[0] = (24000, 1000, 8000)     # support wall below spot
    spec[-1] = (24800, 6000, 1000)    # distinct resistance far above
    chain = build_chain_dataframe(
        [r for (s, ce, pe) in spec for r in (_row(s, "CE", ce), _row(s, "PE", pe))]
    )
    sig = SignalEngine().evaluate("NIFTY", spot=24050, current=chain)[0]
    for score in (sig.confidence, sig.risk, sig.probability):
        assert 0 <= score <= 100
    assert sig.kind in set(SignalKind)
    assert sig.supporting_indicators  # non-empty rationale


def test_engine_respects_min_confidence_config() -> None:
    spec = [(k, 1000, 1500) for k in range(24000, 24900, 100)]
    spec[0] = (24000, 1000, 8000)
    chain = build_chain_dataframe(
        [r for (s, ce, pe) in spec for r in (_row(s, "CE", ce), _row(s, "PE", pe))]
    )
    strict = SignalEngine(SignalConfig(min_confidence=99))
    assert strict.evaluate("NIFTY", spot=24050, current=chain) == []


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_signal_persistence_roundtrip(tmp_path: Path) -> None:
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema()
    sig = Signal(
        index_name="NIFTY", direction=Direction.BULLISH, kind=SignalKind.REVERSAL,
        spot=24050, confidence=72, risk=30, probability=68,
        entry=24050, stop_loss=24000, target1=24100, target2=24150, target3=24200,
        reason="test", supporting_indicators=["PCR", "Support"],
    )
    new_id = db.insert_signal(sig)
    assert new_id > 0
    recent = db.fetch_recent_signals("NIFTY", limit=5)
    assert len(recent) == 1
    assert recent[0]["direction"] == "bullish"
    assert recent[0]["confidence"] == 72
    db.close()


def test_fetch_option_chain_asof(tmp_path: Path) -> None:
    from database.models import OptionTick, OptionType
    db = SQLiteDatabase(str(tmp_path / "t.db"))
    db.connect(); db.init_schema()
    early = OptionTick(token="111", name="NIFTY", strike=24500, option_type=OptionType.CALL,
                       expiry="2025-01-30", ltp=100.0, oi=10000)
    early.ts = early.ts.replace(year=2025, month=1, day=1)
    db.insert_option_ticks([early])
    late = OptionTick(token="111", name="NIFTY", strike=24500, option_type=OptionType.CALL,
                      expiry="2025-01-30", ltp=130.0, oi=15000)
    db.insert_option_ticks([late])
    asof = db.fetch_option_chain_asof("NIFTY", "2025-06-01T00:00:00+00:00")
    # Only the early row is <= mid-cutoff... actually both are; latest <= cutoff wins.
    assert len(asof) == 1
    db.close()
