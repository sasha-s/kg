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

import sys
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cfg(ctx: click.Context) -> "KGConfig":  # type: ignore[name-defined]
    from kg.config import load_config
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
    from kg.config import init_config, load_config
    from kg.indexer import rebuild_all

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

    from kg.bootstrap import bootstrap_patterns
    slugs = bootstrap_patterns(cfg)
    if slugs:
        click.echo(f"Bootstrapped patterns: {', '.join(slugs)}")


# ---------------------------------------------------------------------------
# kg reindex / kg upgrade
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def reindex(ctx: click.Context) -> None:
    """Rebuild SQLite index from all node.jsonl files."""
    from kg.indexer import rebuild_all
    cfg = _load_cfg(ctx)
    cfg.ensure_dirs()
    n = rebuild_all(cfg.nodes_dir, cfg.db_path, verbose=True)
    click.echo(f"Indexed {n} nodes")


@cli.command()
@click.pass_context
def upgrade(ctx: click.Context) -> None:
    """Rebuild index and apply any schema migrations (safe to run anytime)."""
    from kg.indexer import rebuild_all
    cfg = _load_cfg(ctx)
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
@click.pass_context
def add(ctx: click.Context, slug: str, text: str, bullet_type: str, status: str | None) -> None:
    """Add a bullet to a node (auto-creates node if missing)."""
    from kg.indexer import index_node
    from kg.reader import FileStore

    cfg = _load_cfg(ctx)
    store = FileStore(cfg.nodes_dir)
    bullet = store.add_bullet(slug, text=text, bullet_type=bullet_type, status=status)
    index_node(slug, nodes_dir=cfg.nodes_dir, db_path=cfg.db_path)
    click.echo(bullet.id)


# ---------------------------------------------------------------------------
# kg show
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("slug")
@click.pass_context
def show(ctx: click.Context, slug: str) -> None:
    """Show all bullets for a node."""
    from kg.reader import FileStore

    cfg = _load_cfg(ctx)
    store = FileStore(cfg.nodes_dir)
    node = store.get(slug)
    if node is None:
        raise click.ClickException(f"Node not found: {slug}")

    click.echo(f"# {node.title}  [{node.slug}]  type={node.type}")
    for b in node.live_bullets:
        prefix = f"({b.type}) " if b.type != "fact" else ""
        vote_info = ""
        if b.useful or b.harmful:
            vote_info = f"  [+{b.useful}/-{b.harmful}]"
        click.echo(f"  {prefix}{b.text}  ←{b.id}{vote_info}")


# ---------------------------------------------------------------------------
# kg search
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, show_default=True)
@click.option("--flat", is_flag=True, help="Show individual bullets, not grouped by node")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, flat: bool) -> None:
    """FTS5 search over bullets."""
    from kg.indexer import search_fts

    cfg = _load_cfg(ctx)
    rows = search_fts(query, cfg.db_path, limit=limit)
    if not rows:
        click.echo("(no results)")
        return

    if flat:
        for r in rows:
            click.echo(f"[{r['slug']}] {r['text']}  ←{r['bullet_id']}")
        return

    # Group by slug
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["slug"], []).append(r)

    for slug, bullets in groups.items():
        click.echo(f"\n[{slug}]")
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
@click.pass_context
def context(
    ctx: click.Context,
    query: str | None,
    compact: bool,
    session: str | None,
    max_tokens: int,
    limit: int,
    query_file: str | None,
) -> None:
    """Packed context output for LLM injection."""
    from kg.context import build_context

    if query_file:
        query = Path(query_file).read_text().strip()
    if not query:
        raise click.ClickException("Provide QUERY or --query-file")

    cfg = _load_cfg(ctx)
    result = build_context(
        query,
        db_path=cfg.db_path,
        nodes_dir=cfg.nodes_dir,
        max_tokens=max_tokens,
        limit=limit,
    )

    if not result.nodes:
        click.echo("(no results)")
        return

    click.echo(result.format_compact())


# ---------------------------------------------------------------------------
# kg bootstrap
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--overwrite", is_flag=True, help="Re-install even if pattern nodes already exist")
@click.pass_context
def bootstrap(ctx: click.Context, overwrite: bool) -> None:
    """Load bundled pattern nodes into the graph (fleeting-notes, graph-first-workflow, etc.)."""
    from kg.bootstrap import bootstrap_patterns
    cfg = _load_cfg(ctx)
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
@click.pass_context
def start(ctx: click.Context, scope: str) -> None:
    """Ensure everything is running: index, watcher, MCP server, hooks."""
    from kg.daemon import ensure_watcher
    from kg.indexer import rebuild_all
    from kg.install import ensure_hook_installed, ensure_mcp_registered

    cfg = _load_cfg(ctx)
    cfg.ensure_dirs()

    # 1. Reindex
    click.echo("Indexing nodes...")
    n = rebuild_all(cfg.nodes_dir, cfg.db_path)
    click.echo(f"  ✓ Indexed {n} nodes")

    # 2. Watcher
    click.echo("Starting watcher...")
    method, status = ensure_watcher(cfg)
    click.echo(f"  ✓ Watcher [{method}]: {status}")

    # 3. MCP server
    click.echo("Registering MCP server...")
    ok, msg = ensure_mcp_registered(scope=scope)
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} {msg}")

    # 4. Hook
    click.echo("Installing session_context hook...")
    ok, msg = ensure_hook_installed()
    marker = "✓" if ok else "✗"
    click.echo(f"  {marker} {msg}")

    click.echo("\nDone. Run `kg status` to verify.")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show status of watcher, MCP server, and hook."""
    from kg.daemon import watcher_status
    from kg.install import mcp_health

    cfg = _load_cfg(ctx)

    from kg.reader import FileStore
    store = FileStore(cfg.nodes_dir)
    node_count = len(store.list_slugs())

    click.echo(f"Project   : {cfg.name} ({cfg.root})")
    click.echo(f"Nodes     : {node_count} ({cfg.nodes_dir})")
    click.echo(f"Index     : {cfg.db_path}")
    click.echo(f"Watcher   : {watcher_status(cfg)}")
    click.echo(f"MCP       : {mcp_health(cfg)}")


@cli.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the background watcher (if running via PID file)."""
    from kg.daemon import stop_watcher
    cfg = _load_cfg(ctx)
    result = stop_watcher(cfg)
    click.echo(f"Watcher: {result}")


# ---------------------------------------------------------------------------
# kg serve
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--root", default=None, help="Override project root (default: auto-detect from cwd)")
def serve(root: str | None) -> None:
    """Start stdio MCP server (connect via Claude Code MCP config)."""
    from kg.mcp import run_server
    root_path = Path(root).resolve() if root else None
    run_server(root_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
