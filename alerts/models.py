"""
Alert model + priority.

Priority is derived from a signal's confidence and risk so the dispatcher can
decide what is worth pushing and how loudly. Thresholds are injected via
PriorityConfig (no magic numbers at call sites).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum

from pydantic import BaseModel, Field

from signals.models import Signal


class Priority(IntEnum):
    """Ordered so comparisons work: CRITICAL > HIGH > MEDIUM > LOW."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @classmethod
    def from_name(cls, name: str) -> "Priority":
        return cls[name.upper()]


@dataclass(frozen=True)
class PriorityConfig:
    """Confidence/risk cut-offs for each priority band."""

    critical_confidence: int = 85
    critical_max_risk: int = 40
    high_confidence: int = 70
    medium_confidence: int = 55


def map_priority(signal: Signal, config: PriorityConfig | None = None) -> Priority:
    """Translate a signal's scores into an alert priority."""
    cfg = config or PriorityConfig()
    if signal.confidence >= cfg.critical_confidence and signal.risk <= cfg.critical_max_risk:
        return Priority.CRITICAL
    if signal.confidence >= cfg.high_confidence:
        return Priority.HIGH
    if signal.confidence >= cfg.medium_confidence:
        return Priority.MEDIUM
    return Priority.LOW


class Alert(BaseModel):
    """A dispatched (or attempted) alert, persisted for history/audit."""

    index_name: str
    priority: Priority
    direction: str
    kind: str
    confidence: int
    message: str
    channel: str
    status: str = "sent"            # sent / failed / suppressed
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
