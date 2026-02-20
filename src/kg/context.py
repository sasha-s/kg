"""Context packing: FTS search → compact ranked output for LLM injection.

Compact output format:
    [slug] Title  ●N bullets  ↑1430 credits
    bullet text ←b-id1 | another bullet ←b-id2
    ⚠ needs review (1430 credits, 12 bullets)   ← only when budget exceeded
    ↳ Explore: [other-slug], [third-slug]

After serving a node, its token_budget is incremented by chars_served in meta.jsonl.
Budget clears on explicit review (kg show / memory_show).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kg.indexer import get_backlinks, search_fts

if TYPE_CHECKING:
    from kg.models import FileNode

_CROSSREF_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]")
_INTERNAL_PREFIX = ("_",)


@dataclass
class ContextNode:
    slug: str
    title: str
    score: float
    bullets: list[tuple[str, str]]   # (bullet_id, text)
    total_bullets: int = 0           # total live bullets in node (not just matched)
    token_budget: float = 0.0
    explore: list[str] = field(default_factory=list)
    review_hint: str | None = None

    def format_compact(self) -> str:
        """Compact format: header with counts, bullets, optional review hint, explore."""
        bullet_parts = [f"{text} ←{bid}" for bid, text in self.bullets]

        # Header: slug, title, bullet count, budget if significant
        meta_parts: list[str] = []
        if self.total_bullets:
            meta_parts.append(f"●{self.total_bullets}")
        if self.token_budget >= 100:
            meta_parts.append(f"↑{int(self.token_budget)}")
        meta_suffix = f"  {'  '.join(meta_parts)}" if meta_parts else ""
        header = f"[{self.slug}] {self.title}{meta_suffix}"

        body = " | ".join(bullet_parts)
        lines = [header]
        if body:
            lines.append(body)
        if self.review_hint:
            lines.append(self.review_hint)
        if self.explore:
            explore_str = ", ".join(f"[{s}]" for s in self.explore[:6])
            lines.append(f"↳ Explore: {explore_str}")
        return "\n".join(lines)


@dataclass
class PackedContext:
    nodes: list[ContextNode]
    total_chars: int

    def format_compact(self) -> str:
        return "\n\n".join(n.format_compact() for n in self.nodes)


def build_context(
    query: str,
    *,
    db_path: Path,
    nodes_dir: Path,
    max_tokens: int = 1000,
    limit: int = 20,
    session_id: str | None = None,
    seen_slugs: set[str] | None = None,
    update_budget: bool = True,
    review_threshold: float = 500.0,
) -> PackedContext:
    """FTS search → group by node → pack into budget → return PackedContext.

    If update_budget=True (default), increments each served node's token_budget
    in meta.jsonl by the number of chars included in the output.
    """
    from kg.reader import FileStore

    char_budget = max_tokens * 4  # rough: 1 token ≈ 4 chars

    raw = search_fts(query, db_path, limit=limit * 3)

    # Group by slug, preserve best rank per slug
    groups: dict[str, list[tuple[str, str]]] = {}
    slug_rank: dict[str, float] = {}
    for r in raw:
        slug = r["slug"]
        if slug.startswith(_INTERNAL_PREFIX):
            continue
        if seen_slugs and slug in seen_slugs:
            continue
        if slug not in groups:
            groups[slug] = []
            slug_rank[slug] = abs(r["rank"])
        groups[slug].append((r["bullet_id"], r["text"]))

    sorted_slugs = sorted(groups, key=lambda s: slug_rank[s])

    store = FileStore(nodes_dir)
    packed_nodes: list[ContextNode] = []
    total_chars = 0

    for slug in sorted_slugs:
        if total_chars >= char_budget:
            break

        node = store.get(slug)
        if node is None:
            continue

        live = node.live_bullets
        matched_ids = {bid for bid, _ in groups[slug]}
        bullets = [(b.id, b.text) for b in live if b.id in matched_ids]

        # Explore hints from cross-refs and backlinks
        explore: set[str] = set()
        for _, text in bullets:
            for ref in _CROSSREF_RE.findall(text):
                if ref != slug and not ref.startswith(_INTERNAL_PREFIX):
                    explore.add(ref)
        for bl in get_backlinks(slug, db_path)[:4]:
            if not bl.startswith(_INTERNAL_PREFIX):
                explore.add(bl)

        hint = node.review_hint(threshold=review_threshold, bullet_count=len(live))

        ctx_node = ContextNode(
            slug=slug,
            title=node.title,
            score=1.0 / (1.0 + slug_rank[slug]),
            bullets=bullets,
            total_bullets=len(live),
            token_budget=node.token_budget,
            explore=sorted(explore - {n.slug for n in packed_nodes}),
            review_hint=hint,
        )


        estimated = len(ctx_node.format_compact())
        if total_chars + estimated > char_budget and packed_nodes:
            half = bullets[: max(1, len(bullets) // 2)]
            ctx_node.bullets = half
            estimated = len(ctx_node.format_compact())
            if total_chars + estimated > char_budget:
                continue

        packed_nodes.append(ctx_node)
        total_chars += estimated

    # Update budgets after packing (increment by chars contributed)
    if update_budget and packed_nodes:
        _update_budgets(packed_nodes, store)

    return PackedContext(nodes=packed_nodes, total_chars=total_chars)


def _update_budgets(nodes: list[ContextNode], store: "FileStore") -> None:  # noqa: F821
    """Increment each node's token_budget by chars served."""
    for ctx_node in nodes:
        chars = len(ctx_node.format_compact())
        try:
            store.update_node_budget(ctx_node.slug, chars)
        except Exception:
            pass  # never fail context output due to budget update
