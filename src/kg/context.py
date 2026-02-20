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

from kg.indexer import get_backlinks, get_calibration, score_to_quantile, search_fts
from kg.reader import FileStore

if TYPE_CHECKING:
    from pathlib import Path

    from kg.config import KGConfig

_CROSSREF_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-]*[a-z0-9])\]\]")
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
        """Single-line format: [slug] Title: bullet ←id | bullet ←id  ⚠"""
        bullet_parts = [f"{text} ←{bid}" for bid, text in self.bullets]
        body = " | ".join(bullet_parts)
        prefix = f"[{self.slug}] {self.title}"
        line = f"{prefix}: {body}" if body else prefix
        if self.review_hint:
            line += "  ⚠"
        return line


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
    cfg: KGConfig | None = None,
    max_tokens: int = 1000,
    limit: int = 20,
    session_id: str | None = None,
    rerank_query: str | None = None,
    seen_slugs: set[str] | None = None,
    update_budget: bool = True,
    review_threshold: float = 500.0,
) -> PackedContext:
    """FTS search → group by node → pack into budget → return PackedContext.

    If update_budget=True (default), increments each served node's token_budget
    in meta.jsonl by the number of chars included in the output.
    """
    char_budget = max_tokens * 4  # rough: 1 token ≈ 4 chars

    # Load session transcript fingerprint for dedup and boost
    fp = None
    session_ref_slugs: set[str] = set()
    if session_id:
        with contextlib.suppress(Exception):
            from kg.transcript import fingerprint_transcript, resolve_session_transcript
            tp = resolve_session_transcript(session_id)
            if tp:
                fp = fingerprint_transcript(tp)
                # Extract [slug] cross-refs from transcript for score boosting
                import re as _re
                for _slug in _re.findall(r"\[\[([a-z_][a-z0-9_]+-[a-z][a-z0-9_-]*[a-z0-9])\]\]", fp.text):
                    session_ref_slugs.add(_slug)

    raw = search_fts(query, db_path, limit=limit * 3, cfg=cfg)

    # Group bullets by slug; track best raw FTS score per slug (negated bm25, higher = better)
    groups: dict[str, list[tuple[str, str]]] = {}
    fts_scores: dict[str, float] = {}
    for r in raw:
        slug = r["slug"]
        if slug.startswith(_INTERNAL_PREFIX):
            continue
        if seen_slugs and slug in seen_slugs:
            continue
        if slug not in groups:
            groups[slug] = []
            fts_scores[slug] = -r["rank"]   # negate: higher = better
        groups[slug].append((r["bullet_id"], r["text"]))

    # 1.3x boost for nodes mentioned in the current session
    if session_ref_slugs:
        for _s in fts_scores:
            if _s in session_ref_slugs:
                fts_scores[_s] *= 1.3

    # Vector search
    vec_scores: dict[str, float] = {}
    if cfg is not None:
        with contextlib.suppress(Exception):
            from kg.vector_client import search_vector
            for slug, score in search_vector(query, cfg, k=limit * 3):
                if not slug.startswith(_INTERNAL_PREFIX):
                    vec_scores[slug] = float(score)

    # Load nodes to get vote scores + fill vector-only hits
    store_for_vec = FileStore(nodes_dir)
    vote_multipliers: dict[str, float] = {}
    for slug in list(groups) + [s for s in vec_scores if s not in groups]:
        if seen_slugs and slug in seen_slugs:
            continue
        node = store_for_vec.get(slug)
        if node is None:
            continue
        live = node.live_bullets
        if live:
            # Per-node vote multiplier: mean vote_score, centered so 0.5 → 1.0
            # Range: 0→0, 0.5→1.0, 1.0→2.0  (neutral bullets don't change rank)
            mean_vs = sum(b.vote_score() for b in live) / len(live)
            vote_multipliers[slug] = mean_vs * 2.0
        if slug not in groups:
            groups[slug] = [(b.id, b.text) for b in live]

    sorted_slugs = _rank_slugs(groups, fts_scores, vec_scores, db_path, cfg, vote_multipliers)

    # Rerank top results with cross-encoder (uses rerank_query or query)
    if cfg is not None and cfg.search.use_reranker and len(sorted_slugs) >= 2:
        _rq = rerank_query or query
        with contextlib.suppress(Exception):
            from kg.reranker import rerank as _rerank
            store_tmp = FileStore(nodes_dir)
            candidates: list[tuple[str, str]] = []
            for _slug in sorted_slugs[:min(len(sorted_slugs), limit * 2)]:
                _node = store_tmp.get(_slug)
                if _node is None:
                    continue
                _text = _node.title + " " + " ".join(b.text for b in _node.live_bullets[:5])
                candidates.append((_slug, _text))
            if len(candidates) >= 2:
                reranked = _rerank(_rq, candidates, cfg)
                reranked_order = [s for s, _ in reranked]
                # Keep any slugs not in candidates at the end
                rest = [s for s in sorted_slugs if s not in {s for s, _ in candidates}]
                sorted_slugs = reranked_order + rest

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

        # Filter bullets already shown in this session
        if fp is not None:
            bullets = [
                (bid, text) for bid, text in bullets
                if bid not in fp.ids and (not fp.text or text not in fp.text)
            ]
            if not bullets:
                continue

        # Explore hints from cross-refs and backlinks
        explore: set[str] = set()
        for _, text in bullets:
            for ref in _CROSSREF_RE.findall(text):
                if ref != slug and not ref.startswith(_INTERNAL_PREFIX):
                    explore.add(ref)
        for bl in get_backlinks(slug, db_path, cfg=cfg)[:4]:
            if not bl.startswith(_INTERNAL_PREFIX):
                explore.add(bl)

        hint = node.review_hint(threshold=review_threshold, bullet_count=len(live))

        ctx_node = ContextNode(
            slug=slug,
            title=node.title,
            score=fts_scores.get(slug, 0.0),  # raw for display; fusion used for ranking
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


def _rank_slugs(
    groups: dict[str, list[tuple[str, str]]],
    fts_scores: dict[str, float],
    vec_scores: dict[str, float],
    db_path: Path,
    cfg: KGConfig | None,
    vote_multipliers: dict[str, float] | None = None,
) -> list[str]:
    """Rank FTS-matched slugs using calibrated quantile fusion (or rank-based fallback).

    FTS scores: negated bm25, higher = better.
    Vector scores: cosine similarity 0-1, higher = better.
    Returns slugs sorted best-first.
    """
    fts_w = cfg.search.fts_weight if cfg is not None else 0.5
    vec_w = cfg.search.vector_weight if cfg is not None else 0.5
    dual_bonus = cfg.search.dual_match_bonus if cfg is not None else 0.1

    # Load calibration breakpoints
    fts_cal = get_calibration("fts", db_path, cfg)
    vec_cal = get_calibration("vector", db_path, cfg)
    fts_breaks = fts_cal[1] if fts_cal else None
    vec_breaks = vec_cal[1] if vec_cal else None

    # Pre-compute rank-based fallback percentiles for FTS
    fts_ranked = sorted(fts_scores.items(), key=lambda x: x[1], reverse=True)
    n_fts = len(fts_ranked)
    fts_rank_pos = {slug: i for i, (slug, _) in enumerate(fts_ranked)}

    slug_score: dict[str, float] = {}
    for slug in groups:
        fts_raw = fts_scores.get(slug, 0.0)
        vec_raw = vec_scores.get(slug, 0.0)

        # FTS quantile
        if fts_breaks and fts_raw > 0:
            fts_q = score_to_quantile(fts_raw, fts_breaks)
        elif n_fts > 1:
            pos = fts_rank_pos.get(slug, n_fts - 1)
            fts_q = 1.0 - pos / (n_fts - 1)
        else:
            fts_q = 1.0 if fts_raw > 0 else 0.0

        # Vector quantile (cosine is already 0-1, use as-is when no calibration)
        vec_q = score_to_quantile(vec_raw, vec_breaks) if vec_breaks and vec_raw > 0 else vec_raw

        bonus = dual_bonus if (fts_raw > 0 and vec_raw > 0) else 0.0
        base = fts_w * fts_q + vec_w * vec_q + bonus
        # Vote quality multiplier: neutral (0 votes) → 1.0, useful → >1.0, harmful → <1.0
        vm = vote_multipliers.get(slug, 1.0) if vote_multipliers else 1.0
        slug_score[slug] = base * vm

    return sorted(groups, key=lambda s: slug_score[s], reverse=True)
