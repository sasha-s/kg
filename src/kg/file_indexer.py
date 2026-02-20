"""File source indexer: chunk files → SQLite (FTS, backlinks).

No LLM extraction — pure FTS indexing.

Schema additions (in indexer._ensure_schema):
    file_sources(path, content_hash, slug, indexed_at)

File nodes use:
    nodes.slug  = "_doc-<sha256[:12]>"    (stable: based on relative path)
    nodes.type  = "doc"
    bullets.type = "chunk"
    bullets.text = chunk content

This reuses the existing FTS5 index on bullets.text, so `kg search` and
`kg context` naturally search both curated nodes and file chunks.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from kg._vendor.fastcdc import fastcdc_py  # Cython-accelerated if built, else pure Python

if TYPE_CHECKING:
    from kg.config import SourceConfig

# Chunk size parameters (bytes; ~4 chars/token)
_CHUNK_MIN = 512    # ~128 tokens min
_CHUNK_AVG = 1500   # ~375 tokens target
_CHUNK_MAX = 4000   # ~1K tokens max

# After CDC split, drop chunks shorter than this (likely stubs)
_CHUNK_MIN_CHARS = 64

# Binary detection: if >30% of first 512 bytes are non-printable, skip
_BINARY_THRESHOLD = 0.30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _path_slug(rel_path: str) -> str:
    """Stable slug from relative path: _doc-<sha256[:12]>."""
    h = hashlib.sha256(rel_path.encode()).hexdigest()[:12]
    return f"_doc-{h}"


def _chunk_id(slug: str, idx: int) -> str:
    return f"{slug}-c{idx:04d}"


def _is_binary(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:512]
    except OSError:
        return True
    non_printable = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    return len(sample) > 0 and non_printable / len(sample) > _BINARY_THRESHOLD


def _fastcdc_chunks(text: str) -> list[str]:
    """Split text using content-defined chunking, snapping splits to line boundaries.

    Uses fastcdc rolling hash for stable chunk boundaries that survive local edits.
    Split points are snapped to the nearest newline for cleaner chunks.
    """
    if not text:
        return []

    data = text.encode("utf-8")

    # Short documents: return as single chunk
    if len(data) <= _CHUNK_MIN:
        return [text] if text.strip() else []

    # CDC split points
    cdc_chunks = fastcdc_py(data, min_size=_CHUNK_MIN, avg_size=_CHUNK_AVG, max_size=_CHUNK_MAX)

    # Collect raw split points
    split_points = [0]
    for c in cdc_chunks:
        split_points.append(c.offset + c.length)
    if split_points[-1] != len(data):
        split_points[-1] = len(data)

    # Snap internal split points to nearest newline
    for i in range(1, len(split_points) - 1):
        pos = split_points[i]
        split_points[i] = _snap_to_newline(data, pos)

    # Deduplicate (snapping may merge adjacent points)
    split_points = sorted(set(split_points))

    # Extract text chunks
    result: list[str] = []
    for i in range(len(split_points) - 1):
        chunk_bytes = data[split_points[i] : split_points[i + 1]]
        try:
            chunk_text = chunk_bytes.decode("utf-8")
        except UnicodeDecodeError:
            chunk_text = chunk_bytes.decode("utf-8", errors="replace")
        chunk_text = chunk_text.strip()
        if len(chunk_text) >= _CHUNK_MIN_CHARS:
            result.append(chunk_text)

    return result or ([text.strip()] if text.strip() else [])


def _snap_to_newline(data: bytes, pos: int, window: int = 256) -> int:
    """Snap byte position to nearest newline within window, or return pos."""
    if pos <= 0 or pos >= len(data):
        return pos
    if data[pos - 1] == 0x0A:
        return pos  # already at line boundary

    # Search backward
    bwd = None
    for j in range(pos - 1, max(0, pos - window) - 1, -1):
        if data[j] == 0x0A:
            bwd = j + 1
            break

    # Search forward
    fwd = None
    for j in range(pos, min(len(data), pos + window)):
        if data[j] == 0x0A:
            fwd = j + 1
            break

    if fwd is not None and bwd is not None:
        return fwd if (fwd - pos) <= (pos - bwd) else bwd
    if fwd is not None:
        return fwd
    if bwd is not None:
        return bwd
    return pos


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _git_files(source_path: Path) -> list[Path] | None:
    """Return git-tracked files in source_path. Returns None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=source_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return [source_path / p for p in result.stdout.splitlines() if p]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _glob_files(source_path: Path, include: list[str], exclude: list[str]) -> list[Path]:
    """Glob-based file discovery respecting include/exclude patterns."""
    def _excluded(rel: str) -> bool:
        return any(fnmatch(rel, pat.lstrip("/")) for pat in exclude)

    files: list[Path] = []
    for pattern in include:
        for p in source_path.rglob(pattern.lstrip("*").lstrip("/")):
            if p.is_file():
                rel = str(p.relative_to(source_path))
                if not _excluded(rel):
                    files.append(p)

    # Deduplicate
    return list(dict.fromkeys(files))


