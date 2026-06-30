"""
Backtest insights.

Turns the metrics into plain-language observations. This is deliberately
rule-based and conservative rather than an ML/LLM claim engine: every statement
is a direct readout of the numbers, hedged for sample size. (A real LLM summary
could be plugged in here later, but it would still be summarising these same
metrics — so the honest version lives here.)
"""

from __future__ import annotations

from backtest.models import BacktestReport

_MIN_BUCKET = 5      # don't editorialise on buckets smaller than this
_SMALL_SAMPLE = 30


def generate_insights(report: BacktestReport) -> list[str]:
    m = report.metrics
    n = int(m.get("trades", 0))
    if n == 0:
        return ["No trades were generated over this window — nothing to evaluate."]

    out: list[str] = []

    if n < _SMALL_SAMPLE:
        out.append(
            f"Only {n} trades in this window — too few to draw firm conclusions; "
            "treat everything below as directional, not statistically reliable."
        )

    exp = m.get("expectancy_r", 0.0)
    if exp > 0:
        out.append(
            f"Positive expectancy: {exp:+.2f}R per trade across {n} trades "
            f"(total {m.get('total_r', 0):+.1f}R)."
        )
    else:
        out.append(
            f"Negative/flat expectancy: {exp:+.2f}R per trade — over this window the "
            "signals did not show an edge after stops."
        )

    pf = m.get("profit_factor", 0.0)
    if pf != float("inf"):
        out.append(
            f"Win rate {m.get('win_rate', 0):.0f}% with profit factor {pf:.2f} "
            f"(avg win {m.get('avg_win_r', 0):+.2f}R vs avg loss {m.get('avg_loss_r', 0):+.2f}R)."
        )

    out.append(
        f"Worst peak-to-trough drawdown was {m.get('max_drawdown_r', 0):.1f}R — "
        "size positions so a run of losses like that is survivable."
    )

    # Best / worst signal kind (only where the bucket is large enough).
    eligible = {
        k: v for k, v in report.by_kind.items()
        if int(v.get("trades", 0)) >= _MIN_BUCKET
    }
    if eligible:
        best = max(eligible.items(), key=lambda kv: kv[1].get("expectancy_r", 0))
        worst = min(eligible.items(), key=lambda kv: kv[1].get("expectancy_r", 0))
        out.append(
            f"By type: '{best[0]}' performed best ({best[1].get('expectancy_r', 0):+.2f}R/trade), "
            f"'{worst[0]}' worst ({worst[1].get('expectancy_r', 0):+.2f}R/trade)."
        )

    # Direction skew.
    dirs = report.by_direction
    if {"bullish", "bearish"}.issubset(dirs):
        b = dirs["bullish"].get("expectancy_r", 0)
        s = dirs["bearish"].get("expectancy_r", 0)
        if abs(b - s) >= 0.2:
            stronger = "long" if b > s else "short"
            out.append(
                f"Clear directional skew: {stronger} signals were stronger "
                f"(long {b:+.2f}R vs short {s:+.2f}R per trade)."
            )

    return out
