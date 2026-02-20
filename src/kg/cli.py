"""kg CLI — knowledge graph backed by JSONL files and SQLite index.

Commands:
    kg init [NAME]             create kg.toml + .kg/ dirs
    kg reindex                 rebuild SQLite from all node.jsonl files
    kg upgrade                 reindex + apply any schema migrations
    kg add SLUG TEXT           add a bullet to a node
    kg show SLUG               dump a node's bullets
    kg search QUERY            FTS5 search
    kg context QUERY           packed context for LLM injection
    kg serve                   start stdio MCP server
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import click

from kg.bootstrap import bootstrap_patterns
from kg.config import KGConfig, SourceConfig, init_config, load_config
from kg.context import build_context
from kg.daemon import ensure_watcher, stop_watcher, watcher_status
from kg.file_indexer import collect_files, index_source
from kg.indexer import index_node, rebuild_all, search_fts
from kg.install import ensure_hook_installed, ensure_mcp_registered, hook_status, mcp_health
from kg.mcp import run_server
from kg.reader import FileStore
from kg.watcher import run_from_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cfg() -> KGConfig:
    try:
        return load_config()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="kg")
def cli() -> None:
    """kg — lightweight knowledge graph."""


# ---------------------------------------------------------------------------
# kg init
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("name", required=False)
@click.option("--dir", "root", default=".", show_default=True, help="Project root")
def init(name: str | None, root: str) -> None:
    """Create kg.toml and .kg/ directories in the current project."""
    root_path = Path(root).resolve()
    try:
        config_path = init_config(root_path, name=name)
        click.echo(f"Created {config_path}")
    except FileExistsError:
        click.echo("kg.toml already exists — skipping init")

    cfg = load_config(root_path)
    cfg.ensure_dirs()
    click.echo(f"Nodes dir : {cfg.nodes_dir}")
    click.echo(f"Index dir : {cfg.index_dir}")

    n = rebuild_all(cfg.nodes_dir, cfg.db_path)
    click.echo(f"Indexed {n} nodes")

    slugs = bootstrap_patterns(cfg)
    if slugs:
        click.echo(f"Bootstrapped patterns: {', '.join(slugs)}")


# ---------------------------------------------------------------------------
# kg reindex / kg upgrade
# ---------------------------------------------------------------------------

@cli.command()
def reindex() -> None:
    """Rebuild SQLite index from all node.jsonl files."""
    cfg = _load_cfg()
    cfg.ensure_dirs()
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, verbose=True)
    click.echo(f"Indexed {n} nodes")


@cli.command()
def upgrade() -> None:
    """Rebuild index and apply any schema migrations (safe to run anytime)."""
    cfg = _load_cfg()
    cfg.ensure_dirs()
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, verbose=True)
    click.echo(f"Upgraded: indexed {n} nodes")


# ---------------------------------------------------------------------------
# kg add
# ---------------------------------------------------------------------------

BULLET_TYPES = ["fact", "gotcha", "decision", "task", "note", "success", "failure"]


@cli.command()
@click.argument("slug")
@click.argument("text")
@click.option("--type", "bullet_type", default="fact", type=click.Choice(BULLET_TYPES), show_default=True)
@click.option("--status", default=None, type=click.Choice(["pending", "completed", "archived"]))
def add(slug: str, text: str, bullet_type: str, status: str | None) -> None:
    """Add a bullet to a node (auto-creates node if missing)."""
    cfg = _load_cfg()
    store = FileStore(cfg.nodes_dir)
    bullet = store.add_bullet(slug, text=text, bullet_type=bullet_type, status=status)
    index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path)
    click.echo(bullet.id)


# ---------------------------------------------------------------------------
# kg show
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("slug")
@click.option("--max-width", "-w", default=0, help="Truncate bullet text to N chars (0 = unlimited)")
def show(slug: str, max_width: int) -> None:
    """Show all bullets for a node."""
    cfg = _load_cfg()
    store = FileStore(cfg.nodes_dir)
    node = store.get(slug)
    if node is None:
        raise click.ClickException(f"Node not found: {slug}")

    live = node.live_bullets
    budget_info = f"  ↑{int(node.token_budget)} credits" if node.token_budget >= 100 else ""
    threshold = cfg.review.budget_threshold
    hint = node.review_hint(threshold=threshold, bullet_count=len(live))
    click.echo(f"# {node.title}  [{node.slug}]  type={node.type}  ●{len(live)} bullets{budget_info}")
    if hint:
        bar = "─" * 60
        click.echo(bar)
        click.echo(f"⚠ NEEDS REVIEW: {int(node.token_budget)} credits, {len(live)} bullets  see [node-review]")
        click.echo(f"  Run `kg review {slug}` when done.")
        click.echo(bar)
    for b in live:
        prefix = f"({b.type}) " if b.type != "fact" else ""
        vote_info = f"  [+{b.useful}/-{b.harmful}]" if b.useful or b.harmful else ""
        text = (b.text[:max_width] + "…") if max_width and len(b.text) > max_width else b.text
        click.echo(f"  {prefix}{text}  ←{b.id}{vote_info}")


# ---------------------------------------------------------------------------
# kg review
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("slug", required=False)
@click.option("--limit", "-n", default=20, show_default=True)
@click.option("--threshold", default=None, type=float, help="Min token_budget to list (default: from kg.toml [review])")
def review(slug: str | None, limit: int, threshold: float | None) -> None:
    """List nodes needing review, or mark a node as reviewed.

    \b
    kg review              # list nodes ordered by budget
    kg review <slug>       # mark as reviewed — clears budget
    """
    cfg = _load_cfg()
    effective_threshold = threshold if threshold is not None else cfg.review.budget_threshold

    if slug:
        # Mark a specific node as reviewed
        store = FileStore(cfg.nodes_dir)
        node = store.get(slug)
        if node is None:
            raise click.ClickException(f"Node not found: {slug}")
        store.clear_node_budget(slug)
        index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path)
        click.echo(f"Marked reviewed: [{slug}] {node.title}  (budget cleared)")
        return

    # List nodes needing review
    if not cfg.db_path.exists():
        click.echo("No index found — run `kg reindex` first")
        return
    conn = sqlite3.connect(str(cfg.db_path))
    rows = conn.execute(
        """SELECT slug, title, bullet_count, token_budget, last_reviewed
           FROM nodes
           WHERE token_budget >= ? AND type NOT LIKE '_%'
           ORDER BY token_budget DESC
           LIMIT ?""",
        (effective_threshold, limit),
    ).fetchall()
    conn.close()
    if not rows:
        click.echo(f"No nodes above {int(effective_threshold)} credits — graph looks healthy.")
        return
    click.echo(f"{'Credits':>8}  {'Bullets':>7}  Node")
    click.echo("-" * 50)
    for row_slug, title, bullet_count, budget, last_reviewed in rows:
        reviewed = f"  last reviewed {last_reviewed[:10]}" if last_reviewed else ""
        click.echo(f"{int(budget):>8}  {bullet_count or 0:>7}  [{row_slug}] {title}{reviewed}")


# ---------------------------------------------------------------------------
# kg search
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False)
@click.option("--query-file", "-Q", default=None, type=click.Path(exists=True), help="Read query from file (avoids shell escaping)")
@click.option("--session", "-s", default=None, help="Session ID (reserved for future session-aware boost)")
@click.option("--limit", "-n", default=20, show_default=True)
@click.option("--flat", is_flag=True, help="Show individual bullets, not grouped by node")
def search(query: str | None, query_file: str | None, session: str | None, limit: int, flat: bool) -> None:  # noqa: ARG001
    """FTS5 search over bullets."""
    if query_file:
        query = Path(query_file).read_text().strip()
    if not query:
        raise click.ClickException("Provide QUERY or --query-file / -Q")

    cfg = _load_cfg()
    rows = search_fts(query, cfg.db_path, limit=limit)
    if not rows:
        click.echo("(no results)")
        return

    if flat:
        for r in rows:
            click.echo(f"[{r['slug']}] {r['text']}  ←{r['bullet_id']}")
        return

    # Group by slug, fetch titles from index
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["slug"], []).append(r)

    if cfg.db_path.exists():
        conn = sqlite3.connect(str(cfg.db_path))
        titles: dict[str, str] = dict(
            conn.execute(
                f"SELECT slug, title FROM nodes WHERE slug IN ({','.join('?' * len(groups))})",  # noqa: S608
                list(groups),
            ).fetchall()
        )
        conn.close()
    else:
        titles = {}

    for slug, bullets in groups.items():
        title = titles.get(slug, "")
        header = f"[{slug}]" + (f"  {title}" if title and title != slug else "")
        click.echo(f"\n{header}")
        for b in bullets:
            click.echo(f"  {b['text']}  ←{b['bullet_id']}")


# ---------------------------------------------------------------------------
# kg context
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query", required=False)
@click.option("--compact", "-c", is_flag=True, help="Compact output (default)")
@click.option("--session", "-s", default=None, help="Session ID for differential context")
@click.option("--max-tokens", default=1000, show_default=True)
@click.option("--limit", "-n", default=20, show_default=True)
@click.option("--query-file", "-Q", default=None, type=click.Path(exists=True))
def context(
    query: str | None,
    compact: bool,  # noqa: ARG001  (reserved for future non-compact mode)
    session: str | None,
    max_tokens: int,
    limit: int,
    query_file: str | None,
) -> None:
    """Packed context output for LLM injection."""
    if query_file:
        query = Path(query_file).read_text().strip()
    if not query:
        raise click.ClickException("Provide QUERY or --query-file")

    cfg = _load_cfg()
    result = build_context(
        query,
        db_path=cfg.db_path,
        nodes_dir=cfg.nodes_dir,
        max_tokens=max_tokens,
        limit=limit,
        session_id=session,
        review_threshold=cfg.review.budget_threshold,
    )

    if not result.nodes:
        click.echo("(no results)")
        return

    click.echo(result.format_compact())


# ---------------------------------------------------------------------------
# kg index
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("path", required=False)
@click.option("--source", "source_name", default=None, help="Index only this named [[sources]] entry")
@click.option("--include", "-p", multiple=True, help="File patterns (e.g. '**/*.py'). One-off only.")
@click.option("--exclude", "-x", multiple=True, help="Exclude patterns. One-off only.")
@click.option("--no-git", is_flag=True, help="Don't use git ls-files")
@click.option("--max-size", default=512, show_default=True, help="Max file size in KB")
@click.option("--dry-run", is_flag=True)
@click.option("--verbose", "-v", is_flag=True)
@click.option("--watch", is_flag=True, help="Keep running: reindex on changes (uses inotify/poll)")
def index(
    path: str | None,
    source_name: str | None,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
    no_git: bool,
    max_size: int,
    dry_run: bool,
    verbose: bool,
    watch: bool,
) -> None:
    """Index files for FTS search (no LLM extraction).

    Examples:
      kg index                     # index all [[sources]] from kg.toml
      kg index src/ -p '**/*.py'   # one-off: index a directory
      kg index --source workspace  # index a named [[sources]] entry
      kg index --watch             # inotify watcher mode
    """
    cfg = _load_cfg()
    cfg.ensure_dirs()

    if watch:
        click.echo("Starting watcher (Ctrl+C to stop)...")
        run_from_config(cfg.root)
        return

    # Build list of sources to index
    if path:
        src = SourceConfig(
            path=path,
            name="",
            include=list(include) if include else list(cfg.sources[0].include if cfg.sources else ["**/*"]),
            exclude=list(exclude) if exclude else [],
            use_git=not no_git,
            max_size_kb=max_size,
        ).resolve(cfg.root)
        sources_to_index = [src]
    elif source_name:
        sources_to_index = [s for s in cfg.sources if s.name == source_name]
        if not sources_to_index:
            raise click.ClickException(f"No [[sources]] entry named '{source_name}'")
    else:
        sources_to_index = cfg.sources
        if not sources_to_index:
            raise click.ClickException(
                "No [[sources]] in kg.toml. Add one or pass a PATH argument."
            )

    total: dict[str, int] = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0}

    for src in sources_to_index:
        label = src.name or str(src.path)
        click.echo(f"Indexing: {label} ({src.abs_path})")

        if dry_run:
            files = collect_files(src)
            click.echo(f"  Would index {len(files)} files (dry run)")
            continue

        stats = index_source(src, db_path=cfg.db_path, verbose=verbose)
        for k, v in stats.items():
            total[k] += v

        parts = [f"{v} {k}" for k, v in stats.items() if v]
        click.echo(f"  {', '.join(parts)}")

    if not dry_run and len(sources_to_index) > 1:
        parts = [f"{v} {k}" for k, v in total.items() if v]
        click.echo(f"Total: {', '.join(parts)}")


# ---------------------------------------------------------------------------
# kg bootstrap
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--overwrite", is_flag=True, help="Re-install even if pattern nodes already exist")
def bootstrap(overwrite: bool) -> None:
    """Load bundled pattern nodes into the graph (fleeting-notes, graph-first-workflow, etc.)."""
    cfg = _load_cfg()
    slugs = bootstrap_patterns(cfg, overwrite=overwrite)
    if slugs:
        click.echo(f"Bootstrapped: {', '.join(slugs)}")
    else:
        click.echo("All patterns already present (use --overwrite to reinstall)")


# ---------------------------------------------------------------------------
# kg start / status / stop
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--scope", default="user", type=click.Choice(["user", "local", "project"]), show_default=True, help="Claude MCP scope")
def start(scope: str) -> None:
    """Ensure everything is running: index, watcher, MCP server, hooks."""
    cfg = _load_cfg()
    cfg.ensure_dirs()

    # 1. Reindex
    click.echo("Indexing nodes...")
    n = rebuild_all(cfg.nodes_dir, cfg.db_path)
    click.echo(f"  ✓ Indexed {n} nodes")

    # 2. Index file sources
    if cfg.sources:
        click.echo(f"Indexing {len(cfg.sources)} file source(s)...")
        for src in cfg.sources:
            stats = index_source(src, db_path=cfg.db_path)
            parts = [f"{v} {k}" for k, v in stats.items() if v]
            click.echo(f"  [{src.name or src.path}] {', '.join(parts) or 'no changes'}")

    # 3. Watcher
    click.echo("Starting watcher...")
    method, wstatus = ensure_watcher(cfg)
    click.echo(f"  ✓ Watcher [{method}]: {wstatus}")

    # 4. MCP server
    click.echo("Registering MCP server...")
    ok, msg = ensure_mcp_registered(scope=scope)
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} {msg}")

    # 5. Hook
    click.echo("Installing session_context hook...")
    ok, msg = ensure_hook_installed()
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} {msg}")

    click.echo("\nDone. Run `kg status` to verify.")


@cli.command()
def status() -> None:
    """Show project stats and status of watcher, MCP server, and hook."""
    cfg = _load_cfg()

    click.echo(f"Project   : {cfg.name}  ({cfg.root})")

    # Node + bullet stats from SQLite (fast)
    if cfg.db_path.exists():
        conn = sqlite3.connect(str(cfg.db_path))
        row = conn.execute(
            "SELECT COUNT(*), SUM(bullet_count) FROM nodes WHERE type NOT LIKE '_%'"
        ).fetchone()
        n_nodes, n_bullets = (row[0] or 0), (row[1] or 0)
        review_count = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE token_budget >= ? AND type NOT LIKE '_%'",
            (cfg.review.budget_threshold,),
        ).fetchone()[0]
        conn.close()
        review_hint = f"  ⚠ {review_count} need review" if review_count else ""
        click.echo(f"Nodes     : {n_nodes} nodes, {n_bullets} bullets{review_hint}")
        db_mtime = cfg.db_path.stat().st_mtime
        age_s = int(time.time() - db_mtime)
        if age_s < 120:
            age = f"{age_s}s ago"
        elif age_s < 3600:
            age = f"{age_s // 60}m ago"
        else:
            age = f"{age_s // 3600}h ago"
        click.echo(f"Index     : {cfg.db_path}  (updated {age})")
    else:
        store = FileStore(cfg.nodes_dir)
        n_nodes = len(store.list_slugs())
        click.echo(f"Nodes     : {n_nodes} (no index — run `kg reindex`)")
        click.echo(f"Index     : {cfg.db_path}  (missing)")

    click.echo(f"Watcher   : {watcher_status(cfg)}")
    click.echo(f"MCP       : {mcp_health(cfg)}")
    click.echo(f"Hook      : {hook_status()}")


@cli.command()
def stop() -> None:
    """Stop the background watcher (if running via PID file)."""
    cfg = _load_cfg()
    result = stop_watcher(cfg)
    click.echo(f"Watcher: {result}")


# ---------------------------------------------------------------------------
# kg serve
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--root", default=None, help="Override project root (default: auto-detect from cwd)")
def serve(root: str | None) -> None:
    """Start stdio MCP server (connect via Claude Code MCP config)."""
    root_path = Path(root).resolve() if root else None
    run_server(root_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
