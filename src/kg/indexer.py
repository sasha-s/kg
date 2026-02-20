"""Build/rebuild the SQLite index from node.jsonl files.

The SQLite DB is a pure derived cache — delete it and rebuild anytime.
Stores: FTS5 over bullet text, backlinks (from [slug] refs), embeddings per node.

Entry points:
    rebuild_all(nodes_dir, db_path)    # full rebuild (e.g. after clone)
    index_node(slug, nodes_dir, db_path)  # incremental (called by watcher)
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from datetime import UTC
from typing import TYPE_CHECKING

from kg.file_indexer import ensure_file_schema
from kg.reader import FileStore

if TYPE_CHECKING:
    from pathlib import Path

    from kg.config import KGConfig
    from kg.models import FileNode

# Cross-reference pattern: [slug] in bullet text
_CROSSREF_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]")


def _get_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Detect 0-byte / corrupt DB before connecting — gives a clear error instead
    # of an opaque "disk I/O error" on PRAGMA.  Happens on virtiofs (OrbStack/Docker)
    # when WAL files get desynchronised; fix: kg stop && rm .kg/index/graph.db* && kg reindex && kg start
    if db_path.exists() and db_path.stat().st_size == 0:
        msg = (
            f"SQLite DB is empty (0 bytes): {db_path}\n"
            f"Fix: kg stop && rm {db_path}* && kg reindex && kg start"
        )
        raise sqlite3.OperationalError(msg)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise sqlite3.OperationalError(
            f"Failed to open DB {db_path} — may be corrupt (virtiofs/NFS issue).\n"
            f"Fix: kg stop && rm {db_path}* && kg reindex && kg start\n"
            f"Original error: {exc}"
        ) from exc
    return conn


def _get_conn_readonly(db_path: Path) -> sqlite3.Connection:
    """Open db read-only — no write lock, safe to call while watcher is running."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _conn_for(cfg: KGConfig | None, db_path: Path) -> sqlite3.Connection:
    """Return a DB connection: Turso if cfg.use_turso, else local SQLite."""
    if cfg is not None and cfg.use_turso:
        from kg.db import get_conn
        return get_conn(cfg)
    return _get_conn(db_path)


