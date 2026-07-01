"""
Typed records that flow between the collector and the database.

Keeping these as Pydantic models (rather than loose dicts) gives us validation
at the boundary, free serialisation, and a single source of truth for the
column set. The DB layer maps these to/from rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class OptionType(str, Enum):
    CALL = "CE"
    PUT = "PE"


class Instrument(BaseModel):
    """A single tradable option contract resolved from the scrip master."""

    token: str                       # Angel One symbol token (exchange token)
    symbol: str                      # e.g. NIFTY25JAN24500CE
    name: str                        # underlying, e.g. NIFTY
    exchange: str = "NFO"
    strike: float
    option_type: OptionType
    expiry: str                      # ISO date string, e.g. 2025-01-30
    lot_size: int


class SpotTick(BaseModel):
    """Underlying index spot price tick."""

    name: str
    token: str
    ltp: float
    ts: datetime = Field(default_factory=_utcnow)


class OptionTick(BaseModel):
    """
    One normalised market-data tick for a single option contract.

    This is what the WebSocket callback produces after decoding a raw binary
    packet; it is also the row shape persisted to the option_chain table.
    """

    token: str
    name: str
    strike: float
    option_type: OptionType
    expiry: str
    ltp: float
    volume: int = 0
    oi: int = 0
    oi_change: int = 0
    bid: float = 0.0
    ask: float = 0.0
    ts: datetime = Field(default_factory=_utcnow)
