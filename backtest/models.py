"""
Backtest data models.

A backtest replays historical snapshots through the live SignalEngine, opens a
simulated trade whenever a signal fires, and walks the subsequent spot path to a
stop or target. Outcomes are measured in R-multiples (multiples of the risk per
trade) so results are comparable across price levels and indices.

Nothing here trades real money — it is a simulator for evaluating signal edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from signals.models import Signal


class ExitReason(str, Enum):
    STOP = "stop"        # hit the stop-loss (-1R by construction)
    TARGET = "target"    # hit the chosen target
    TIME = "time"        # max holding period elapsed
    EOD = "eod"          # ran out of data


@dataclass(frozen=True)
class BacktestConfig:
    """Knobs for a backtest run (injected, never hardcoded at call sites)."""

    eval_interval_sec: int = 60        # how often to re-evaluate signals
    lookback_sec: int = 300            # OI-flow lookback for the engine
    signal_cooldown_sec: int = 300     # don't reopen same (dir,kind) within this
    target_index: int = 1              # which target to exit on: 1, 2, or 3
    max_hold_sec: int = 3600           # force time-exit after this long


@dataclass(frozen=True)
class TradeResult:
    """The outcome of one simulated trade."""

    signal: Signal
    entry: float
    exit_price: float
    exit_reason: ExitReason
    realized_r: float
    bars_held: int
    mfe_r: float          # max favourable excursion, in R
    mae_r: float          # max adverse excursion, in R
    entry_ts: str
    exit_ts: str


@dataclass
class BacktestReport:
    """Trades plus aggregate performance metrics."""

    index_name: str
    trades: list[TradeResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)
    by_direction: dict[str, dict[str, float]] = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list)   # cumulative R
    insights: list[str] = field(default_factory=list)