def _migrate_fts_if_needed(conn: sqlite3.Connection) -> None:
    """Drop bullets_fts if it was created with content=bullets (broken schema).

    FTS5 UNINDEXED columns in a content= table are read back from the content table by
    column name at query time.  Our FTS column is named 'bullet_id' but the content table
    (bullets) uses 'id', causing OperationalError: no such column: T.bullet_id.
    Fix: switch to a self-contained FTS5 table (no content=) so FTS stores its own copy.
    The table will be recreated by _ensure_schema and repopulated by the next kg rebuild.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='bullets_fts'"
    ).fetchone()
    if row and "content=bullets" in row[0]:
        # Drop old FTS table and its triggers, then recreate below
        conn.executescript("""
            DROP TRIGGER IF EXISTS bullets_ai;
            DROP TRIGGER IF EXISTS bullets_ad;
            DROP TRIGGER IF EXISTS bullets_au;
            DROP TABLE IF EXISTS bullets_fts;
        """)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Migrate legacy FTS table before running CREATE IF NOT EXISTS
    with contextlib.suppress(sqlite3.OperationalError):  # bullets_fts_config may not exist yet
        _migrate_fts_if_needed(conn)

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
            used INTEGER DEFAULT 0,
            num_voted INTEGER DEFAULT 0
        );

        -- Self-contained FTS5 table (no content= link): stores its own copy of text,
        -- node_slug, bullet_id so UNINDEXED retrieval doesn't depend on content table
        -- column names matching.
        CREATE VIRTUAL TABLE IF NOT EXISTS bullets_fts USING fts5(
            text,
            node_slug UNINDEXED,
            bullet_id UNINDEXED
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

        CREATE TABLE IF NOT EXISTS calibration (
            key TEXT PRIMARY KEY,
            breaks TEXT,        -- JSON float array (percentile breakpoints)
            bullet_count INTEGER,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS calibration_ops (
            id INTEGER PRIMARY KEY DEFAULT 1,
            ops_count INTEGER DEFAULT 0
        );
        INSERT OR IGNORE INTO calibration_ops(id, ops_count) VALUES (1, 0);

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
    # Migrate: add num_voted column if missing (idempotent)
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE bullets ADD COLUMN num_voted INTEGER DEFAULT 0")
        conn.commit()
    # Extend schema for file sources (idempotent)
    ensure_file_schema(conn)


def _embed_node(slug: str, node: FileNode, cfg: KGConfig, conn: sqlite3.Connection) -> None:
    """Embed node text and store in embeddings table + notify vector server."""
    from datetime import datetime

    try:
        import json as _json
        import urllib.request as _urllib

        from kg.vector_client import embed
    except ImportError:
        return

    live = node.live_bullets
    text = node.title + "\n" + "\n".join(b.text for b in live)
    if not text.strip():
        return

    try:
        vectors = embed([text], cfg, task_type="doc")
        if not vectors:
            return
        vector = vectors[0]
        # Store in embeddings table
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(node_slug, vector, model, updated_at) VALUES (?, ?, ?, ?)",
            (slug, vector.tobytes(), cfg.embeddings.model, datetime.now(UTC).isoformat()),
        )
        # Notify vector server (non-blocking, best-effort)
        with contextlib.suppress(Exception):
            data = _json.dumps({"id": slug, "vector": vector.tolist()}).encode()
            req = _urllib.Request(
                f"http://127.0.0.1:{cfg.server.vector_port}/add",
                data=data,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            _urllib.urlopen(req, timeout=1)  # noqa: S310
    except Exception as exc:
        import sys
        print(f"kg: WARNING: embedding failed for [{slug}]: {exc}", file=sys.stderr, flush=True)


def index_node(slug: str, *, nodes_dir: Path, db_path: Path, cfg: KGConfig | None = None) -> None:
    """Re-index a single node: wipe its rows and re-insert from node.jsonl."""
    store = FileStore(nodes_dir)
    node = store.get(slug)

    conn = _conn_for(cfg, db_path)
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
                "INSERT OR REPLACE INTO bullets(id, node_slug, type, text, status, created_at, useful, harmful, used, num_voted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (b.id, node.slug, b.type, b.text, b.status, b.created_at,
                 b.useful, b.harmful, b.used, b.useful + b.harmful),
            )
            # Extract backlinks from text
            for ref in _CROSSREF_RE.findall(b.text):
                if ref != slug:
                    conn.execute(
                        "INSERT OR IGNORE INTO backlinks(from_slug, to_slug) VALUES (?, ?)",
                        (slug, ref),
                    )

        # Generate and store embedding if cfg provided
        if cfg is not None:
            _embed_node(slug, node, cfg, conn)

    # Increment calibration ops counter (best-effort)
    with contextlib.suppress(Exception):
        conn.execute("UPDATE calibration_ops SET ops_count = ops_count + 1 WHERE id = 1")
        conn.commit()


def rebuild_all(nodes_dir: Path, db_path: Path, *, verbose: bool = False, cfg: KGConfig | None = None) -> int:
    """Full rebuild: drop and recreate SQLite from all node.jsonl files."""
    if cfg is not None and cfg.use_turso:
        # Turso: truncate tables instead of deleting the file
        conn = _conn_for(cfg, db_path)
        # Drop all tables and recreate — cleaner than trying to truncate with triggers
        conn.executescript("""
            DROP TRIGGER IF EXISTS bullets_ai;
            DROP TRIGGER IF EXISTS bullets_ad;
            DROP TRIGGER IF EXISTS bullets_au;
            DROP TABLE IF EXISTS bullets_fts;
            DROP TABLE IF EXISTS embeddings;
            DROP TABLE IF EXISTS backlinks;
            DROP TABLE IF EXISTS bullets;
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS file_sources;
            DROP INDEX IF EXISTS file_sources_slug;
        """)
        _ensure_schema(conn)
        conn.close()
    else:
        if db_path.exists():
            db_path.unlink()
        conn = _get_conn(db_path)
        _ensure_schema(conn)
        conn.close()

    store = FileStore(nodes_dir)
    slugs = store.list_slugs()

    for slug in slugs:
        if verbose:
            print(f"  {slug}")
        index_node(slug, nodes_dir=nodes_dir, db_path=db_path, cfg=cfg)

    return len(slugs)


_STOPWORDS = frozenset({
    "a", "about", "above", "after", "again", "against", "ain", "all", "am", "an",
    "and", "any", "are", "aren", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "couldn", "d", "did",
    "didn", "do", "does", "doesn", "doing", "don", "down", "during", "each", "few",
    "for", "from", "further", "had", "hadn", "has", "hasn", "have", "haven", "having",
    "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i",
    "if", "in", "into", "is", "isn", "it", "its", "itself", "just", "ll", "m", "ma",
    "me", "mightn", "more", "most", "mustn", "my", "myself", "needn", "no", "nor",
    "not", "now", "o", "of", "off", "on", "once", "only", "or", "other", "our",
    "ours", "ourselves", "out", "over", "own", "re", "s", "same", "shan", "she",
    "should", "shouldn", "so", "some", "such", "t", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "until", "up", "ve", "very", "was", "wasn", "we",
    "were", "weren", "what", "when", "where", "which", "while", "who", "whom", "why",
    "will", "with", "won", "wouldn", "y", "you", "your", "yours", "yourself", "yourselves",
})


def _build_fts_query(query: str) -> str | None:
    """Split query into OR-joined prefix terms, filtering stopwords. Returns None if empty."""
    terms = [w for w in re.split(r"[\s\W]+", query.lower()) if w and w not in _STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f"{t}*" for t in terms)


def search_fts(query: str, db_path: Path, limit: int = 20, cfg: KGConfig | None = None) -> list[dict]:
    """FTS5 search over bullet text. Returns list of {slug, bullet_id, text, rank}.

    Uses OR + prefix matching (same as mg): 'how to add bullets' →
    'add* OR bullets*' so partial matches work across multiple bullets.
    """
    if cfg is None and not db_path.exists():
        return []
    fts_query = _build_fts_query(query)
    if fts_query is None:
        return []
    conn = _conn_for(cfg, db_path)
    _ensure_schema(conn)  # migrate FTS schema if needed (idempotent)
    rows = conn.execute(
        """SELECT node_slug, bullet_id, text, bm25(bullets_fts) as rank
           FROM bullets_fts
           WHERE bullets_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (fts_query, limit),
    ).fetchall()
    return [
        {"slug": r[0], "bullet_id": r[1], "text": r[2], "rank": r[3]}
        for r in rows
    ]


