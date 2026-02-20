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

import time
from pathlib import Path

import click

from kg import reranker as _reranker
from kg.bootstrap import bootstrap_patterns
from kg.config import KGConfig, SourceConfig, init_config, load_config
from kg.context import build_context
from kg.daemon import (
    ensure_vector_server,
    ensure_watcher,
    stop_vector_server,
    stop_watcher,
    vector_server_status,
    watcher_status,
)
from kg.db import get_conn as _get_db_conn
from kg.file_indexer import collect_files, index_source
from kg.indexer import calibrate, get_calibration_status, index_node, rebuild_all, search_fts
from kg.install import (
    ensure_hook_installed,
    ensure_mcp_registered,
    ensure_stop_hook_installed,
    list_all_hooks,
    mcp_health,
)
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
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, verbose=True, cfg=cfg)
    click.echo(f"Indexed {n} nodes")


@cli.command()
def upgrade() -> None:
    """Rebuild index and apply any schema migrations (safe to run anytime)."""
    cfg = _load_cfg()
    cfg.ensure_dirs()
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, verbose=True, cfg=cfg)
    click.echo(f"Upgraded: indexed {n} nodes")


@cli.command()
@click.option(
    "--sample-size", default=200, show_default=True, help="Bullets to sample for calibration"
)
def calibrate_cmd(sample_size: int) -> None:
    """Calibrate FTS and vector search score quantiles.

    Samples bullets, runs searches, computes percentile breakpoints used to
    normalize scores when blending FTS and vector results.
    """
    cfg = _load_cfg()
    result = calibrate(cfg.db_path, cfg, sample_size=sample_size)
    if "error" in result:
        click.echo(f"Error: {result['error']}", err=True)
        return
    if "warning" in result:
        click.echo(f"Warning: {result['warning']}", err=True)
        return
    parts = [f"Sampled {result['bullets_sampled']} bullets"]
    if result.get("fts_calibrated"):
        parts.append(f"FTS: {result['fts_scores']} scores → calibrated")
    else:
        parts.append(f"FTS: {result.get('fts_scores', 0)} scores (need ≥20 to calibrate)")
    if result.get("vec_calibrated"):
        parts.append(f"Vector: {result['vec_scores']} scores → calibrated")
    elif result.get("vec_scores", 0) > 0:
        parts.append(f"Vector: {result['vec_scores']} scores (need ≥20 to calibrate)")
    click.echo("\n".join(parts))


cli.add_command(calibrate_cmd, name="calibrate")


# ---------------------------------------------------------------------------
# kg add
# ---------------------------------------------------------------------------

BULLET_TYPES = ["fact", "gotcha", "decision", "task", "note", "success", "failure"]


@cli.command()
@click.argument("slug")
@click.argument("text")
@click.option(
    "--type", "bullet_type", default="fact", type=click.Choice(BULLET_TYPES), show_default=True
)
@click.option("--status", default=None, type=click.Choice(["pending", "completed", "archived"]))
def add(slug: str, text: str, bullet_type: str, status: str | None) -> None:
    """Add a bullet to a node (auto-creates node if missing)."""
    cfg = _load_cfg()
    store = FileStore(cfg.nodes_dir)
    bullet = store.add_bullet(slug, text=text, bullet_type=bullet_type, status=status)
    index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path, cfg=cfg)
    click.echo(bullet.id)


# ---------------------------------------------------------------------------
# kg show
# ---------------------------------------------------------------------------


def _show_backlinks(slug: str, cfg: KGConfig, query: str | None = None, limit: int = 10) -> None:
    """Print bullets from other nodes that reference [slug].

    With query: ranked by cross-encoder. Without: ranked by node embedding cosine similarity.
    """
    if not cfg.db_path.exists():
        return
    conn = _get_db_conn(cfg)
    pattern = f"%[{slug}]%"
    rows = conn.execute(
        """SELECT b.id, b.node_slug, b.text
           FROM bullets b
           WHERE b.text LIKE ? AND b.node_slug != ?
           LIMIT 100""",
        (pattern, slug),
    ).fetchall()

    if not rows:
        conn.close()
        return
    conn.close()

    if query:
        ranked = _reranker.rerank(query, [(r[0], r[2]) for r in rows], cfg)
        id_order = {cid: i for i, (cid, _) in enumerate(ranked)}
        rows_sorted = sorted(rows, key=lambda r: id_order.get(r[0], len(rows)))
    else:
        rows_sorted = rows

    click.echo(f"\nReferenced by ({min(len(rows_sorted), limit)}):")
    for bid, node_slug, text in rows_sorted[:limit]:
        click.echo(f"  [{node_slug}] {text}  ←{bid}")


