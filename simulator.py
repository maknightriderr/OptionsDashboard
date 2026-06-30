"""
Trade simulation (pure, framework-free, unit-tested).

Given a signal's entry/stop/target and the subsequent spot price path, decide
how the trade resolves. Outcomes are in R-multiples where 1R = the trade's own
risk (distance from entry to stop), so a stop is always -1R and a target is the
target distance expressed in R.

Fill assumptions (documented so results are interpretable):
  * We only have spot tick samples, not full OHLC bars, so crossings are checked
    per tick. The first tick to breach the stop or target ends the trade and is
    assumed to fill at the level (not at the tick's price) — a small optimistic
    simplification consistent across all trades.
  * If neither level is touched before the path ends, the trade exits at the
    last available price (EOD) or at max-hold (TIME).
"""

from __future__ import annotations

from signals.models import Direction, Signal

from backtest.models import ExitReason, TradeResult


def simulate_trade(
    signal: Signal,
    target: float,
    price_path: list[dict],
    max_bars: int | None = None,
) -> TradeResult:
    """
    Resolve a single trade against ``price_path`` (list of {ts, ltp}, oldest→newest).

    ``target`` is the chosen exit target (T1/T2/T3). ``max_bars`` forces a
    time-exit after that many ticks.
    """
    entry = signal.entry
    stop = signal.stop_loss
    is_long = signal.direction is Direction.BULLISH
    risk = abs(entry - stop) or 1e-9

    def to_r(price: float) -> float:
        return (price - entry) / risk if is_long else (entry - price) / risk

    mfe_r = 0.0
    mae_r = 0.0
    last_price = entry
    last_ts = signal.ts.isoformat()
    bars = 0

    for i, point in enumerate(price_path):
        price = float(point["ltp"])
        last_price = price
        last_ts = point.get("ts", last_ts)
        bars = i + 1
        r_now = to_r(price)
        mfe_r = max(mfe_r, r_now)
        mae_r = min(mae_r, r_now)

        stop_hit = price <= stop if is_long else price >= stop
        target_hit = price >= target if is_long else price <= target

        if stop_hit:
            return TradeResult(signal, entry, stop, ExitReason.STOP, to_r(stop),
                               bars, mfe_r, mae_r, signal.ts.isoformat(), last_ts)
        if target_hit:
            return TradeResult(signal, entry, target, ExitReason.TARGET, to_r(target),
                               bars, mfe_r, mae_r, signal.ts.isoformat(), last_ts)
        if max_bars is not None and bars >= max_bars:
            return TradeResult(signal, entry, price, ExitReason.TIME, r_now,
                               bars, mfe_r, mae_r, signal.ts.isoformat(), last_ts)

    return TradeResult(signal, entry, last_price, ExitReason.EOD, to_r(last_price),
                       bars, mfe_r, mae_r, signal.ts.isoformat(), last_ts)
