"""Vector server: keeps embedding model + in-memory numpy index warm.

Started by supervisord alongside the watcher. Provides:
  GET  /health              → {"status": "ok", "n_vectors": N}
  POST /embed               → {"vectors": [[float, ...]]}
  POST /search              → {"results": [{"id": str, "score": float}]}
  POST /add                 → {"ok": true}
  POST /add_batch           → {"ok": true, "n": N}
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from kg.embedder import CachedEmbedder, get_embedder

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray


# ---------------------------------------------------------------------------
# Module-level singletons (set by run_vector_server before serving)
# ---------------------------------------------------------------------------

_embedder: CachedEmbedder | None = None
_index: VectorIndex | None = None


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------


class VectorIndex:
    """Thread-safe in-memory vector index using numpy cosine similarity."""

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.matrix: NDArray[np.float32] | None = None  # pyright: ignore[reportUndefinedVariable]
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """Return the number of vectors stored."""
        with self._lock:
            return len(self.ids)

    def add(self, node_id: str, vector: NDArray[np.float32]) -> None:  # pyright: ignore[reportUndefinedVariable]
        """Add a single vector (thread-safe)."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        with self._lock:
            if node_id in self.ids:
                # Update existing
                idx = self.ids.index(node_id)
                if self.matrix is not None:
                    self.matrix[idx] = vector
            else:
                self.ids.append(node_id)
                vec = vector.reshape(1, -1).astype(np.float32)
                if self.matrix is None:
                    self.matrix = vec
                else:
                    self.matrix = np.vstack([self.matrix, vec])

    def add_batch(self, ids: list[str], vectors: list[NDArray[np.float32]]) -> None:  # pyright: ignore[reportUndefinedVariable]
        """Bulk-load vectors (thread-safe). Replaces any existing ids."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        if not ids:
            return
        with self._lock:
            self.ids = list(ids)
            self.matrix = np.array(vectors, dtype=np.float32)

    def remove(self, node_id: str) -> None:
        """Remove a vector by id (thread-safe)."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        with self._lock:
            if node_id not in self.ids:
                return
            idx = self.ids.index(node_id)
            self.ids.pop(idx)
            if self.matrix is not None:
                new_matrix = np.delete(self.matrix, idx, axis=0)
                self.matrix = None if new_matrix.shape[0] == 0 else new_matrix

    def search(self, query_vector: NDArray[np.float32], k: int = 20) -> list[tuple[str, float]]:  # pyright: ignore[reportUndefinedVariable]
        """Cosine similarity search; returns top-k (id, score) sorted desc."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        with self._lock:
            if self.matrix is None or len(self.ids) == 0:
                return []
            # Normalise query
            q = query_vector.astype(np.float32)
            q_norm = np.linalg.norm(q)
            if q_norm > 0:
                q = q / q_norm
            # Normalise matrix rows
            norms = np.linalg.norm(self.matrix, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            normed = self.matrix / norms
            scores: NDArray[np.float32] = (normed @ q).astype(np.float32)
            top_k = min(k, len(self.ids))
            top_indices = np.argpartition(scores, -top_k)[-top_k:]
            top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]
            return [(self.ids[int(i)], float(scores[int(i)])) for i in top_indices]


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class KGVectorHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the vector server."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress default access log output."""

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))  # type: ignore[no-any-return]
        except (json.JSONDecodeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/health":
            n = _index.size if _index is not None else 0
            self._send_json({"status": "ok", "n_vectors": n})
        else:
            self._send_error(404, "not found")

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        body = self._read_json()
        if body is None:
            self._send_error(400, "invalid JSON")
            return

        if self.path == "/embed":
            self._handle_embed(body)
        elif self.path == "/search":
            self._handle_search(body)
        elif self.path == "/add":
            self._handle_add(body)
        elif self.path == "/add_batch":
            self._handle_add_batch(body)
        else:
            self._send_error(404, "not found")

    def _handle_embed(self, body: dict[str, Any]) -> None:
        texts: list[str] = body.get("texts", [])
        context: str = body.get("context", "")
        task_type: str = body.get("task_type", "doc")
        if not isinstance(texts, list) or not texts:
            self._send_error(400, "texts must be a non-empty list")
            return
        if _embedder is None:
            self._send_error(503, "embedder not initialised")
            return
        try:
            if task_type == "query":
                vectors = [_embedder.embed_query(t) for t in texts]
            else:
                contexts = [context] * len(texts)
                vectors = _embedder.embed_batch(texts, contexts)
            self._send_json({"vectors": [v.tolist() for v in vectors]})
        except Exception as exc:
            self._send_error(500, str(exc))

    def _handle_search(self, body: dict[str, Any]) -> None:
        raw_vector = body.get("vector")
        k: int = int(body.get("k", 20))
        if raw_vector is None or not isinstance(raw_vector, list):
            self._send_error(400, "vector must be a list of floats")
            return
        if _index is None:
            self._send_error(503, "index not initialised")
            return
        try:
            import numpy as np
            query_vec = np.array(raw_vector, dtype=np.float32)
            results = _index.search(query_vec, k=k)
            self._send_json({"results": [{"id": id_, "score": score} for id_, score in results]})
        except Exception as exc:
            self._send_error(500, str(exc))

    def _handle_add(self, body: dict[str, Any]) -> None:
        id_ = body.get("id")
        raw_vector = body.get("vector")
        if not isinstance(id_, str) or not id_:
            self._send_error(400, "id must be a non-empty string")
            return
        if raw_vector is None or not isinstance(raw_vector, list):
            self._send_error(400, "vector must be a list of floats")
            return
        if _index is None:
            self._send_error(503, "index not initialised")
            return
        try:
            import numpy as np
            _index.add(id_, np.array(raw_vector, dtype=np.float32))
            self._send_json({"ok": True})
        except Exception as exc:
            self._send_error(500, str(exc))

    def _handle_add_batch(self, body: dict[str, Any]) -> None:
        ids: list[str] = body.get("ids", [])
        raw_vectors: list[list[float]] = body.get("vectors", [])
        if not isinstance(ids, list) or not isinstance(raw_vectors, list):
            self._send_error(400, "ids and vectors must be lists")
            return
        if len(ids) != len(raw_vectors):
            self._send_error(400, "ids and vectors must have the same length")
            return
        if _index is None:
            self._send_error(503, "index not initialised")
            return
        try:
            import numpy as np
            vecs = [np.array(v, dtype=np.float32) for v in raw_vectors]
            _index.add_batch(ids, vecs)
            self._send_json({"ok": True, "n": len(ids)})
        except Exception as exc:
            self._send_error(500, str(exc))


# ---------------------------------------------------------------------------
# DB loader
# ---------------------------------------------------------------------------


def load_index_from_db(db_path: Path) -> tuple[list[str], NDArray[np.float32]] | tuple[list[str], None]:  # pyright: ignore[reportUndefinedVariable]
    """Load existing embeddings from the SQLite graph.db.

    Returns (ids, matrix) where matrix is (N, D) float32, or ([], None) if empty.
    """
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
        # Table may not exist yet
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

    matrix: NDArray[np.float32] = np.array(vecs, dtype=np.float32)  # pyright: ignore[reportUndefinedVariable]
    return ids, matrix


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_vector_server(cfg: KGConfig) -> None:  # pyright: ignore[reportUndefinedVariable]  # noqa: F821
    """Initialise embedder + index, then serve forever on cfg.server.vector_port."""
    global _embedder, _index  # noqa: PLW0603

    from kg.config import KGConfig as _KGConfig  # local import to break circular at module level

    if not isinstance(cfg, _KGConfig):
        msg = f"Expected KGConfig, got {type(cfg)}"
        raise TypeError(msg)

    cache_dir = cfg.index_dir / "embedding_cache"
    _embedder = get_embedder(cfg.embeddings.model, cache_dir)

    _index = VectorIndex()
    ids, matrix = load_index_from_db(cfg.db_path)
    if ids and matrix is not None:
        _index.add_batch(ids, [matrix[i] for i in range(len(ids))])

    host = "127.0.0.1"
    port = cfg.server.vector_port
    server = ThreadingHTTPServer((host, port), KGVectorHandler)
    print(f"vector-server listening on port {port}", flush=True)
    server.serve_forever()


# ---------------------------------------------------------------------------
# __main__
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m kg.vector_server <config_root>", flush=True)
        sys.exit(1)

    from kg.config import load_config

    _cfg = load_config(sys.argv[1])
    run_vector_server(_cfg)