def _show_links_to(slug: str, cfg: KGConfig, query: str | None = None, limit: int = 10) -> None:
    """Print outgoing cross-references from this node.

    With query: ranked by cross-encoder against target node title.
    Without: listed in natural order.
    """
    if not cfg.db_path.exists():
        return
    conn = _get_db_conn(cfg)
    rows = conn.execute(
        """SELECT bl.to_slug, n.title
           FROM backlinks bl
           JOIN nodes n ON n.slug = bl.to_slug
           WHERE bl.from_slug = ?
           ORDER BY bl.to_slug""",
        (slug,),
    ).fetchall()
    conn.close()

    if not rows:
        return

    if query:
        ranked = _reranker.rerank(query, list(rows), cfg)
        id_order = {cid: i for i, (cid, _) in enumerate(ranked)}
        rows = sorted(rows, key=lambda r: id_order.get(r[0], len(rows)))

    click.echo(f"\nLinks to ({min(len(rows), limit)}):")
    for to_slug, title in rows[:limit]:
        click.echo(f"  [{to_slug}] {title}")


@cli.command()
@click.argument("slug")
@click.option(
    "--query", "-q", default=None, help="Rank bullets and links by relevance to this query"
)
@click.option("--limit", "-l", default=10, show_default=True, help="Max bullets to show (0 = all)")
@click.option(
    "--offset",
    "-o",
    default=0,
    show_default=True,
    help="Skip first N bullets (for pagination, ignored with -q)",
)
@click.option(
    "--max-width", "-w", default=0, help="Truncate bullet text to N chars (0 = unlimited)"
)
@click.option("--no-backlinks", is_flag=True, help="Skip backlinks and links sections")
def show(
    slug: str, query: str | None, limit: int, offset: int, max_width: int, no_backlinks: bool
) -> None:
    """Show bullets for a node.

    \b
    kg show <slug>              # first 10 bullets
    kg show <slug> -l 0         # all bullets
    kg show <slug> -l 5 -o 5    # bullets 6-10
    kg show <slug> -q "query"   # rank bullets and links by relevance
    """
    cfg = _load_cfg()
    store = FileStore(cfg.nodes_dir)
    node = store.get(slug)
    if node is None:
        raise click.ClickException(f"Node not found: {slug}")

    live = node.live_bullets
    total = len(live)

    if query:
        ranked = _reranker.rerank(query, [(b.id, b.text) for b in live], cfg)
        id_order = {bid: i for i, (bid, _) in enumerate(ranked)}
        live = sorted(live, key=lambda b: id_order.get(b.id, total))
        page = live if limit == 0 else live[:limit]
        ranked_label = f'  ranked by "{query}"'
    else:
        page = live[offset:] if limit == 0 else live[offset : offset + limit]
        ranked_label = ""

    shown = len(page)
    budget_info = f"  ↑{int(node.token_budget)} credits" if node.token_budget >= 100 else ""
    created = f"  created {node.created_at[:10]}" if node.created_at else ""
    if query:
        page_info = (
            f"  [top {shown} of {total}{ranked_label}]"
            if shown < total
            else f"  [{total} total{ranked_label}]"
        )
    else:
        page_info = (
            f"  [{offset + 1}-{offset + shown} of {total}]"
            if (offset or (limit and shown < total))
            else f"  [{total} total]"
            if limit == 0
            else ""
        )
    threshold = cfg.review.budget_threshold
    hint = node.review_hint(threshold=threshold, bullet_count=total)
    click.echo(
        f"# {node.title}  [{node.slug}]  type={node.type}  ●{total} bullets{budget_info}{created}{page_info}"
    )
    if hint:
        bar = "─" * 60
        click.echo(bar)
        see_ref = "" if slug == "node-review" else "  see [node-review]"
        cpb = int(node.credits_per_bullet(total))
        click.echo(f"⚠ NEEDS REVIEW: {int(node.token_budget)} credits, {cpb}/bullet{see_ref}")
        click.echo(f"  Run `kg review {slug}` when done.")
        click.echo(bar)
    for b in page:
        prefix = f"({b.type}) " if b.type != "fact" else ""
        vote_info = f"  [+{b.useful}/-{b.harmful}]" if b.useful or b.harmful else ""
        text = (b.text[:max_width] + "…") if max_width and len(b.text) > max_width else b.text
        click.echo(f"  {prefix}{text}  ←{b.id}{vote_info}")
    if not query and limit and shown < total and not offset:
        click.echo(f"  … {total - shown} more  (use -l 0 or -o {shown} to see more)")
    if not no_backlinks:
        _show_backlinks(slug, cfg, query=query)
        _show_links_to(slug, cfg, query=query)


