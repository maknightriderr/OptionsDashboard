"""
Backtest runner / entrypoint.

    python -m backtest.runner            # backtest all configured indices
    python -m backtest.runner NIFTY      # just one

Reads stored history (read-only), runs the backtest, prints a summary, and
writes a JSON report under logs/ for later inspection.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from config.settings import get_settings
from database.factory import make_database
from signals.engine import SignalConfig, SignalEngine
from utils.logging import configure_logging

from backtest.datasource import DatabaseDataSource
from backtest.engine import Backtester
from backtest.models import BacktestConfig, BacktestReport

logger = logging.getLogger(__name__)


def run_backtest(index_name: str) -> BacktestReport:
    settings = get_settings()
    db = make_database(settings, read_only=True)
    db.connect()
    try:
        source = DatabaseDataSource(db)
        backtester = Backtester(SignalEngine(SignalConfig()), source, BacktestConfig())
        return backtester.run(index_name)
    finally:
        db.close()


def _print_summary(report: BacktestReport) -> None:
    m = report.metrics
    print(f"\n=== Backtest: {report.index_name} ===")
    if not report.trades:
        print("No trades generated.")
    else:
        print(f"Trades: {m['trades']}  Win rate: {m['win_rate']}%  "
              f"Expectancy: {m['expectancy_r']:+.3f}R  Total: {m['total_r']:+.1f}R")
        print(f"Profit factor: {m['profit_factor']}  Max DD: {m['max_drawdown_r']}R")
    print("\nInsights:")
    for line in report.insights:
        print(f"  • {line}")


def _save(report: BacktestReport, settings_log_dir: str) -> Path:
    Path(settings_log_dir).mkdir(parents=True, exist_ok=True)
    out = Path(settings_log_dir) / f"backtest_{report.index_name}.json"
    payload = {
        "index_name": report.index_name,
        "metrics": report.metrics,
        "by_kind": report.by_kind,
        "by_direction": report.by_direction,
        "equity_curve": report.equity_curve,
        "insights": report.insights,
        "trades": [
            {**{k: v for k, v in asdict(t).items() if k != "signal"},
             "direction": t.signal.direction.value, "kind": t.signal.kind.value}
            for t in report.trades
        ],
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    indices = [sys.argv[1].upper()] if len(sys.argv) > 1 else settings.indices
    for index_name in indices:
        report = run_backtest(index_name)
        _print_summary(report)
        path = _save(report, settings.log_dir)
        print(f"\nSaved report → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
