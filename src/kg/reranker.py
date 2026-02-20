"""Cross-encoder reranking: fastembed TextCrossEncoder with embedding cosine fallback.

Used by context.py to rerank search results for better relevance.
Module-level model cache avoids reloading on every call.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.config import KGConfig

_encoder: object | None = None
_encoder_model: str = ""


def _get_encoder(model: str) -> object | None:
    """Load (or return cached) fastembed TextCrossEncoder. Returns None if unavailable."""
    global _encoder, _encoder_model  # noqa: PLW0603
    if _encoder is not None and _encoder_model == model:
        return _encoder
    with contextlib.suppress(Exception):
        from kg.embedder import _suppress_ort_warnings
        _suppress_ort_warnings()
        from fastembed import TextCrossEncoder  # type: ignore[import-not-found]
        _encoder = TextCrossEncoder(model_name=model)
        _encoder_model = model
        return _encoder
    return None


def rerank(
    query: str,
    candidates: list[tuple[str, str]],
    cfg: KGConfig,
) -> list[tuple[str, float]]:
    """Score (id, text) pairs by relevance to query. Returns best-first list of (id, score).

    Tries fastembed TextCrossEncoder first; falls back to embedding cosine similarity.
    Returns original order with 0.0 scores if nothing is available.
    """
    if not candidates:
        return []

    # Try cross-encoder (fastembed TextCrossEncoder)
    encoder = _get_encoder(cfg.search.reranker_model)
    if encoder is not None:
        with contextlib.suppress(Exception):
            texts = [text for _, text in candidates]
            scores = list(encoder.rerank(query=query, documents=texts))  # type: ignore[attr-defined]
            return sorted(
                [(cid, float(s)) for (cid, _), s in zip(candidates, scores, strict=True)],
                key=lambda x: x[1],
                reverse=True,
            )

    # Fallback: embedding cosine similarity
    with contextlib.suppress(Exception):
        import numpy as np

        from kg.vector_client import embed

        texts = [text for _, text in candidates]
        q_vec = embed([query], cfg, task_type="query")[0]
        t_vecs = embed(texts, cfg, task_type="doc")
        scores_f: list[float] = []
        for tv in t_vecs:
            n_q = float(np.linalg.norm(q_vec))
            n_t = float(np.linalg.norm(tv))
            s = float(np.dot(q_vec, tv)) / (n_q * n_t) if n_q > 0 and n_t > 0 else 0.0
            scores_f.append(s)
        return sorted(
            [(cid, s) for (cid, _), s in zip(candidates, scores_f, strict=True)],
            key=lambda x: x[1],
            reverse=True,
        )

    # Nothing available — return original order
    return [(cid, 0.0) for cid, _ in candidates]
