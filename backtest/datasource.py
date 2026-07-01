"""
Backtest data source.

The backtester depends on this small Protocol rather than the database directly,
so it can be driven by synthetic in-memory data in tests and by real stored
history in production — same engine, swappable source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from database.interface import Database


@runtime_checkable
class BacktestDataSource(Protocol):
    def bounds(self, index_name: str) -> tuple[str, str] | None:
        """(earliest_ts, latest_ts) of available spot data, or None if empty."""

    def evaluation_times(
        self, index_name: str, start: str, end: str, interval_sec: int
    ) -> list[str]:
        """Regular grid of ISO timestamps in [start, end]."""

    def chain_asof(self, index_name: str, ts: str) -> list[dict]:
        """Latest option tick per contract with ts <= given (rows)."""

    def spot_asof(self, index_name: str, ts: str) -> float | None:
        """Latest spot price with ts <= given."""

    def spot_path(self, index_name: str, after_ts: str, until_ts: str) -> list[dict]:
        """Spot ticks strictly after entry up to until_ts (oldest→newest)."""


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class DatabaseDataSource:
    """BacktestDataSource backed by a (read-only) Database."""

    def __init__(self, database: Database, max_evaluations: int = 5000) -> None:
        self._db = database
        self._max_evaluations = max_evaluations

    def bounds(self, index_name: str) -> tuple[str, str] | None:
        series = self._db.fetch_spot_series(index_name, "0000", "9999")
        if not series:
            return None
        return series[0]["ts"], series[-1]["ts"]

    def evaluation_times(
        self, index_name: str, start: str, end: str, interval_sec: int
    ) -> list[str]:
        t = _parse(start)
        end_dt = _parse(end)
        step = timedelta(seconds=max(interval_sec, 1))
        times: list[str] = []
        while t <= end_dt and len(times) < self._max_evaluations:
            times.append(t.isoformat())
            t += step
        return times

    def chain_asof(self, index_name: str, ts: str) -> list[dict]:
        return list(self._db.fetch_option_chain_asof(index_name, ts))

    def spot_asof(self, index_name: str, ts: str) -> float | None:
        row = self._db.fetch_spot_asof(index_name, ts)
        return float(row["ltp"]) if row else None

    def spot_path(self, index_name: str, after_ts: str, until_ts: str) -> list[dict]:
        rows = self._db.fetch_spot_series(index_name, after_ts, until_ts)
        # Strictly after the entry timestamp.
        return [r for r in rows if r["ts"] > after_ts]
