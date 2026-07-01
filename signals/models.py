"""
Signal data model.

A Signal is the engine's output: a directional read with a confidence/risk/
probability triple and a concrete trade frame (entry, stop, three targets) plus
the human-readable reasoning and the indicators that supported it.

NOTE ON SEMANTICS: confidence, risk, and probability are *heuristic* scores
derived from how strongly and how unanimously the indicators agree. They are
decision aids, not statistical guarantees, and nothing here is trading advice.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SignalKind(str, Enum):
    """Catalogue of signal archetypes (Phase 3 implements a subset)."""

    TREND_FOLLOWING = "trend_following"
    MOMENTUM = "momentum"
    REVERSAL = "reversal"
    BREAKOUT = "breakout"
    BREAKDOWN = "breakdown"
    # Reserved for later phases: SCALP, EXPIRY_SPECIAL, INTRADAY, ...


class Signal(BaseModel):
    """One scored, actionable signal."""

    index_name: str
    direction: Direction
    kind: SignalKind
    spot: float

    confidence: int = Field(ge=0, le=100, description="Strength of agreement, 0-100")
    risk: int = Field(ge=0, le=100, description="Heuristic risk, higher = riskier")
    probability: int = Field(ge=0, le=100, description="Heuristic win-lean, 0-100")

    entry: float
    stop_loss: float
    target1: float
    target2: float
    target3: float

    reason: str
    supporting_indicators: list[str] = Field(default_factory=list)
    ts: datetime = Field(default_factory=_utcnow)
