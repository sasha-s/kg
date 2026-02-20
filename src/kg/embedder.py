"""Embedding generation for kg: Gemini (default) or local fastembed, with diskcache."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from diskcache import Cache
    from fastembed import TextEmbedding
    from google import genai
    from numpy.typing import NDArray


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_model_name(model: str) -> str:
    """Sanitize a model string for use as a filesystem directory name."""
    return model.replace("/", "_").replace(":", "_")


# ---------------------------------------------------------------------------
# GeminiEmbedder
# ---------------------------------------------------------------------------

@dataclass
class GeminiEmbedder:
    """Sync embedder using Google Gemini text-embedding-004."""

    model: str = "gemini-embedding-001"
    dimensions: int = 768
    document_task_type: str = "RETRIEVAL_DOCUMENT"
    query_task_type: str = "RETRIEVAL_QUERY"
    api_key: str | None = None

    _client: genai.Client | None = field(default=None, repr=False, init=False)  # pyright: ignore[reportUndefinedVariable]

    @property
    def client(self) -> genai.Client:  # pyright: ignore[reportUndefinedVariable]
        """Get or create the Gemini client (lazy)."""
        if self._client is None:
            try:
                from google import genai as _genai
            except ImportError as e:
                msg = "google-genai is required for Gemini embeddings: pip install google-genai"
                raise ImportError(msg) from e
            key = self.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self._client = _genai.Client(api_key=key) if key else _genai.Client()
        return self._client

    def _contextual(self, text: str, context: str) -> str:
        if context:
            return f"{context}: {text}"
        return text

    def embed_document(self, text: str, context: str = "") -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a single document."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        full_text = self._contextual(text, context)
        result = self.client.models.embed_content(
            model=self.model,
            contents=full_text,
            config={
                "task_type": self.document_task_type,
                "output_dimensionality": self.dimensions,
            },
        )
        if result.embeddings is None:
            msg = "Gemini API returned no embeddings"
            raise RuntimeError(msg)
        return np.array(result.embeddings[0].values, dtype=np.float32)

    def embed_query(self, text: str) -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a search query."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        result = self.client.models.embed_content(
            model=self.model,
            contents=text,
            config={
                "task_type": self.query_task_type,
                "output_dimensionality": self.dimensions,
            },
        )
        if result.embeddings is None:
            msg = "Gemini API returned no embeddings"
            raise RuntimeError(msg)
        return np.array(result.embeddings[0].values, dtype=np.float32)

    def embed_batch(
        self,
        texts: list[str],
        contexts: list[str] | None = None,
    ) -> list[NDArray[np.float32]]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a batch of documents."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        if not texts:
            return []
        if contexts is None:
            contexts = [""] * len(texts)
        elif len(contexts) != len(texts):
            msg = "contexts must have the same length as texts"
            raise ValueError(msg)
        full_texts = [self._contextual(t, c) for t, c in zip(texts, contexts, strict=True)]
        result = self.client.models.embed_content(
            model=self.model,
            contents=full_texts,  # type: ignore[arg-type]  # SDK types too strict: list[str] is valid
            config={
                "task_type": self.document_task_type,
                "output_dimensionality": self.dimensions,
            },
        )
        if result.embeddings is None:
            msg = "Gemini API returned no embeddings"
            raise RuntimeError(msg)
        return [np.array(emb.values, dtype=np.float32) for emb in result.embeddings]


# ---------------------------------------------------------------------------
# FastEmbedEmbedder
# ---------------------------------------------------------------------------

@dataclass
class FastEmbedEmbedder:
    """Local embedder using fastembed TextEmbedding (ONNX, no API key needed)."""

    model: str = "BAAI/bge-small-en-v1.5"
    dimensions: int = 384  # bge-small-en-v1.5 is 384-dim
    _fe_model: TextEmbedding | None = field(default=None, repr=False, init=False)  # pyright: ignore[reportUndefinedVariable]

    @property
    def _model(self) -> TextEmbedding:  # pyright: ignore[reportUndefinedVariable]
        """Get or create the fastembed model (lazy)."""
        if self._fe_model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                msg = "fastembed is required for local embeddings: pip install fastembed"
                raise ImportError(msg) from e
            self._fe_model = TextEmbedding(self.model)
        return self._fe_model

    def _contextual(self, text: str, context: str) -> str:
        if context:
            return f"{context}: {text}"
        return text

    def embed_document(self, text: str, context: str = "") -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a single document."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        full_text = self._contextual(text, context)
        embeddings = list(self._model.embed([full_text]))
        return np.array(embeddings[0], dtype=np.float32)

    def embed_query(self, text: str) -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a search query."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        embeddings = list(self._model.embed([text]))
        return np.array(embeddings[0], dtype=np.float32)

    def embed_batch(
        self,
        texts: list[str],
        contexts: list[str] | None = None,
    ) -> list[NDArray[np.float32]]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a batch of documents."""
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        if not texts:
            return []
        if contexts is None:
            contexts = [""] * len(texts)
        elif len(contexts) != len(texts):
            msg = "contexts must have the same length as texts"
            raise ValueError(msg)
        full_texts = [self._contextual(t, c) for t, c in zip(texts, contexts, strict=True)]
        embeddings = list(self._model.embed(full_texts))
        return [np.array(emb, dtype=np.float32) for emb in embeddings]


