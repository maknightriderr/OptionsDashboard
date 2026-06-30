"""
SQLite implementation of the Database interface.

Two production-relevant details are handled here:

1. WAL mode. The collector writes continuously while the dashboard reads. In
   the default rollback journal that produces "database is locked" errors.
   Write-Ahead Logging lets readers and a writer proceed concurrently.

2. Thread safety. SmartWebSocketV2 invokes callbacks on its own thread, so the
   connection is opened with check_same_thread=False and every statement is
   guarded by a re-entrant lock. SQLite still serialises writers, but the lock
   keeps our access well-defined and avoids interleaved executemany batches.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from database.interface import Database
from database.models import OptionTick, SpotTick
from database.schema import SCHEMA_STATEMENTS

logger = logging.getLogger(__name__)


class SQLiteDatabase(Database):
    """File-backed SQLite store tuned for concurrent read/write."""

    def __init__(self, db_path: str, read_only: bool = False) -> None:
        self._db_path = db_path
        self._read_only = read_only
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ---- lifecycle ----------------------------------------------------------
    def connect(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,   # accessed from the websocket thread
            timeout=30.0,              # wait rather than instantly erroring on lock
        )
        self._conn.row_factory = sqlite3.Row
        # Concurrency + durability pragmas.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, much faster
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self._read_only:
            # The dashboard opens the same file; query_only forbids writes at the
            # connection level while still allowing WAL shared-memory reads
            # (cleaner than OS-level mode=ro, which can stumble on the -shm file).
            self._conn.execute("PRAGMA query_only=ON")
        self._conn.commit()
        logger.info(
            "SQLite connected at %s (WAL%s).",
            self._db_path, ", read-only" if self._read_only else "",
        )

    def init_schema(self) -> None:
        conn = self._require_conn()
        with self._lock:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
        logger.info("Schema ensured (%d statements).", len(SCHEMA_STATEMENTS))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()
                self._conn.close()
                self._conn = None
                logger.info("SQLite connection closed.")

    # ---- writes -------------------------------------------------------------
    def insert_spot(self, tick: SpotTick) -> None:
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                "INSERT INTO spot_ticks (name, token, ltp, ts) VALUES (?, ?, ?, ?)",
                (tick.name, tick.token, tick.ltp, tick.ts.isoformat()),
            )
            conn.commit()

    def insert_option_ticks(self, ticks: Iterable[OptionTick]) -> int:
        rows = [
            (
                t.token, t.name, t.strike, t.option_type.value, t.expiry,
                t.ltp, t.volume, t.oi, t.oi_change, t.bid, t.ask, t.ts.isoformat(),
            )
            for t in ticks
        ]
        if not rows:
            return 0
        conn = self._require_conn()
        with self._lock:
            conn.executemany(
                """
                INSERT INTO option_ticks
                    (token, name, strike, option_type, expiry, ltp,
                     volume, oi, oi_change, bid, ask, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    # ---- reads --------------------------------------------------------------
    def fetch_recent_option_ticks(
        self, name: str, limit: int = 100
    ) -> Sequence[dict[str, Any]]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT * FROM option_ticks WHERE name = ? ORDER BY ts DESC LIMIT ?",
                (name, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def fetch_latest_option_chain(self, name: str) -> Sequence[dict[str, Any]]:
        """
        Latest tick per contract. We pick MAX(id) per token rather than MAX(ts):
        id is monotonic with insertion, so it is an exact "most recent row"
        selector and is immune to any timestamp formatting quirks.
        """
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                SELECT t.*
                FROM option_ticks AS t
                JOIN (
                    SELECT MAX(id) AS mid
                    FROM option_ticks
                    WHERE name = ?
                    GROUP BY token
                ) AS latest ON t.id = latest.mid
                ORDER BY t.strike ASC, t.option_type ASC
                """,
                (name,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def fetch_latest_spot(self, name: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT * FROM spot_ticks WHERE name = ? ORDER BY id DESC LIMIT 1",
                (name,),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def fetch_available_indices(self) -> Sequence[str]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute("SELECT DISTINCT name FROM option_ticks ORDER BY name")
            return [row["name"] for row in cursor.fetchall()]

    def fetch_option_chain_asof(
        self, name: str, before_ts: str
    ) -> Sequence[dict[str, Any]]:
        """Latest tick per contract whose ts <= before_ts (earlier snapshot)."""
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                SELECT t.*
                FROM option_ticks AS t
                JOIN (
                    SELECT token, MAX(id) AS mid
                    FROM option_ticks
                    WHERE name = ? AND ts <= ?
                    GROUP BY token
                ) AS latest ON t.id = latest.mid
                ORDER BY t.strike ASC, t.option_type ASC
                """,
                (name, before_ts),
            )
            return [dict(row) for row in cursor.fetchall()]

    def insert_signal(self, signal: "Signal") -> int:
        import json
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                INSERT INTO signals
                    (index_name, direction, kind, spot, confidence, risk, probability,
                     entry, stop_loss, target1, target2, target3, reason, supporting, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.index_name, signal.direction.value, signal.kind.value,
                    signal.spot, signal.confidence, signal.risk, signal.probability,
                    signal.entry, signal.stop_loss, signal.target1, signal.target2,
                    signal.target3, signal.reason,
                    json.dumps(signal.supporting_indicators), signal.ts.isoformat(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def fetch_recent_signals(
        self, name: str, limit: int = 20
    ) -> Sequence[dict[str, Any]]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT * FROM signals WHERE index_name = ? ORDER BY id DESC LIMIT ?",
                (name, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def insert_alert(self, alert: "Alert") -> int:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                INSERT INTO alerts
                    (index_name, priority, direction, kind, confidence,
                     message, channel, status, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.index_name, int(alert.priority), alert.direction, alert.kind,
                    alert.confidence, alert.message, alert.channel, alert.status,
                    alert.ts.isoformat(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def fetch_recent_alerts(
        self, name: str, limit: int = 20
    ) -> Sequence[dict[str, Any]]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT * FROM alerts WHERE index_name = ? ORDER BY id DESC LIMIT ?",
                (name, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def insert_iv_snapshot(self, name: str, atm_iv: float, ts: str) -> int:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "INSERT INTO iv_history (index_name, atm_iv, ts) VALUES (?, ?, ?)",
                (name, atm_iv, ts),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def fetch_iv_history(self, name: str, limit: int = 500) -> Sequence[float]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                SELECT atm_iv FROM (
                    SELECT atm_iv, id FROM iv_history
                    WHERE index_name = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (name, limit),
            )
            return [float(row["atm_iv"]) for row in cursor.fetchall()]

    def fetch_spot_asof(self, name: str, ts: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                "SELECT * FROM spot_ticks WHERE name = ? AND ts <= ? ORDER BY id DESC LIMIT 1",
                (name, ts),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def fetch_spot_series(
        self, name: str, start_ts: str, end_ts: str
    ) -> Sequence[dict[str, Any]]:
        conn = self._require_conn()
        with self._lock:
            cursor = conn.execute(
                """
                SELECT ts, ltp FROM spot_ticks
                WHERE name = ? AND ts >= ? AND ts <= ?
                ORDER BY id ASC
                """,
                (name, start_ts, end_ts),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---- helpers ------------------------------------------------------------
    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before use.")
        return self._conn
