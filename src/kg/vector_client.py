"""Vector client: proxy to vector server (fast) or direct fallback (cold)."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from kg.config import KGConfig

_TIMEOUT = 5  # seconds for all network calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def server_url(cfg: KGConfig) -> str:  # pyright: ignore[reportUndefinedVariable]
    """Return the base URL for the vector server."""
    return f"http://127.0.0.1:{cfg.server.vector_port}"


def _post(url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST JSON to url; returns None on connection errors, raises on others."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except (ConnectionRefusedError, urllib.error.URLError) as exc:
        # Treat connection-refused and timeout as "server not available"
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(cause, (ConnectionRefusedError, TimeoutError)):
            return None
        # URLError wrapping an OSError with errno ECONNREFUSED
        if isinstance(cause, OSError) and getattr(cause, "errno", None) in (111, 61):
            return None
        raise


def is_server_running(cfg: KGConfig) -> bool:  # pyright: ignore[reportUndefinedVariable]
    """Return True if the vector server is reachable."""
    url = server_url(cfg) + "/health"
    req = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------


def embed(
    texts: list[str],
    cfg: KGConfig,  # pyright: ignore[reportUndefinedVariable]
    *,
    context: str = "",
    task_type: str = "doc",
) -> list[NDArray[np.float32]]:  # pyright: ignore[reportUndefinedVariable]
    """Embed texts via server (fast) or local embedder (fallback).

    Returns a list of numpy arrays, one per input text.
    """
    try:
        import numpy as np
    except ImportError as e:
        msg = "numpy is required: pip install numpy"
        raise ImportError(msg) from e

    # Try server first
    result = _post(
        server_url(cfg) + "/embed",
        {"texts": texts, "context": context, "task_type": task_type},
    )
    if result is not None:
        return [np.array(v, dtype=np.float32) for v in result["vectors"]]

    # Fallback: direct computation
    from kg.embedder import get_embedder

    cache_dir = cfg.index_dir / "embedding_cache"
    embedder = get_embedder(cfg.embeddings.model, cache_dir)
    if task_type == "query":
        return [embedder.embed_query(t) for t in texts]
    contexts = [context] * len(texts)
    return embedder.embed_batch(texts, contexts)


# ---------------------------------------------------------------------------
# search_vector
# ---------------------------------------------------------------------------


def _load_all_vectors_from_db(db_path: Path) -> tuple[list[str], NDArray[np.float32]] | tuple[list[str], None]:  # pyright: ignore[reportUndefinedVariable]
    """Load all embeddings from SQLite for local fallback search."""
    try:
        import numpy as np
    except ImportError as e:
        msg = "numpy is required: pip install numpy"
        raise ImportError(msg) from e

    if not db_path.exists():
        return [], None

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT node_slug, vector FROM embeddings WHERE vector IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return [], None
    finally:
        conn.close()

    if not rows:
        return [], None

    ids: list[str] = []
    vecs: list[NDArray[np.float32]] = []  # pyright: ignore[reportUndefinedVariable]
    for slug, blob in rows:
        if blob is None:
            continue
        ids.append(slug)
        vecs.append(np.frombuffer(blob, dtype=np.float32))

    if not ids:
        return [], None

    return ids, np.array(vecs, dtype=np.float32)


def search_vector(
    query_text: str,
    cfg: KGConfig,  # pyright: ignore[reportUndefinedVariable]
    *,
    k: int = 20,
) -> list[tuple[str, float]]:
    """Semantic search: embed query then find nearest vectors.

    Returns list of (node_slug, score) tuples sorted by score descending.
    """
    try:
        import numpy as np
    except ImportError as e:
        msg = "numpy is required: pip install numpy"
        raise ImportError(msg) from e

    # Embed the query (tries server first, falls back to local)
    query_vec = embed([query_text], cfg, task_type="query")[0]

    # Try server search
    result = _post(
        server_url(cfg) + "/search",
        {"vector": query_vec.tolist(), "k": k},
    )
    if result is not None:
        return [(r["id"], float(r["score"])) for r in result["results"]]

    # Fallback: local numpy cosine similarity
    ids, matrix = _load_all_vectors_from_db(cfg.db_path)
    if not ids or matrix is None:
        return []

    q = query_vec.astype(np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm > 0:
        q = q / q_norm

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    normed = matrix / norms
    scores: NDArray[np.float32] = (normed @ q).astype(np.float32)  # pyright: ignore[reportUndefinedVariable]

    top_k = min(k, len(ids))
    top_indices = np.argpartition(scores, -top_k)[-top_k:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]
    return [(ids[int(i)], float(scores[int(i)])) for i in top_indices]