# ---------------------------------------------------------------------------
# CachedEmbedder
# ---------------------------------------------------------------------------

@dataclass
class CachedEmbedder:
    """Wraps a GeminiEmbedder or FastEmbedEmbedder with diskcache on disk.

    Cache key: sha256 of "{task_type}:{context}:{text}:{dimensions}"
    Cache path: {cache_dir}/{safe_model_name}/
    Stored as raw float32 bytes (.tobytes() / np.frombuffer).
    """

    embedder: GeminiEmbedder | FastEmbedEmbedder
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "kg" / "embeddings")
    _disk_cache: Cache | None = field(default=None, repr=False, init=False)  # pyright: ignore[reportUndefinedVariable]

    @property
    def dimensions(self) -> int:
        """Embedding dimensions delegated to the inner embedder."""
        return self.embedder.dimensions

    @property
    def _cache(self) -> Cache:  # pyright: ignore[reportUndefinedVariable]
        """Get or create the diskcache instance (lazy, model-specific directory)."""
        if self._disk_cache is None:
            try:
                from diskcache import Cache
            except ImportError as e:
                msg = "diskcache is required for caching: pip install diskcache"
                raise ImportError(msg) from e
            model_dir = self.cache_dir / _safe_model_name(self.embedder.model)
            model_dir.mkdir(parents=True, exist_ok=True)
            self._disk_cache = Cache(str(model_dir))
        return self._disk_cache

    def _cache_key(self, text: str, context: str, task_type: str) -> str:
        """Build a sha256 cache key."""
        raw = f"{task_type}:{context}:{text}:{self.dimensions}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _from_bytes(self, data: bytes) -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        try:
            import numpy as np
        except ImportError as e:
            msg = "numpy is required: pip install numpy"
            raise ImportError(msg) from e
        return np.frombuffer(data, dtype=np.float32)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_document(self, text: str, context: str = "") -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a document, returning cached result if available."""
        key = self._cache_key(text, context, "document")
        cached = self._cache.get(key)
        if cached is not None:
            return self._from_bytes(cached)  # type: ignore[arg-type]
        embedding = self.embedder.embed_document(text, context)
        self._cache.set(key, embedding.tobytes())
        return embedding

    def embed_query(self, text: str) -> NDArray[np.float32]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a search query, returning cached result if available."""
        key = self._cache_key(text, "", "query")
        cached = self._cache.get(key)
        if cached is not None:
            return self._from_bytes(cached)  # type: ignore[arg-type]
        embedding = self.embedder.embed_query(text)
        self._cache.set(key, embedding.tobytes())
        return embedding

    def embed_batch(
        self,
        texts: list[str],
        contexts: list[str] | None = None,
    ) -> list[NDArray[np.float32]]:  # pyright: ignore[reportUndefinedVariable]
        """Embed a batch of documents, using cache per item."""
        if not texts:
            return []
        if contexts is None:
            contexts = [""] * len(texts)

        results: list[NDArray[np.float32] | None] = [None] * len(texts)  # pyright: ignore[reportUndefinedVariable]
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []
        uncached_contexts: list[str] = []

        for i, (text, ctx) in enumerate(zip(texts, contexts, strict=True)):
            key = self._cache_key(text, ctx, "document")
            cached = self._cache.get(key)
            if cached is not None:
                results[i] = self._from_bytes(cached)  # type: ignore[arg-type]
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)
                uncached_contexts.append(ctx)

        if uncached_texts:
            new_embeddings = self.embedder.embed_batch(uncached_texts, uncached_contexts)
            for idx, text, ctx, emb in zip(
                uncached_indices, uncached_texts, uncached_contexts, new_embeddings, strict=True
            ):
                key = self._cache_key(text, ctx, "document")
                self._cache.set(key, emb.tobytes())
                results[idx] = emb

        return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def is_local_model(model_str: str) -> bool:
    """Return True if model_str is a local (non-API) model.

    Gemini models are prefixed with ``gemini:``.
    OpenAI models are prefixed with ``openai:``.
    Everything else (bare model name or ``fastembed:`` prefix) is local.
    """
    lower = model_str.lower()
    return not lower.startswith(("gemini:", "openai:"))


def get_embedder(model: str, cache_dir: Path) -> CachedEmbedder:
    """Factory: create a CachedEmbedder for the given model string.

    Supported prefixes:
    - ``gemini:<model>``  → GeminiEmbedder (reads GEMINI_API_KEY or GOOGLE_API_KEY)
    - ``fastembed:<model>`` or bare name → FastEmbedEmbedder
    """
    lower = model.lower()
    if lower.startswith("gemini:"):
        bare = model[len("gemini:"):]
        base: GeminiEmbedder | FastEmbedEmbedder = GeminiEmbedder(model=bare)
    elif lower.startswith("fastembed:"):
        bare = model[len("fastembed:"):]
        base = FastEmbedEmbedder(model=bare)
    else:
        # No prefix — treat as fastembed model name
        base = FastEmbedEmbedder(model=model)
    return CachedEmbedder(embedder=base, cache_dir=cache_dir)