def collect_files(source: SourceConfig) -> list[Path]:
    """Return all indexable files for a source config."""
    source_path = source.abs_path
    if not source_path.exists():
        return []

    if source.use_git:
        git_files = _git_files(source_path)
        if git_files is not None:
            include_pats = source.include
            exclude_pats = source.exclude
            result: list[Path] = []
            for p in git_files:
                if not p.is_file():
                    continue
                rel = str(p.relative_to(source_path))
                if any(fnmatch(rel, pat) or fnmatch(p.name, pat.split("/")[-1])
                       for pat in include_pats):
                    if not any(fnmatch(rel, pat.lstrip("/")) for pat in exclude_pats):
                        result.append(p)
            return result

    return _glob_files(source_path, source.include, source.exclude)


# ---------------------------------------------------------------------------
# SQLite schema extension (called from indexer._ensure_schema)
# ---------------------------------------------------------------------------

def ensure_file_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS file_sources (
            path        TEXT PRIMARY KEY,   -- absolute path
            rel_path    TEXT NOT NULL,      -- relative to source root
            content_hash TEXT NOT NULL,
            slug        TEXT NOT NULL,      -- _doc-<hash>
            source_name TEXT DEFAULT '',
            indexed_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS file_sources_slug ON file_sources(slug);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Index / delete single file
# ---------------------------------------------------------------------------

def index_file(
    path: Path,
    *,
    rel_path: str,
    source_name: str,
    db_path: Path,
    max_size_kb: int = 512,
) -> str | None:
    """Index a single file into SQLite. Returns slug or None if skipped."""
    if not path.exists() or not path.is_file():
        return None
    if path.stat().st_size > max_size_kb * 1024:
        return None
    if _is_binary(path):
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    content_hash = _content_hash(text)
    slug = _path_slug(rel_path)
    now = datetime.now(UTC).isoformat()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_file_schema(conn)

    with conn:
        # Check if unchanged
        row = conn.execute(
            "SELECT content_hash FROM file_sources WHERE path = ?", (str(path),)
        ).fetchone()
        if row and row[0] == content_hash:
            return slug  # unchanged

        # Wipe old data
        conn.execute("DELETE FROM nodes WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM file_sources WHERE path = ?", (str(path),))

        title = rel_path
        conn.execute(
            "INSERT INTO nodes(slug, title, type, created_at, bullet_count) VALUES (?, ?, ?, ?, ?)",
            (slug, title, "doc", now, 0),
        )

        chunks = _fastcdc_chunks(text)
        for idx, chunk in enumerate(chunks):
            cid = _chunk_id(slug, idx)
            conn.execute(
                "INSERT OR REPLACE INTO bullets(id, node_slug, type, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cid, slug, "chunk", chunk, now),
            )

        conn.execute(
            "UPDATE nodes SET bullet_count = ? WHERE slug = ?",
            (len(chunks), slug),
        )

        conn.execute(
            "INSERT OR REPLACE INTO file_sources(path, rel_path, content_hash, slug, source_name, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(path), rel_path, content_hash, slug, source_name, now),
        )

    conn.close()
    return slug


def delete_file_index(path: Path, db_path: Path) -> bool:
    """Remove a file's index entries. Returns True if anything was deleted."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        row = conn.execute(
            "SELECT slug FROM file_sources WHERE path = ?", (str(path),)
        ).fetchone()
        if not row:
            conn.close()
            return False
        slug = row[0]
        conn.execute("DELETE FROM nodes WHERE slug = ?", (slug,))
        conn.execute("DELETE FROM file_sources WHERE path = ?", (str(path),))
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Index a whole source config
# ---------------------------------------------------------------------------

def index_source(
    source: SourceConfig,
    *,
    db_path: Path,
    verbose: bool = False,
) -> dict[str, int]:
    """Index all files in a source. Returns stats: new, updated, unchanged, skipped, deleted."""
    files = collect_files(source)
    source_path = source.abs_path
    stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0}

    if not db_path.exists():
        return stats

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_file_schema(conn)

    # Detect deleted files
    current_abs = {str(p) for p in files}
    source_prefix = str(source_path.resolve()).rstrip("/") + "/"
    orphans = conn.execute(
        "SELECT path, slug FROM file_sources WHERE path LIKE ?",
        (source_prefix + "%",),
    ).fetchall()
    conn.close()

    for orphan_path, _orphan_slug in orphans:
        if orphan_path not in current_abs:
            delete_file_index(Path(orphan_path), db_path)
            stats["deleted"] += 1
            if verbose:
                print(f"  deleted: {orphan_path}")

    # Index current files
    for p in files:
        try:
            rel = str(p.relative_to(source_path))
        except ValueError:
            rel = p.name

        # Check if already in DB with same hash (pre-check without locking)
        conn2 = sqlite3.connect(str(db_path))
        ensure_file_schema(conn2)
        row = conn2.execute(
            "SELECT content_hash FROM file_sources WHERE path = ?", (str(p),)
        ).fetchone()
        conn2.close()

        if p.stat().st_size > source.max_size_kb * 1024 or _is_binary(p):
            stats["skipped"] += 1
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats["skipped"] += 1
            continue

        new_hash = _content_hash(text)
        if row and row[0] == new_hash:
            stats["unchanged"] += 1
            continue

        was_new = row is None
        result = index_file(
            p,
            rel_path=rel,
            source_name=source.name,
            db_path=db_path,
            max_size_kb=source.max_size_kb,
        )
        if result:
            if was_new:
                stats["new"] += 1
            else:
                stats["updated"] += 1
            if verbose:
                action = "new" if was_new else "updated"
                print(f"  {action}: {rel}")
        else:
            stats["skipped"] += 1

    return stats
