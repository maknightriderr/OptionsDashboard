"""
Schema definitions (DDL).

Phase 1 only needs spot ticks and the option-chain snapshot stream, but the
table set is named and shaped so later phases (signals, trades, alerts) slot in
without renaming anything. Times are stored as ISO-8601 UTC strings for
portability across SQLite and PostgreSQL; convert at the edges if you need IST.

The placeholder style here is SQLite's "?"; the Postgres implementation will
re-template these with "%s". DDL itself is standard enough to be shared.
"""

from __future__ import annotations

# Each statement is created with IF NOT EXISTS so init_schema is idempotent.
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS spot_ticks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        token      TEXT    NOT NULL,
        ltp        REAL    NOT NULL,
        ts         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS option_ticks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        token       TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        strike      REAL    NOT NULL,
        option_type TEXT    NOT NULL,   -- CE / PE
        expiry      TEXT    NOT NULL,
        ltp         REAL    NOT NULL,
        volume      INTEGER NOT NULL DEFAULT 0,
        oi          INTEGER NOT NULL DEFAULT 0,
        oi_change   INTEGER NOT NULL DEFAULT 0,
        bid         REAL    NOT NULL DEFAULT 0,
        ask         REAL    NOT NULL DEFAULT 0,
        ts          TEXT    NOT NULL
    )
    """,
    # Hot query path: "latest rows for this underlying" -> index on (name, ts).
    "CREATE INDEX IF NOT EXISTS idx_option_ticks_name_ts ON option_ticks (name, ts)",
    "CREATE INDEX IF NOT EXISTS idx_spot_ticks_name_ts   ON spot_ticks (name, ts)",
    """
    CREATE TABLE IF NOT EXISTS signals (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        index_name     TEXT    NOT NULL,
        direction      TEXT    NOT NULL,   -- bullish / bearish / neutral
        kind           TEXT    NOT NULL,
        spot           REAL    NOT NULL,
        confidence     INTEGER NOT NULL,
        risk           INTEGER NOT NULL,
        probability    INTEGER NOT NULL,
        entry          REAL    NOT NULL,
        stop_loss      REAL    NOT NULL,
        target1        REAL    NOT NULL,
        target2        REAL    NOT NULL,
        target3        REAL    NOT NULL,
        reason         TEXT    NOT NULL,
        supporting     TEXT    NOT NULL,   -- JSON array of indicator labels
        ts             TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_name_ts ON signals (index_name, ts)",
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        index_name  TEXT    NOT NULL,
        priority    INTEGER NOT NULL,   -- 0 LOW .. 3 CRITICAL
        direction   TEXT    NOT NULL,
        kind        TEXT    NOT NULL,
        confidence  INTEGER NOT NULL,
        message     TEXT    NOT NULL,
        channel     TEXT    NOT NULL,
        status      TEXT    NOT NULL,   -- sent / failed / suppressed
        ts          TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alerts_name_ts ON alerts (index_name, ts)",
    """
    CREATE TABLE IF NOT EXISTS iv_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        index_name  TEXT    NOT NULL,
        atm_iv      REAL    NOT NULL,   -- ATM implied vol in percent
        ts          TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_iv_history_name_ts ON iv_history (index_name, ts)",
)
