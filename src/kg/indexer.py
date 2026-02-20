"""Build/rebuild the SQLite index from node.jsonl files.

The SQLite DB is a pure derived cache — delete it and rebuild anytime.
Stores: FTS5 over bullet text, backlinks (from [slug] refs), embeddings per node.

Entry points:
    rebuild_all(nodes_dir, db_path)    # full rebuild (e.g. after clone)
    index_node(slug, nodes_dir, db_path)  # incremental (called by watcher)
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Cross-reference pattern: [slug] in bullet text
_CROSSREF_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]")


def _get_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            slug TEXT PRIMARY KEY,
            title TEXT,
            type TEXT,
            created_at TEXT,
            bullet_count INTEGER DEFAULT 0,
            token_budget REAL DEFAULT 0,
            last_reviewed TEXT
        );

        CREATE TABLE IF NOT EXISTS bullets (
            id TEXT PRIMARY KEY,
            node_slug TEXT NOT NULL REFERENCES nodes(slug) ON DELETE CASCADE,
            type TEXT,
            text TEXT,
            status TEXT,
            created_at TEXT,
            useful INTEGER DEFAULT 0,
            harmful INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS bullets_fts USING fts5(
            text,
            node_slug UNINDEXED,
            bullet_id UNINDEXED,
            content=bullets,
            content_rowid=rowid
        );

        CREATE TABLE IF NOT EXISTS backlinks (
            from_slug TEXT NOT NULL,
            to_slug TEXT NOT NULL,
            PRIMARY KEY (from_slug, to_slug)
        );

        CREATE TABLE IF NOT EXISTS embeddings (
            node_slug TEXT PRIMARY KEY,
            vector BLOB,   -- raw float32 bytes
            model TEXT,
            updated_at TEXT
        );

        -- FTS triggers
        CREATE TRIGGER IF NOT EXISTS bullets_ai AFTER INSERT ON bullets BEGIN
            INSERT INTO bullets_fts(rowid, text, node_slug, bullet_id)
            VALUES (new.rowid, new.text, new.node_slug, new.id);
        END;
        CREATE TRIGGER IF NOT EXISTS bullets_ad AFTER DELETE ON bullets BEGIN
            INSERT INTO bullets_fts(bullets_fts, rowid, text, node_slug, bullet_id)
            VALUES ('delete', old.rowid, old.text, old.node_slug, old.id);
        END;
        CREATE TRIGGER IF NOT EXISTS bullets_au AFTER UPDATE ON bullets BEGIN
            INSERT INTO bullets_fts(bullets_fts, rowid, text, node_slug, bullet_id)
            VALUES ('delete', old.rowid, old.text, old.node_slug, old.id);
            INSERT INTO bullets_fts(rowid, text, node_slug, bullet_id)
            VALUES (new.rowid, new.text, new.node_slug, new.id);
        END;
    """)
    conn.commit()
    # Extend schema for file sources (idempotent)
    from kg.file_indexer import ensure_file_schema
    ensure_file_schema(conn)


def index_node(slug: str, *, nodes_dir: Path, db_path: Path) -> None:
    """Re-index a single node: wipe its rows and re-insert from node.jsonl."""
    from kg.reader import FileStore

    store = FileStore(nodes_dir)
    node = store.get(slug)

    conn = _get_conn(db_path)
    _ensure_schema(conn)

    with conn:
        # Wipe existing data for this node (CASCADE deletes bullets too)
        conn.execute("DELETE FROM nodes WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM backlinks WHERE from_slug = ?", (slug,))

        if node is None:
            # Node file deleted — removal is sufficient
            return

        live = node.live_bullets
        conn.execute(
            "INSERT OR REPLACE INTO nodes(slug, title, type, created_at, bullet_count, token_budget, last_reviewed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (node.slug, node.title, node.type, node.created_at, len(live),
             node.token_budget, node.last_reviewed or None),
        )

        for b in live:
            conn.execute(
                "INSERT OR REPLACE INTO bullets(id, node_slug, type, text, status, created_at, useful, harmful, used) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (b.id, node.slug, b.type, b.text, b.status, b.created_at,
                 b.useful, b.harmful, b.used),
            )
            # Extract backlinks from text
            for ref in _CROSSREF_RE.findall(b.text):
                if ref != slug:
                    conn.execute(
                        "INSERT OR IGNORE INTO backlinks(from_slug, to_slug) VALUES (?, ?)",
                        (slug, ref),
                    )


def rebuild_all(nodes_dir: Path, db_path: Path, *, verbose: bool = False) -> int:
    """Full rebuild: drop and recreate SQLite from all node.jsonl files."""
    if db_path.exists():
        db_path.unlink()

    conn = _get_conn(db_path)
    _ensure_schema(conn)
    conn.close()

    from kg.reader import FileStore
    store = FileStore(nodes_dir)
    slugs = store.list_slugs()

    for slug in slugs:
        if verbose:
            print(f"  {slug}")
        index_node(slug, nodes_dir=nodes_dir, db_path=db_path)

    return len(slugs)


def search_fts(query: str, db_path: Path, limit: int = 20) -> list[dict]:
    """FTS5 search over bullet text. Returns list of {slug, bullet_id, text, rank}."""
    if not db_path.exists():
        return []
    conn = _get_conn(db_path)
    rows = conn.execute(
        """SELECT node_slug, bullet_id, text, rank
           FROM bullets_fts
           WHERE bullets_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (query, limit),
    ).fetchall()
    return [
        {"slug": r[0], "bullet_id": r[1], "text": r[2], "rank": r[3]}
        for r in rows
    ]


def get_backlinks(slug: str, db_path: Path) -> list[str]:
    """Return slugs of nodes that link TO this slug."""
    if not db_path.exists():
        return []
    conn = _get_conn(db_path)
    rows = conn.execute(
        "SELECT from_slug FROM backlinks WHERE to_slug = ?", (slug,)
    ).fetchall()
    return [r[0] for r in rows]