# ---------------------------------------------------------------------------
# kg review
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("slug", required=False)
@click.option("--limit", "-n", default=20, show_default=True)
@click.option(
    "--threshold",
    default=None,
    type=float,
    help="Min token_budget to list (default: from kg.toml [review])",
)
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
        index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path, cfg=cfg)
        click.echo(f"Marked reviewed: [{slug}] {node.title}  (budget cleared)")
        return

    # List nodes needing review — read from files (always current, never stale)
    if not cfg.nodes_dir.exists():
        click.echo("No nodes directory found — run `kg init` first")
        return
    store = FileStore(cfg.nodes_dir)
    candidates = sorted(
        (
            n
            for n in store.iter_nodes()
            if not n.slug.startswith("_")
            and n.needs_review(effective_threshold, len(n.live_bullets))
        ),
        key=lambda n: n.credits_per_bullet(len(n.live_bullets)),
        reverse=True,
    )[:limit]
    if not candidates:
        click.echo(
            f"No nodes above {int(effective_threshold)} credits/bullet — graph looks healthy."
        )
        return
    click.echo(f"{'Cr/bullet':>9}  {'Credits':>8}  {'Bullets':>7}  Node")
    click.echo("-" * 60)
    for n in candidates:
        live = len(n.live_bullets)
        reviewed = f"  last reviewed {n.last_reviewed[:10]}" if n.last_reviewed else ""
        click.echo(
            f"{int(n.credits_per_bullet(live)):>9}  {int(n.token_budget):>8}  {live:>7}  [{n.slug}] {n.title}{reviewed}"
        )


