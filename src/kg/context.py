"""Context packing: FTS search → compact ranked output for LLM injection.

Compact output format (matches `mg context -c`):
    [slug] Title
    bullet text ←b-id1 | another bullet ←b-id2
    ↳ Explore: [other-slug], [third-slug]

Backlinks / cross-references in [slug] notation are collected as "explore" hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kg.indexer import get_backlinks, search_fts

if TYPE_CHECKING:
    pass

_CROSSREF_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]")
_INTERNAL_PREFIX = ("_",)


@dataclass
class ContextNode:
    slug: str
    title: str
    score: float
    bullets: list[tuple[str, str]]   # (bullet_id, text)
    explore: list[str] = field(default_factory=list)

    def format_compact(self) -> str:
        """Single-line format: bullets joined with ' | ', IDs as ←id."""
        bullet_parts = [f"{text} ←{bid}" for bid, text in self.bullets]
        header = f"[{self.slug}] {self.title}"
        body = " | ".join(bullet_parts)
        lines = [header, body] if body else [header]
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
) -> PackedContext:
    """FTS search → group by node → pack into budget → return PackedContext."""
    from kg.reader import FileStore

    # Characters budget (rough: 1 token ≈ 4 chars)
    char_budget = max_tokens * 4

    raw = search_fts(query, db_path, limit=limit * 3)

    # Group by slug, preserve best rank per slug
    groups: dict[str, list[tuple[str, str]]] = {}  # slug → [(bullet_id, text)]
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

    # Sort slugs by best rank (FTS rank is negative; abs gives magnitude)
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

        # Collect bullets that matched (keep order from FTS)
        matched_ids = {bid for bid, _ in groups[slug]}
        bullets = [(b.id, b.text) for b in node.live_bullets if b.id in matched_ids]

        # Collect explore hints from backlinks and cross-refs in matched bullets
        explore: set[str] = set()
        for _, text in bullets:
            for ref in _CROSSREF_RE.findall(text):
                if ref != slug and not ref.startswith(_INTERNAL_PREFIX):
                    explore.add(ref)
        backlinks = get_backlinks(slug, db_path)
        for bl in backlinks[:4]:
            if not bl.startswith(_INTERNAL_PREFIX):
                explore.add(bl)

        ctx_node = ContextNode(
            slug=slug,
            title=node.title,
            score=1.0 / (1.0 + slug_rank[slug]),
            bullets=bullets,
            explore=sorted(explore - {n.slug for n in packed_nodes}),
        )

        estimated = len(ctx_node.format_compact())
        if total_chars + estimated > char_budget and packed_nodes:
            # Try fitting with fewer bullets
            half = bullets[: max(1, len(bullets) // 2)]
            ctx_node.bullets = half
            estimated = len(ctx_node.format_compact())
            if total_chars + estimated > char_budget:
                continue

        packed_nodes.append(ctx_node)
        total_chars += estimated

    return PackedContext(nodes=packed_nodes, total_chars=total_chars)
