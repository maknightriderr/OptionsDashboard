"""
Offline tests for Phase 6: the trade simulator (stop/target/eod, long & short,
MFE/MAE), performance metrics (win rate, expectancy, profit factor, drawdown),
the backtest engine on a synthetic data source, and the insights generator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analytics.indicators import build_chain_dataframe
from signals.engine import SignalEngine
from signals.models import Direction, Signal, SignalKind

from backtest.engine import Backtester
from backtest.insights import generate_insights
from backtest.metrics import compute_metrics, equity_curve, max_drawdown_r
from backtest.models import BacktestConfig, BacktestReport, ExitReason, TradeResult
from backtest.simulator import simulate_trade


def _sig(direction=Direction.BULLISH, entry=100.0, stop=90.0,
         t1=110.0, t2=120.0, t3=130.0, kind=SignalKind.REVERSAL) -> Signal:
    return Signal(
        index_name="NIFTY", direction=direction, kind=kind, spot=entry,
        confidence=70, risk=30, probability=65,
        entry=entry, stop_loss=stop, target1=t1, target2=t2, target3=t3,
        reason="t", supporting_indicators=["PCR"],
    )


def _path(prices: list[float]) -> list[dict]:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [{"ts": (base + timedelta(minutes=i)).isoformat(), "ltp": p}
            for i, p in enumerate(prices)]


# --------------------------------------------------------------------------- #
# simulator
# --------------------------------------------------------------------------- #
def test_long_hits_target() -> None:
    r = simulate_trade(_sig(), target=110.0, price_path=_path([102, 106, 110.5]))
    assert r.exit_reason is ExitReason.TARGET
    assert r.realized_r == pytest.approx(1.0)   # (110-100)/(100-90)


def test_long_hits_stop() -> None:
    r = simulate_trade(_sig(), target=110.0, price_path=_path([98, 95, 89]))
    assert r.exit_reason is ExitReason.STOP
    assert r.realized_r == pytest.approx(-1.0)


def test_short_hits_target() -> None:
    s = _sig(direction=Direction.BEARISH, entry=100, stop=110, t1=90, t2=80, t3=70)
    r = simulate_trade(s, target=90.0, price_path=_path([98, 94, 89]))
    assert r.exit_reason is ExitReason.TARGET
    assert r.realized_r == pytest.approx(1.0)   # (100-90)/(110-100)


def test_short_hits_stop() -> None:
    s = _sig(direction=Direction.BEARISH, entry=100, stop=110, t1=90, t2=80, t3=70)
    r = simulate_trade(s, target=90.0, price_path=_path([103, 108, 111]))
    assert r.exit_reason is ExitReason.STOP
    assert r.realized_r == pytest.approx(-1.0)


def test_eod_exit_and_excursions() -> None:
    r = simulate_trade(_sig(), target=110.0, price_path=_path([105, 108, 104]))
    assert r.exit_reason is ExitReason.EOD
    assert r.realized_r == pytest.approx(0.4)    # (104-100)/10
    assert r.mfe_r == pytest.approx(0.8)         # best was 108 -> +0.8R
    assert r.mae_r == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def _trade(r: float, kind=SignalKind.REVERSAL, direction=Direction.BULLISH) -> TradeResult:
    return TradeResult(_sig(direction=direction, kind=kind), 100.0, 100.0 + r,
                       ExitReason.TARGET if r > 0 else ExitReason.STOP, r, 1, max(r, 0), min(r, 0),
                       "2025-01-01T00:00:00+00:00", "2025-01-01T00:05:00+00:00")


def test_compute_metrics() -> None:
    trades = [_trade(r) for r in (1.0, -1.0, 2.0, -1.0, 0.5)]
    m = compute_metrics(trades)
    assert m["trades"] == 5 and m["wins"] == 3 and m["losses"] == 2
    assert m["win_rate"] == 60.0
    assert m["expectancy_r"] == pytest.approx(0.3)
    assert m["total_r"] == pytest.approx(1.5)
    assert m["profit_factor"] == pytest.approx(1.75)


def test_max_drawdown() -> None:
    curve = equity_curve([_trade(r) for r in (1.0, -1.0, 2.0, -1.0, 0.5)])
    assert curve == [1.0, 0.0, 2.0, 1.0, 1.5]
    assert max_drawdown_r(curve) == pytest.approx(1.0)


def test_metrics_empty() -> None:
    assert compute_metrics([])["trades"] == 0


# --------------------------------------------------------------------------- #
# engine with a synthetic data source
# --------------------------------------------------------------------------- #
class FakeSource:
    """Bullish chain + a rising price path -> one winning long trade."""

    def __init__(self) -> None:
        spec = [(k, 1000, 1500) for k in range(24000, 24900, 100)]
        spec[0] = (24000, 1000, 8000)     # put support wall below spot
        spec[-1] = (24800, 6000, 1000)    # resistance far above
        self._rows = [
            {"strike": s, "option_type": ot, "oi": (ce if ot == "CE" else pe),
             "oi_change": 0, "volume": 0, "ltp": 50.0, "expiry": "2025-01-30",
             "ts": "2025-01-01T03:45:00+00:00"}
            for (s, ce, pe) in spec for ot in ("CE", "PE")
        ]
        base = datetime(2025, 1, 1, 3, 45, tzinfo=timezone.utc)
        # Rising path that clears target1 (~24146).
        self._path = [
            {"ts": (base + timedelta(minutes=i)).isoformat(), "ltp": 24050 + i * 20}
            for i in range(1, 12)
        ]

    def bounds(self, index_name): return ("2025-01-01T03:45:00+00:00", "2025-01-01T04:00:00+00:00")
    def evaluation_times(self, index_name, start, end, interval_sec): return [start]
    def chain_asof(self, index_name, ts): return self._rows
    def spot_asof(self, index_name, ts): return 24050.0
    def spot_path(self, index_name, after_ts, until_ts): return self._path


def test_backtester_produces_winning_long() -> None:
    bt = Backtester(SignalEngine(), FakeSource(), BacktestConfig(target_index=1))
    report = bt.run("NIFTY")
    assert report.metrics["trades"] == 1
    trade = report.trades[0]
    assert trade.signal.direction is Direction.BULLISH
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.realized_r > 0
    assert report.insights  # non-empty


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #
def test_insights_flag_small_sample() -> None:
    trades = [_trade(r) for r in (1.0, -1.0, 0.5)]
    report = BacktestReport(index_name="NIFTY", trades=trades,
                            metrics=compute_metrics(trades))
    lines = generate_insights(report)
    assert any("too few" in s.lower() or "directional" in s.lower() for s in lines)


def test_insights_no_trades() -> None:
    report = BacktestReport(index_name="NIFTY", metrics=compute_metrics([]))
    assert generate_insights(report)
