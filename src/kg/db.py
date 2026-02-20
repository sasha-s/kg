"""DB connection: sqlite3 (default) or libsql (Turso)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from kg.config import KGConfig


def get_conn(cfg: KGConfig) -> sqlite3.Connection:
    """Return a database connection for the given config.

    If cfg.use_turso is True, connects to Turso via libsql
    (install with: uv add --optional turso libsql).
    Otherwise opens a local SQLite file at cfg.db_path with WAL mode and
    foreign key enforcement.

    The returned object is sqlite3.Connection-compatible in both cases.
    """
    if cfg.use_turso:
        try:
            import libsql  # type: ignore[import-not-found]
            conn: sqlite3.Connection = libsql.connect(
                cfg.database.url,
                auth_token=cfg.database.token,
            )
            return conn
        except ImportError:
            pass  # fall through to local SQLite

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    local_conn = sqlite3.connect(str(cfg.db_path))
    local_conn.execute("PRAGMA journal_mode=WAL")
    local_conn.execute("PRAGMA foreign_keys=ON")
    return local_conn


def cfg_from_path(db_path: Path) -> sqlite3.Connection:
    """Open a local SQLite connection directly from a file path.

    Convenience for callers that don't have a full KGConfig.
    Enables WAL mode and foreign key enforcement.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
