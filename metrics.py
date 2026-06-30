"""
Performance metrics (pure, unit-tested).

All returns are in R-multiples. The headline numbers:
  * win_rate      — share of trades with positive R
  * expectancy    — average R per trade (the single most important number)
  * profit_factor — gross winning R / gross losing R
  * total_r       — sum of R across all trades
  * max_drawdown_r— largest peak-to-trough drop on the cumulative-R equity curve
"""

from __future__ import annotations

from collections.abc import Iterable

from backtest.models import TradeResult


def equity_curve(trades: Iterable[TradeResult]) -> list[float]:
    """Cumulative R after each trade, in order."""
    curve: list[float] = []
    running = 0.0
    for t in trades:
        running += t.realized_r
        curve.append(round(running, 4))
    return curve


def max_drawdown_r(curve: list[float]) -> float:
    """Largest peak-to-trough decline on the equity curve (in R, >= 0)."""
    peak = 0.0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return round(max_dd, 4)


def compute_metrics(trades: list[TradeResult]) -> dict[str, float]:
    """Aggregate a list of trades into headline metrics."""
    n = len(trades)
    if n == 0:
        return {"trades": 0}

    rs = [t.realized_r for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    curve = equity_curve(trades)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100.0, 1),
        "expectancy_r": round(sum(rs) / n, 3),
        "total_r": round(sum(rs), 3),
        "avg_win_r": round(gross_win / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "max_drawdown_r": max_drawdown_r(curve),
        "best_r": round(max(rs), 3),
        "worst_r": round(min(rs), 3),
    }


def group_metrics(trades: list[TradeResult], key: str) -> dict[str, dict[str, float]]:
    """Compute metrics bucketed by 'kind' or 'direction'."""
    buckets: dict[str, list[TradeResult]] = {}
    for t in trades:
        label = t.signal.kind.value if key == "kind" else t.signal.direction.value
        buckets.setdefault(label, []).append(t)
    return {label: compute_metrics(ts) for label, ts in buckets.items()}