# ---------------------------------------------------------------------------
# kg search
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("query", required=False)
@click.option(
    "--query-file",
    "-Q",
    default=None,
    type=click.Path(exists=True),
    help="Read query from file (avoids shell escaping)",
)
@click.option(
    "--rerank-query",
    "-q",
    "rerank_query",
    default=None,
    help="Rerank results with this query (defaults to search query)",
)
@click.option(
    "--session", "-s", default=None, help="Session ID (reserved for future session-aware boost)"
)
@click.option("--limit", "-n", default=20, show_default=True)
@click.option("--flat", is_flag=True, help="Show individual bullets, not grouped by node")
def search(
    query: str | None,
    query_file: str | None,
    rerank_query: str | None,  # noqa: ARG001
    session: str | None,  # noqa: ARG001
    limit: int,
    flat: bool,
) -> None:
    """FTS5 search over bullets."""
    if query_file:
        query = Path(query_file).read_text().strip()
    if not query:
        raise click.ClickException("Provide QUERY or --query-file / -Q")

    cfg = _load_cfg()
    rows = search_fts(query, cfg.db_path, limit=limit, cfg=cfg)
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
        conn = _get_db_conn(cfg)
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
@click.option(
    "--rerank-query",
    "-q",
    "rerank_query",
    default=None,
    help="Rerank results with this query (defaults to search query)",
)
def context(
    query: str | None,
    compact: bool,  # noqa: ARG001  (reserved for future non-compact mode)
    session: str | None,
    max_tokens: int,
    limit: int,
    query_file: str | None,
    rerank_query: str | None,
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
        cfg=cfg,
        max_tokens=max_tokens,
        limit=limit,
        session_id=session,
        rerank_query=rerank_query,
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
@click.option(
    "--source", "source_name", default=None, help="Index only this named [[sources]] entry"
)
@click.option(
    "--include", "-p", multiple=True, help="File patterns (e.g. '**/*.py'). One-off only."
)
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
            include=list(include)
            if include
            else list(cfg.sources[0].include if cfg.sources else ["**/*"]),
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
# kg vector-server
# ---------------------------------------------------------------------------


@cli.command("vector-server")
@click.option("--root", default=None, help="Override project root")
def vector_server_cmd(root: str | None) -> None:
    """Start vector server in foreground (for debugging)."""
    import subprocess
    import sys

    root_path = Path(root).resolve() if root else Path.cwd()
    subprocess.run([sys.executable, "-m", "kg.vector_server", str(root_path)], check=False)


# ---------------------------------------------------------------------------
# kg start / status / stop
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--scope",
    default="user",
    type=click.Choice(["user", "local", "project"]),
    show_default=True,
    help="Claude MCP scope",
)
def start(scope: str) -> None:
    """Ensure everything is running: index, watcher, MCP server, hooks."""
    cfg = _load_cfg()
    cfg.ensure_dirs()

    # 1. Reindex
    click.echo("Indexing nodes...")
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, cfg=cfg)
    click.echo(f"  ✓ Indexed {n} nodes")

    # 2. Index file sources
    if cfg.sources:
        click.echo(f"Indexing {len(cfg.sources)} file source(s)...")
        for src in cfg.sources:
            stats = index_source(src, db_path=cfg.db_path)
            parts = [f"{v} {k}" for k, v in stats.items() if v]
            click.echo(f"  [{src.name or src.path}] {', '.join(parts) or 'no changes'}")

    # 3. Calibrate search scores
    click.echo("Calibrating search scores...")
    cal_result = calibrate(cfg.db_path, cfg)
    if "error" in cal_result:
        click.echo(f"  ✗ Calibration: {cal_result['error']}", err=True)
    elif "warning" in cal_result:
        click.echo(f"  ⚠ Calibration: {cal_result['warning']}")
    else:
        click.echo(f"  ✓ Calibrated ({cal_result['bullets_sampled']} bullets)")

    # 4. Watcher
    click.echo("Starting watcher...")
    method, wstatus = ensure_watcher(cfg)
    click.echo(f"  ✓ Watcher [{method}]: {wstatus}")

    # 4b. Vector server
    click.echo("Starting vector server...")
    vmethod, vstatus = ensure_vector_server(cfg)
    click.echo(f"  ✓ Vector server [{vmethod}]: {vstatus}")

    # 4. MCP server
    click.echo("Registering MCP server...")
    ok, msg = ensure_mcp_registered(scope=scope)
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} {msg}")

    # 5. Hooks
    click.echo("Installing hooks...")
    ok, msg = ensure_hook_installed()
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} session_context (UserPromptSubmit): {msg}")

    if cfg.hooks.stop:
        ok, msg = ensure_stop_hook_installed()
        marker = "✓" if ok else "✗"
        click.echo(f"  {marker} stop (Stop): {msg}")
    else:
        click.echo("  - stop hook disabled  (set [hooks] stop = true in kg.toml to re-enable)")

    click.echo("\nDone. Run `kg status` to verify.")


