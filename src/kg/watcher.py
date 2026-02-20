"""inotify watcher: watches nodes/ and source dirs, triggers SQLite re-index.

Designed to run as a supervisord-managed daemon:
    python -m kg.watcher CONFIG_ROOT

On IN_CLOSE_WRITE for node.jsonl or meta.jsonl:
    - Re-indexes that node (incremental)

On IN_CLOSE_WRITE for any source file:
    - Re-indexes that file (content-hash checked inside index_file)

Also runs a periodic poll of source dirs every `poll_interval` seconds
as a safety net for missed inotify events.

Falls back to pure polling if inotify is unavailable (macOS, Docker).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kg.config import load_config
from kg.file_indexer import index_file
from kg.file_indexer import index_source as _poll_index_source
from kg.indexer import index_node

if TYPE_CHECKING:
    from kg.config import KGConfig

logger = logging.getLogger("kg.watcher")

_POLL_INTERVAL = 30.0      # seconds between periodic full-source polls
_INOTIFY_TIMEOUT_MS = 5000
_CALIBRATE_INTERVAL = 300.0   # seconds between auto-calibration checks


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _index_node(slug: str, nodes_dir: Path, db_path: Path) -> None:
    try:
        index_node(slug, nodes_dir=nodes_dir, db_path=db_path)
        logger.info("node indexed: %s", slug)
    except Exception:
        logger.exception("failed to index node: %s", slug)


def _index_source_file(path: Path, source_root: Path, source_name: str, db_path: Path, max_size_kb: int) -> None:
    try:
        rel = str(path.relative_to(source_root))
        index_file(path, rel_path=rel, source_name=source_name, db_path=db_path, max_size_kb=max_size_kb)
        logger.info("file indexed: %s", rel)
    except Exception:
        logger.exception("failed to index file: %s", path)


def _slug_from_path(nodes_dir: Path, changed: Path) -> str | None:
    try:
        rel = changed.relative_to(nodes_dir)
        return rel.parts[0]
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# inotify watcher
# ---------------------------------------------------------------------------

def _auto_calibrate_if_stale(db_path: Path, cfg: KGConfig) -> None:
    """Run calibration if ops/bullet threshold exceeded."""
    try:
        from kg.indexer import calibrate, get_calibration_status
        status = get_calibration_status(db_path, cfg)
        threshold = cfg.search.auto_calibrate_threshold
        if status is None:
            calibrate(db_path, cfg)
            logger.info("auto-calibrate: initial calibration done")
        else:
            ops = status["ops_since"]
            bullet_count = max(1, status["bullet_count"])
            if ops / bullet_count >= threshold:
                calibrate(db_path, cfg)
                logger.info("auto-calibrate: recalibrated (ops=%d, bullets=%d)", ops, bullet_count)
    except Exception:
        logger.exception("auto-calibrate failed")


def watch_inotify(nodes_dir: Path, db_path: Path, sources: list[dict] | None = None, cfg: KGConfig | None = None) -> None:
    """Watch using inotify_simple (Linux). Blocks forever.

    sources: list of {path: Path, name: str, max_size_kb: int}
    """
    import inotify_simple  # type: ignore[import]

    inotify = inotify_simple.INotify()
    flags = inotify_simple.flags  # type: ignore[attr-defined]

    # Track watch descriptors → (dir_path, kind, source_meta)
    # kind: "nodes_root", "node_dir", "source_dir"
    watched: dict[int, tuple[Path, str, dict]] = {}

    # Watch nodes root
    wd = inotify.add_watch(str(nodes_dir), flags.CREATE | flags.MOVED_TO)
    watched[wd] = (nodes_dir, "nodes_root", {})

    # Watch existing node dirs
    for node_dir in nodes_dir.iterdir():
        if node_dir.is_dir():
            wd = inotify.add_watch(str(node_dir), flags.CLOSE_WRITE | flags.MOVED_TO)
            watched[wd] = (node_dir, "node_dir", {})

    # Watch source dirs (recursively via rglob subdirs)
    for src in (sources or []):
        src_path: Path = src["path"]
        if src_path.exists():
            wd = inotify.add_watch(str(src_path), flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE)
            watched[wd] = (src_path, "source_dir", src)
            # Watch subdirs too
            for sub in src_path.rglob("*"):
                if sub.is_dir():
                    try:
                        wd = inotify.add_watch(str(sub), flags.CLOSE_WRITE | flags.MOVED_TO)
                        watched[wd] = (sub, "source_dir", src)
                    except OSError:
                        pass

    logger.info("inotify watching nodes=%s, sources=%d", nodes_dir, len(sources or []))

    last_poll = time.monotonic()
    last_calibrate = time.monotonic()

    while True:
        for event in inotify.read(timeout=_INOTIFY_TIMEOUT_MS):
            path_name = event.name
            if not path_name:
                continue

            wd = event.wd
            if wd not in watched:
                continue

            dir_path, kind, meta = watched[wd]

            if kind == "nodes_root":
                # New subdirectory created
                if event.mask & flags.CREATE:
                    new_dir = nodes_dir / path_name
                    if new_dir.is_dir():
                        new_wd = inotify.add_watch(str(new_dir), flags.CLOSE_WRITE | flags.MOVED_TO)
                        watched[new_wd] = (new_dir, "node_dir", {})

            elif kind == "node_dir":
                if path_name.endswith(".jsonl"):
                    changed = dir_path / path_name
                    slug = _slug_from_path(nodes_dir, changed)
                    if slug:
                        _index_node(slug, nodes_dir, db_path)

            elif kind == "source_dir":
                changed = dir_path / path_name
                if changed.is_dir():
                    # New subdirectory — start watching it
                    try:
                        new_wd = inotify.add_watch(str(changed), flags.CLOSE_WRITE | flags.MOVED_TO)
                        watched[new_wd] = (changed, "source_dir", meta)
                    except OSError:
                        pass
                elif changed.is_file():
                    _index_source_file(
                        changed,
                        source_root=meta["path"],
                        source_name=meta.get("name", ""),
                        db_path=db_path,
                        max_size_kb=meta.get("max_size_kb", 512),
                    )

        # Periodic full poll of sources (catch missed events / deletions)
        now = time.monotonic()
        if now - last_poll >= _POLL_INTERVAL and sources:
            _poll_sources(sources, db_path)
            last_poll = now

        now_cal = time.monotonic()
        if now_cal - last_calibrate >= _CALIBRATE_INTERVAL and cfg is not None:
            _auto_calibrate_if_stale(db_path, cfg)
            last_calibrate = now_cal


# ---------------------------------------------------------------------------
# Polling fallback
# ---------------------------------------------------------------------------

def _poll_sources(sources: list[dict], db_path: Path) -> None:
    for src in sources:
        try:
            cfg_src = src.get("config")
            if cfg_src is not None:
                _poll_index_source(cfg_src, db_path=db_path)
        except Exception:
            logger.exception("poll failed for source: %s", src.get("name"))


def watch_poll(
    nodes_dir: Path,
    db_path: Path,
    sources: list[dict] | None = None,
    interval: float = 1.0,
    cfg: KGConfig | None = None,
) -> None:
    """Polling fallback for macOS/Docker. Checks mtime every interval seconds."""
    seen_nodes: dict[Path, float] = {}
    seen_files: dict[Path, float] = {}
    logger.info("polling nodes=%s interval=%.1fs", nodes_dir, interval)
    last_source_poll = time.monotonic()
    last_calibrate = time.monotonic()

    while True:
        # Poll nodes/
        for node_dir in nodes_dir.iterdir():
            if not node_dir.is_dir():
                continue
            for fname in ("node.jsonl", "meta.jsonl"):
                f = node_dir / fname
                if not f.exists():
                    continue
                mtime = f.stat().st_mtime
                if seen_nodes.get(f, 0.0) < mtime:
                    seen_nodes[f] = mtime
                    slug = _slug_from_path(nodes_dir, f)
                    if slug:
                        _index_node(slug, nodes_dir, db_path)

        # Poll source files periodically
        now = time.monotonic()
        if now - last_source_poll >= _POLL_INTERVAL and sources:
            for src in (sources or []):
                src_path: Path = src["path"]
                if not src_path.exists():
                    continue
                for f in src_path.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        mtime = f.stat().st_mtime
                    except OSError:
                        continue
                    if seen_files.get(f, 0.0) < mtime:
                        seen_files[f] = mtime
                        _index_source_file(
                            f,
                            source_root=src_path,
                            source_name=src.get("name", ""),
                            db_path=db_path,
                            max_size_kb=src.get("max_size_kb", 512),
                        )
            last_source_poll = now

        now_cal = time.monotonic()
        if now_cal - last_calibrate >= _CALIBRATE_INTERVAL and cfg is not None:
            _auto_calibrate_if_stale(db_path, cfg)
            last_calibrate = now_cal

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(nodes_dir: Path, db_path: Path, sources: list[dict] | None = None, cfg: KGConfig | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        watch_inotify(nodes_dir, db_path, sources, cfg=cfg)
    except ImportError:
        logger.warning("inotify_simple not available, falling back to polling")
        watch_poll(nodes_dir, db_path, sources, cfg=cfg)


def run_from_config(config_root: Path | None = None) -> None:
    """Load kg.toml and start the watcher."""
    cfg = load_config(config_root)
    nodes_dir, db_path, sources = cfg.nodes_dir, cfg.db_path, [
        {
            "path": src.abs_path,
            "name": src.name,
            "max_size_kb": src.max_size_kb,
            "config": src,
        }
        for src in cfg.sources
    ]
    run(nodes_dir, db_path, sources, cfg=cfg)


if __name__ == "__main__":
    # Accept optional config root as argument
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    run_from_config(root)
