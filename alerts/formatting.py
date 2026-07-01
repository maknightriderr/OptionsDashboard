"""
Message formatting.

Pure functions that turn a Signal into a Telegram-ready HTML string. Kept
separate from sending so the wording is unit-testable and reusable across
channels. We use Telegram's HTML parse mode (simpler/safer than MarkdownV2,
which requires escaping a long list of characters).
"""

from __future__ import annotations

from html import escape

from alerts.models import Priority
from signals.models import Direction, Signal

_PRIORITY_BADGE = {
    Priority.CRITICAL: "🔴 CRITICAL",
    Priority.HIGH: "🟠 HIGH",
    Priority.MEDIUM: "🟡 MEDIUM",
    Priority.LOW: "⚪ LOW",
}


def format_signal_html(signal: Signal, priority: Priority) -> str:
    """Render a signal as a Telegram HTML message."""
    arrow = "📈" if signal.direction is Direction.BULLISH else "📉"
    badge = _PRIORITY_BADGE.get(priority, "")
    kind = signal.kind.value.replace("_", " ").title()
    indicators = ", ".join(escape(i) for i in signal.supporting_indicators)

    lines = [
        f"{arrow} <b>{escape(signal.index_name)} — {signal.direction.value.upper()}</b> "
        f"({escape(kind)})",
        f"{badge}",
        "",
        f"<b>Spot:</b> {signal.spot:,.2f}",
        f"<b>Confidence:</b> {signal.confidence}  "
        f"<b>Risk:</b> {signal.risk}  <b>Prob:</b> {signal.probability}",
        "",
        f"<b>Entry:</b> {signal.entry:,.2f}",
        f"<b>Stop:</b> {signal.stop_loss:,.2f}",
        f"<b>Targets:</b> {signal.target1:,.2f} / {signal.target2:,.2f} / {signal.target3:,.2f}",
        "",
        f"<b>Why:</b> {escape(signal.reason)}",
    ]
    if indicators:
        lines.append(f"<b>Indicators:</b> {indicators}")
    lines.append("")
    lines.append("<i>Heuristic signal — not trading advice.</i>")
    return "\n".join(lines)
