"""
Database factory.

Centralises the one place that knows about concrete backends. Callers ask for
``make_database(settings)`` and receive something typed as the abstract
``Database``. Adding PostgreSQL later means adding one branch here plus a
``PostgresDatabase`` class — nothing else in the codebase changes.
"""

from __future__ import annotations

from config.settings import Settings
from database.interface import Database
from database.sqlite_db import SQLiteDatabase


def make_database(settings: Settings, read_only: bool = False) -> Database:
    """Construct the configured database backend (not yet connected).

    ``read_only`` is used by the dashboard so it can attach to the same file the
    collector writes without any risk of mutating it.
    """
    if settings.db_backend == "sqlite":
        return SQLiteDatabase(settings.db_path, read_only=read_only)
    if settings.db_backend == "postgres":
        # Placeholder for Phase 3+. Implement PostgresDatabase(settings.db_dsn)
        # behind the same Database interface and return it here.
        raise NotImplementedError(
            "PostgreSQL backend not implemented yet. Set OT_DB_BACKEND=sqlite."
        )
    raise ValueError(f"Unknown db_backend: {settings.db_backend!r}")
