"""
The persistence boundary.

Everything upstream (collector, future signal engine, dashboard) talks to this
abstract interface, never to a concrete driver. That is the whole reason the
"easy migration to PostgreSQL" requirement is cheap: write a second
implementation of this ABC and swap it in the factory. No caller changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from database.models import OptionTick, SpotTick
from signals.models import Signal
from alerts.models import Alert


class Database(ABC):
    """Abstract storage backend for market snapshots."""

    @abstractmethod
    def connect(self) -> None:
        """Open the connection and apply backend-specific pragmas/tuning."""

    @abstractmethod
    def init_schema(self) -> None:
        """Create tables and indexes if they do not already exist."""

    @abstractmethod
    def insert_spot(self, tick: SpotTick) -> None:
        """Persist one underlying spot tick."""

    @abstractmethod
    def insert_option_ticks(self, ticks: Iterable[OptionTick]) -> int:
        """Persist a batch of option ticks. Returns the number of rows written."""

    @abstractmethod
    def fetch_recent_option_ticks(
        self, name: str, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        """Return the most recent option ticks for an underlying (newest first)."""

    @abstractmethod
    def fetch_latest_option_chain(self, name: str) -> Sequence[dict[str, Any]]:
        """Return the single latest tick for every contract of an underlying."""

    @abstractmethod
    def fetch_latest_spot(self, name: str) -> dict[str, Any] | None:
        """Return the latest spot tick for an underlying, or None if absent."""

    @abstractmethod
    def fetch_available_indices(self) -> Sequence[str]:
        """Return distinct underlyings that currently have option data."""

    @abstractmethod
    def fetch_option_chain_asof(
        self, name: str, before_ts: str
    ) -> Sequence[dict[str, Any]]:
        """Latest tick per contract with ts <= before_ts (for OI-flow deltas)."""

    @abstractmethod
    def insert_signal(self, signal: "Signal") -> int:
        """Persist a signal; return its new row id."""

    @abstractmethod
    def fetch_recent_signals(
        self, name: str, limit: int = 20
    ) -> Sequence[dict[str, Any]]:
        """Return the most recent signals for an underlying (newest first)."""

    @abstractmethod
    def insert_alert(self, alert: "Alert") -> int:
        """Persist a dispatched/suppressed alert; return its new row id."""

    @abstractmethod
    def fetch_recent_alerts(
        self, name: str, limit: int = 20
    ) -> Sequence[dict[str, Any]]:
        """Return the most recent alerts for an underlying (newest first)."""

    @abstractmethod
    def insert_iv_snapshot(self, name: str, atm_iv: float, ts: str) -> int:
        """Persist an ATM IV reading; return its new row id."""

    @abstractmethod
    def fetch_iv_history(self, name: str, limit: int = 500) -> Sequence[float]:
        """Return recent ATM IV values for an underlying (oldest→newest)."""

    @abstractmethod
    def fetch_spot_asof(self, name: str, ts: str) -> dict[str, Any] | None:
        """Latest spot tick with ts <= given timestamp (point-in-time, for backtests)."""

    @abstractmethod
    def fetch_spot_series(
        self, name: str, start_ts: str, end_ts: str
    ) -> Sequence[dict[str, Any]]:
        """Spot ticks in [start_ts, end_ts], oldest→newest (price path / bounds)."""

    @abstractmethod
    def close(self) -> None:
        """Flush and close the connection."""

    # Context-manager sugar so callers can use `with make_database(...) as db:`.
    def __enter__(self) -> "Database":
        self.connect()
        self.init_schema()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