@cli.command()
def status() -> None:
    """Show project stats and status of watcher, MCP server, and hook."""
    cfg = _load_cfg()

    click.echo(f"Project   : {cfg.name}  ({cfg.root})")

    # Node + bullet stats (review count from files — always current)
    if cfg.nodes_dir.exists():
        _store = FileStore(cfg.nodes_dir)
        _all = [n for n in _store.iter_nodes() if not n.slug.startswith("_")]
        n_nodes = len(_all)
        n_bullets = sum(len(n.live_bullets) for n in _all)
        review_count = sum(
            1 for n in _all if n.needs_review(cfg.review.budget_threshold, len(n.live_bullets))
        )
        review_hint = f"  ⚠ {review_count} need review" if review_count else ""
        click.echo(f"Nodes     : {n_nodes} nodes, {n_bullets} bullets{review_hint}")
        if cfg.use_turso:
            click.echo(f"Index     : {cfg.database.url}")
        elif cfg.db_path.exists():
            age_s = int(time.time() - cfg.db_path.stat().st_mtime)
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

    # Calibration status
    cal = get_calibration_status(cfg.db_path, cfg)
    if cal is None:
        click.echo("Calibration: never  — run `kg calibrate`")
    else:
        ops = cal["ops_since"]
        delta = cal["current_bullets"] - cal["bullet_count"]
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        stale = ops >= 20 or abs(delta) > max(5, cal["bullet_count"] // 10)
        flag = "⚠ stale" if stale else "current"
        hint = "  — run `kg calibrate`" if stale else ""
        click.echo(
            f"Calibration: {flag}  ({cal['bullet_count']} bullets, "
            f"{ops} ops since, {delta_str} bullets){hint}"
        )

    w_status = watcher_status(cfg)
    w_hint = "  — run `kg start` to start" if w_status == "stopped" else ""
    click.echo(f"Watcher   : {w_status}{w_hint}")
    vs_status = vector_server_status(cfg)
    vs_hint = "  — run `kg start` to start" if vs_status == "stopped" else ""
    click.echo(f"Vectors   : {vs_status}{vs_hint}")
    click.echo(f"MCP       : {mcp_health(cfg)}")

    # Hooks — installed hooks + kg hooks not yet installed
    from kg.install import _HOOK_COMMAND, _STOP_HOOK_COMMAND, _claude_settings_path

    hooks = list_all_hooks()
    settings_path = _claude_settings_path()
    installed_commands = {h["command"] for h in hooks}

    # Expected kg hooks based on config
    kg_expected: list[tuple[str, str, bool]] = [
        ("UserPromptSubmit", _HOOK_COMMAND, True),  # always expected
    ]
    if cfg.hooks.stop:
        kg_expected.append(("Stop", _STOP_HOOK_COMMAND, True))

    all_lines: list[str] = []
    for h in hooks:
        kg_marker = " [kg]" if h["kg"] else ""
        all_lines.append(f"  {h['event']}  {h['command']}{kg_marker}")
    for event, cmd, _ in kg_expected:
        if cmd not in installed_commands:
            all_lines.append(f"  {event}  {cmd} [kg] ✗ not installed — run `kg start`")

    n_installed = len(hooks)
    if not all_lines:
        click.echo(f"Hooks     : none  ({settings_path})")
    else:
        click.echo(f"Hooks     : {n_installed} installed  ({settings_path})")
        for line in all_lines:
            click.echo(f"           {line}")

    # Sources — what's being indexed beyond nodes/
    if cfg.sources:
        click.echo(f"Sources   : {len(cfg.sources)} source(s)")
        for src in cfg.sources:
            name_part = f"  [{src.name}]" if src.name else ""
            includes = ", ".join(src.include[:3])
            if len(src.include) > 3:
                includes += f", +{len(src.include) - 3} more"
            click.echo(f"            {src.abs_path}{name_part}  ({includes})")
    else:
        click.echo("Sources   : none  (add [[sources]] to kg.toml to index files)")


@cli.command()
def stop() -> None:
    """Stop the background watcher and vector server (if running via PID file)."""
    cfg = _load_cfg()
    result = stop_watcher(cfg)
    click.echo(f"Watcher: {result}")
    vresult = stop_vector_server(cfg)
    click.echo(f"Vector server: {vresult}")


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
# kg web
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--host",
    "-b",
    default=None,
    help="Bind address (default: kg.toml [server].web_host or 127.0.0.1)",
)
@click.option(
    "--port", "-p", default=None, type=int, help="Port (default: kg.toml [server].web_port or 7345)"
)
def web(host: str | None, port: int | None) -> None:
    """Start local web viewer with FTS+vector search.

    \b
    kg web                  # http://127.0.0.1:7345
    kg web --port 8080
    kg web --host 0.0.0.0   # expose on LAN
    """
    cfg = _load_cfg()
    from kg.web import serve as _web_serve

    _web_serve(cfg, host=host or cfg.server.web_host, port=port or cfg.server.web_port)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
