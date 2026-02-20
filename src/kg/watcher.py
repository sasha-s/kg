"""inotify watcher: watches nodes/ directory and triggers SQLite re-index on changes.

Designed to run as a supervisord-managed daemon:
    python -m kg.watcher /path/to/nodes /path/to/.mg-index/graph.db

On IN_CLOSE_WRITE for any node.jsonl or meta.jsonl:
    - Extracts the slug from the path
    - Re-indexes just that node into SQLite (incremental, not full rebuild)

Falls back to polling if inotify is unavailable (macOS, Docker without inotify).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("mg.file-watcher")


def _slug_from_path(nodes_dir: Path, changed: Path) -> str | None:
    """Extract slug from a changed file path.

    nodes/asyncpg-patterns/node.jsonl  → "asyncpg-patterns"
    nodes/asyncpg-patterns/meta.jsonl  → "asyncpg-patterns"
    """
    try:
        rel = changed.relative_to(nodes_dir)
        return rel.parts[0]
    except (ValueError, IndexError):
        return None


def _index_node(slug: str, nodes_dir: Path, db_path: Path) -> None:
    """Re-index a single node into SQLite."""
    from kg.indexer import index_node
    try:
        index_node(slug, nodes_dir=nodes_dir, db_path=db_path)
        logger.info("indexed: %s", slug)
    except Exception:
        logger.exception("failed to index: %s", slug)


def watch_inotify(nodes_dir: Path, db_path: Path) -> None:
    """Watch using inotify_simple (Linux). Blocks forever."""
    import inotify_simple  # type: ignore[import]

    inotify = inotify_simple.INotify()
    flags = inotify_simple.flags  # type: ignore[attr-defined]

    # Watch the nodes root — new subdirs appear here
    inotify.add_watch(str(nodes_dir), flags.CREATE | flags.MOVED_TO)

    # Watch all existing node dirs
    watched: dict[int, Path] = {}
    for node_dir in nodes_dir.iterdir():
        if node_dir.is_dir():
            wd = inotify.add_watch(str(node_dir), flags.CLOSE_WRITE | flags.MOVED_TO)
            watched[wd] = node_dir

    logger.info("inotify watching %s", nodes_dir)

    while True:
        for event in inotify.read(timeout=5000):
            path_name = event.name  # filename only
            if not path_name:
                continue

            # New subdirectory created under nodes/ — start watching it
            if event.mask & flags.CREATE and not path_name.endswith(".jsonl"):
                new_dir = nodes_dir / path_name
                if new_dir.is_dir():
                    wd = inotify.add_watch(str(new_dir), flags.CLOSE_WRITE | flags.MOVED_TO)
                    watched[wd] = new_dir
                    logger.debug("watching new dir: %s", new_dir)
                continue

            if not path_name.endswith(".jsonl"):
                continue

            # Find which dir this belongs to
            wd = event.wd
            if wd in watched:
                changed_path = watched[wd] / path_name
                slug = _slug_from_path(nodes_dir, changed_path)
                if slug:
                    _index_node(slug, nodes_dir, db_path)


def watch_poll(nodes_dir: Path, db_path: Path, interval: float = 1.0) -> None:
    """Polling fallback for macOS/Docker. Checks mtime every interval seconds."""
    seen: dict[Path, float] = {}
    logger.info("polling %s every %.1fs", nodes_dir, interval)

    while True:
        for node_dir in nodes_dir.iterdir():
            if not node_dir.is_dir():
                continue
            for fname in ("node.jsonl", "meta.jsonl"):
                f = node_dir / fname
                if not f.exists():
                    continue
                mtime = f.stat().st_mtime
                if seen.get(f, 0.0) < mtime:
                    seen[f] = mtime
                    slug = _slug_from_path(nodes_dir, f)
                    if slug:
                        _index_node(slug, nodes_dir, db_path)
        time.sleep(interval)


def run(nodes_dir: Path, db_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        watch_inotify(nodes_dir, db_path)
    except ImportError:
        logger.warning("inotify_simple not available, falling back to polling")
        watch_poll(nodes_dir, db_path)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python -m kg.watcher NODES_DIR DB_PATH", file=sys.stderr)  # noqa: T201
        sys.exit(1)
    run(Path(sys.argv[1]), Path(sys.argv[2]))
