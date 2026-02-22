"""Stdio MCP server for kg using FastMCP.

Tools (named for compatibility with memory_graph MCP):
    memory_context(query, session_id?)          → compact context text
    memory_search(query, limit?)                → list of search results
    memory_show(slug)                           → node text
    memory_add_bullet(node_slug, text, ...)     → bullet_id
    memory_delete_bullet(bullet_id)             → confirm deleted
    memory_mark_reviewed(slug)                  → confirm reviewed
    memory_review(threshold?, limit?)           → nodes needing review

Session ID:
    Auto-injected by hooks/session_context.py via Claude's additionalContext.
    Passed as `session_id` parameter in memory_context calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP

from kg.config import KGConfig, load_config
from kg.context import build_context
from kg.indexer import search_fts
from kg.reader import FileStore

if TYPE_CHECKING:
    from pathlib import Path

mcp: FastMCP = FastMCP("kg")

_cfg_root: Path | None = None
_seen_slugs: set[str] = set()  # node slugs returned this MCP session (differential context)


def _cfg() -> KGConfig:
    cfg = load_config(_cfg_root)
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_context(
    query: Annotated[str, "Search query"],
    session_id: Annotated[str, "Session ID (auto-provided by hook)"] = "",
    max_tokens: Annotated[int, "Max tokens in output"] = 1000,
    limit: Annotated[int, "Max nodes to consider"] = 20,
    fresh: Annotated[bool, "Reset session tracking (start fresh)"] = False,
    existing: Annotated[list[str] | None, "Slugs already in context (will be skipped)"] = None,
) -> str:
    """Search the knowledge graph and return ranked context for LLM injection."""
    global _seen_slugs  # noqa: PLW0603
    if fresh:
        _seen_slugs = set()
    if existing:
        _seen_slugs.update(existing)
    cfg = _cfg()
    result = build_context(
        query,
        db_path=cfg.db_path,
        nodes_dir=cfg.nodes_dir,
        cfg=cfg,
        max_tokens=max_tokens,
        limit=limit,
        session_id=session_id or None,
        review_threshold=cfg.review.budget_threshold,
        seen_slugs=_seen_slugs if _seen_slugs else None,
    )
    if not result.nodes:
        return "(no results)"
    # Track returned slugs for differential context
    _seen_slugs.update(n.slug for n in result.nodes)
    return result.format_compact()


@mcp.tool()
def memory_search(
    query: Annotated[str, "Search query"],
    limit: Annotated[int, "Max results"] = 20,
) -> str:
    """FTS search over bullets. Returns ranked list of matching bullets."""
    cfg = _cfg()
    rows = search_fts(query, cfg.db_path, limit=limit, cfg=cfg)
    if not rows:
        return "(no results)"
    return "\n".join(f"[{r['slug']}] {r['text'][:120]} ←{r['bullet_id']}" for r in rows)


@mcp.tool()
def memory_show(slug: Annotated[str, "Node slug"]) -> str:
    """Show all bullets for a node by slug."""
    cfg = _cfg()
    store = FileStore(cfg.nodes_dir)
    node = store.get(slug)
    if node is None:
        return f"Node not found: {slug}"
    live = node.live_bullets
    budget_info = f"  ↑{int(node.token_budget)} credits" if node.token_budget >= 100 else ""
    hint = node.review_hint(threshold=cfg.review.budget_threshold, bullet_count=len(live))
    lines = [f"# {node.title} [{node.slug}]  type={node.type}  ●{len(live)} bullets{budget_info}"]
    if hint:
        bar = "─" * 60
        lines += [bar, f"⚠ NEEDS REVIEW: {int(node.token_budget)} credits, {len(live)} bullets", bar]
    for b in live:
        prefix = f"({b.type}) " if b.type != "fact" else ""
        lines.append(f"- {prefix}{b.text}  ←{b.id}")
    return "\n".join(lines)


@mcp.tool()
def memory_add_bullet(
    node_slug: Annotated[str, "Node slug (use _fleeting-<session_id[:12]> for session notes)"],
    text: Annotated[str, "Bullet text"],
    bullet_type: Annotated[str, "Bullet type: fact, gotcha, decision, task, note, success, failure"] = "fact",
    status: Annotated[str, "For task bullets: pending, completed, archived"] = "",
) -> str:
    """Add a bullet to a node. Auto-creates node if it doesn't exist."""
    cfg = _cfg()
    store = FileStore(cfg.nodes_dir)
    bullet = store.add_bullet(
        node_slug,
        text=text,
        bullet_type=bullet_type,
        status=status or None,
    )
    return bullet.id


@mcp.tool()
def memory_delete_bullet(
    bullet_id: Annotated[str, "Bullet ID to delete (e.g. b-abc12345)"],
) -> str:
    """Delete a bullet by ID (appends a tombstone — logically removes it from all views)."""
    import json as _json

    cfg = _cfg()
    slug: str | None = None
    if cfg.nodes_dir.exists():
        for path in cfg.nodes_dir.glob("*/node.jsonl"):
            try:
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    obj = _json.loads(line)
                    if obj.get("id") == bullet_id and not obj.get("deleted"):
                        slug = path.parent.name
                        break
            except Exception:  # noqa: BLE001
                continue
            if slug:
                break
    if slug is None:
        return f"Bullet not found: {bullet_id}"
    store = FileStore(cfg.nodes_dir)
    store.delete_bullet(slug, bullet_id)
    return f"Deleted {bullet_id} from [{slug}]"


@mcp.tool()
def memory_mark_reviewed(slug: Annotated[str, "Node slug"]) -> str:
    """Mark a node as reviewed after examining it. Clears the token budget."""
    cfg = _cfg()
    store = FileStore(cfg.nodes_dir)
    if not store.exists(slug):
        return f"Node not found: {slug}"
    store.clear_node_budget(slug)
    return f"Marked reviewed: {slug}"


@mcp.tool()
def memory_review(
    threshold: Annotated[float, "Credits-per-bullet threshold"] = 0,
    limit: Annotated[int, "Max nodes to list"] = 20,
) -> str:
    """List nodes ordered by credits-per-bullet — these need examination and maintenance."""
    cfg = _cfg()
    t = threshold or cfg.review.budget_threshold
    store = FileStore(cfg.nodes_dir)
    candidates = sorted(
        (
            n for n in store.iter_nodes()
            if not n.slug.startswith("_") and n.needs_review(t, len(n.live_bullets))
        ),
        key=lambda n: n.credits_per_bullet(len(n.live_bullets)),
        reverse=True,
    )[:limit]
    if not candidates:
        return "No nodes need review — graph looks healthy."
    lines = [f"{'Cr/bullet':>9}  {'Credits':>8}  {'Bullets':>7}  Node", "-" * 60]
    for n in candidates:
        live = len(n.live_bullets)
        lines.append(f"{int(n.credits_per_bullet(live)):>9}  {int(n.token_budget):>8}  {live:>7}  [{n.slug}] {n.title}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_server(config_root: Path | None = None) -> None:
    """Entry point for `kg serve`."""
    global _cfg_root  # noqa: PLW0603
    _cfg_root = config_root
    mcp.run()