def get_backlinks(slug: str, db_path: Path, cfg: KGConfig | None = None) -> list[str]:
    """Return slugs of nodes that link TO this slug."""
    conn = _conn_for(cfg, db_path)
    if cfg is None and not db_path.exists():
        return []
    rows = conn.execute(
        "SELECT from_slug FROM backlinks WHERE to_slug = ?", (slug,)
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Calibration helpers
# ---------------------------------------------------------------------------


def score_to_quantile(score: float, breaks: list[float]) -> float:
    """Map a raw score to its quantile (0-1) using sorted percentile breakpoints."""
    if not breaks or score <= breaks[0]:
        return 0.0
    if score >= breaks[-1]:
        return 1.0
    step = 1.0 / (len(breaks) - 1)
    for i, b in enumerate(breaks):
        if score < b:
            lower = breaks[i - 1]
            frac = (score - lower) / (b - lower) if b > lower else 0.0
            return (i - 1) * step + frac * step
    return 1.0


def get_calibration(key: str, db_path: Path, cfg: KGConfig | None = None) -> tuple[int, list[float]] | None:
    """Return (bullet_count, breaks) for the given calibration key, or None."""
    if cfg is None and not db_path.exists():
        return None
    conn = _conn_for(cfg, db_path)
    row = conn.execute(
        "SELECT bullet_count, breaks FROM calibration WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    return (row[0], json.loads(row[1]))


def get_calibration_status(db_path: Path, cfg: KGConfig | None = None) -> dict | None:
    """Return calibration info for status display, or None if never calibrated.

    Uses a read-only connection — safe to call while watcher is running.
    """
    if not db_path.exists():
        return None
    if cfg is not None and cfg.use_turso:
        conn = _conn_for(cfg, db_path)
    else:
        conn = _get_conn_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT bullet_count, updated_at FROM calibration WHERE key = 'fts'"
        ).fetchone()
        if row is None:
            return None
        ops_row = conn.execute("SELECT ops_count FROM calibration_ops WHERE id = 1").fetchone()
        current = conn.execute("SELECT COUNT(*) FROM bullets").fetchone()[0]
        return {
            "bullet_count": row[0],
            "updated_at": row[1],
            "ops_since": ops_row[0] if ops_row else 0,
            "current_bullets": current,
        }
    except sqlite3.OperationalError:
        return None


def calibrate(db_path: Path, cfg: KGConfig | None = None, sample_size: int = 200) -> dict:
    """Calibrate FTS and vector score quantiles by sampling the index.

    Samples random bullets, runs FTS and vector searches, computes percentile
    breakpoints for score normalization. Saves results to the calibration table.
    """
    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy required for calibration (pip install numpy)"}

    conn = _conn_for(cfg, db_path)
    _ensure_schema(conn)

    rows = conn.execute(
        "SELECT text FROM bullets ORDER BY RANDOM() LIMIT ?", (sample_size,)
    ).fetchall()
    if not rows:
        return {"bullets_sampled": 0, "warning": "no bullets found"}

    # Collect FTS scores
    fts_scores: list[float] = []
    for (text,) in rows:
        fts_query = _build_fts_query(text[:100])
        if not fts_query:
            continue
        hits = conn.execute(
            "SELECT bm25(bullets_fts) FROM bullets_fts WHERE bullets_fts MATCH ? LIMIT 20",
            (fts_query,),
        ).fetchall()
        for (score,) in hits:
            if score is not None:
                fts_scores.append(-score)   # negate: higher = better

    # Collect vector scores
    vec_scores: list[float] = []
    if cfg is not None:
        with contextlib.suppress(Exception):
            from kg.vector_client import search_vector
            slug_rows = conn.execute(
                "SELECT node_slug FROM embeddings ORDER BY RANDOM() LIMIT ?",
                (min(50, sample_size),),
            ).fetchall()
            for (slug,) in slug_rows:
                title_row = conn.execute(
                    "SELECT title FROM nodes WHERE slug = ?", (slug,)
                ).fetchone()
                if title_row:
                    for _, score in search_vector(title_row[0], cfg, k=20):
                        vec_scores.append(float(score))

    n_breaks = 20

    def _percentile_breaks(scores: list[float]) -> list[float] | None:
        if len(scores) < n_breaks:
            return None
        arr = np.array(sorted(scores), dtype=np.float64)
        return [float(np.percentile(arr, 100.0 * i / (n_breaks - 1))) for i in range(n_breaks)]

    from datetime import datetime
    bullet_count = conn.execute("SELECT COUNT(*) FROM bullets").fetchone()[0]
    now = datetime.now(UTC).isoformat()

    fts_breaks = _percentile_breaks(fts_scores)
    vec_breaks = _percentile_breaks(vec_scores)

    with conn:
        if fts_breaks:
            conn.execute(
                "INSERT OR REPLACE INTO calibration(key, breaks, bullet_count, updated_at)"
                " VALUES (?, ?, ?, ?)",
                ("fts", json.dumps(fts_breaks), bullet_count, now),
            )
        if vec_breaks:
            conn.execute(
                "INSERT OR REPLACE INTO calibration(key, breaks, bullet_count, updated_at)"
                " VALUES (?, ?, ?, ?)",
                ("vector", json.dumps(vec_breaks), bullet_count, now),
            )
        conn.execute("INSERT OR REPLACE INTO calibration_ops(id, ops_count) VALUES (1, 0)")

    return {
        "bullets_sampled": len(rows),
        "fts_scores": len(fts_scores),
        "fts_calibrated": fts_breaks is not None,
        "vec_scores": len(vec_scores),
        "vec_calibrated": vec_breaks is not None,
        "bullet_count": bullet_count,
    }
