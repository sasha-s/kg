"""Context packing: FTS search → compact ranked output for LLM injection.

Compact output format (matches mg context -c):
    [slug] Title: bullet text ←b-id1 | another bullet ←b-id2
    [slug2] bullet text ←b-id3
    ↳ Explore: [other-slug], [third-slug]   ← global, at the end

After serving a node, its token_budget is incremented by chars_served in meta.jsonl.
Budget clears on explicit review (kg show / memory_show).
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kg.indexer import get_backlinks, search_fts
from kg.reader import FileStore

if TYPE_CHECKING:
    from pathlib import Path

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
        """Single-line format: [slug] Title: bullet ←id | bullet ←id"""
        bullet_parts = [f"{text} ←{bid}" for bid, text in self.bullets]
        body = " | ".join(bullet_parts)
        prefix = f"[{self.slug}] {self.title}"
        return f"{prefix}: {body}" if body else prefix


@dataclass
class PackedContext:
    nodes: list[ContextNode]
    total_chars: int

    def format_compact(self) -> str:
        lines = [n.format_compact() for n in self.nodes]
        # Collect all explore hints globally (deduplicated, excluding already-shown slugs)
        shown = {n.slug for n in self.nodes}
        explore: list[str] = []
        seen_explore: set[str] = set()
        for n in self.nodes:
            for s in n.explore:
                if s not in shown and s not in seen_explore:
                    explore.append(s)
                    seen_explore.add(s)
        if explore:
            lines.append("↳ Explore: " + ", ".join(f"[{s}]" for s in explore[:10]))
        return "\n".join(lines)


def build_context(
    query: str,
    *,
    db_path: Path,
    nodes_dir: Path,
    max_tokens: int = 1000,
    limit: int = 20,
    session_id: str | None = None,  # noqa: ARG001  (reserved for differential context)
    seen_slugs: set[str] | None = None,
    update_budget: bool = True,
    review_threshold: float = 500.0,
) -> PackedContext:
    """FTS search → group by node → pack into budget → return PackedContext.

    If update_budget=True (default), increments each served node's token_budget
    in meta.jsonl by the number of chars included in the output.
    """
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


def _update_budgets(nodes: list[ContextNode], store: FileStore) -> None:
    """Increment each node's token_budget by chars served."""
    for ctx_node in nodes:
        chars = len(ctx_node.format_compact())
        with contextlib.suppress(Exception):  # never fail context output due to budget update
            store.update_node_budget(ctx_node.slug, chars)
