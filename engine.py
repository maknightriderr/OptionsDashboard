"""
The backtest engine.

Walks a time grid over stored history; at each step it reconstructs the option
chain as it looked then, runs the live SignalEngine, and — when a signal fires —
simulates the trade against the subsequent spot path. A per (direction, kind)
cooldown prevents reopening the same setup on every adjacent step.

Because it drives the exact same SignalEngine the live runner uses, the backtest
measures the real strategy, not a re-implementation of it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from analytics.indicators import build_chain_dataframe
from signals.engine import SignalEngine
from signals.models import Signal

from backtest.datasource import BacktestDataSource
from backtest.insights import generate_insights
from backtest.metrics import compute_metrics, equity_curve, group_metrics
from backtest.models import BacktestConfig, BacktestReport, TradeResult
from backtest.simulator import simulate_trade

logger = logging.getLogger(__name__)


class Backtester:
    """Replays history through a SignalEngine and simulates the resulting trades."""

    def __init__(
        self,
        engine: SignalEngine,
        data_source: BacktestDataSource,
        config: BacktestConfig | None = None,
    ) -> None:
        self._engine = engine
        self._src = data_source
        self._cfg = config or BacktestConfig()

    def run(
        self, index_name: str, start: str | None = None, end: str | None = None
    ) -> BacktestReport:
        bounds = self._src.bounds(index_name)
        if bounds is None:
            return BacktestReport(index_name=index_name,
                                  insights=["No spot history available to backtest."])
        start = start or bounds[0]
        end = end or bounds[1]

        times = self._src.evaluation_times(
            index_name, start, end, self._cfg.eval_interval_sec
        )
        trades: list[TradeResult] = []
        last_open: dict[tuple[str, str], datetime] = {}

        for ts in times:
            rows = self._src.chain_asof(index_name, ts)
            if not rows:
                continue
            spot = self._src.spot_asof(index_name, ts)
            if spot is None:
                continue

            cutoff = (datetime.fromisoformat(ts)
                      - timedelta(seconds=self._cfg.lookback_sec)).isoformat()
            prev_rows = self._src.chain_asof(index_name, cutoff)
            current = build_chain_dataframe(rows)
            previous = build_chain_dataframe(prev_rows) if prev_rows else None

            for signal in self._engine.evaluate(index_name, spot, current, previous):
                if self._in_cooldown(signal, ts, last_open):
                    continue
                trade = self._simulate(signal, ts)
                if trade is not None:
                    trades.append(trade)
                    last_open[(signal.direction.value, signal.kind.value)] = \
                        datetime.fromisoformat(ts)

        return self._build_report(index_name, trades)

    # ---- helpers ------------------------------------------------------------
    def _in_cooldown(
        self, signal: Signal, ts: str, last_open: dict[tuple[str, str], datetime]
    ) -> bool:
        key = (signal.direction.value, signal.kind.value)
        last = last_open.get(key)
        if last is None:
            return False
        return (datetime.fromisoformat(ts) - last).total_seconds() < self._cfg.signal_cooldown_sec

    def _simulate(self, signal: Signal, ts: str) -> TradeResult | None:
        # Align the signal's timestamp with the evaluation time for path fidelity.
        signal.ts = datetime.fromisoformat(ts)
        until = (datetime.fromisoformat(ts)
                 + timedelta(seconds=self._cfg.max_hold_sec)).isoformat()
        path = self._src.spot_path(signal.index_name, ts, until)
        if not path:
            return None
        target = {1: signal.target1, 2: signal.target2, 3: signal.target3}.get(
            self._cfg.target_index, signal.target1
        )
        return simulate_trade(signal, target, path)

    def _build_report(self, index_name: str, trades: list[TradeResult]) -> BacktestReport:
        report = BacktestReport(
            index_name=index_name,
            trades=trades,
            metrics=compute_metrics(trades),
            by_kind=group_metrics(trades, "kind"),
            by_direction=group_metrics(trades, "direction"),
            equity_curve=equity_curve(trades),
        )
        report.insights = generate_insights(report)
        logger.info(
            "Backtest %s: %d trades, expectancy %.3fR, total %.1fR.",
            index_name, report.metrics.get("trades", 0),
            report.metrics.get("expectancy_r", 0.0), report.metrics.get("total_r", 0.0),
        )
        return report
