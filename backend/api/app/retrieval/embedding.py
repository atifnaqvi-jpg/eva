"""Embedding provider interface (RAG-01 boundary, ARCHITECTURE 9.4).

Why an interface at all: the embedding model is the single most likely thing to
change (cost, quality, self-host vs API), and §16.1 makes a model change a
*re-ingestion*, not an in-place mutation — different dimension, different vector
space. So the provider exposes its dimension and its identity, and storage
refuses to mix vectors from two providers.

Real provider: ``BgeM3Provider`` (self-hosted, 1024 dims, ruled 2026-11-16).
Test provider: ``HashingEmbedder`` — deterministic, dependency-free, and
*actually different per model name*, which is what lets the model-switch test
prove a real swap rather than a fake one.
"""

import hashlib
import math
import os
from typing import Protocol, runtime_checkable

# The embedding service rejects any request with more texts than this
# (``backend/embed/service.py``: ``MAX_BATCH``). Read from the same variable so the
# two cannot drift; a client batch larger than the server's cap is a hard 422 for
# the whole document, not a partial result.
MAX_EMBED_BATCH = int(os.environ.get("EMBEDDING_MAX_BATCH", "64"))


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds text into a fixed-width float vector."""

    key: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashingEmbedder:
    """Deterministic bag-of-hashed-words embedder for tests.

    Not semantically meaningful, but it is a *consistent* vector space per
    ``key`` and it respects ``dimension``, so retrieval ordering, storage and
    dimension migration can all be exercised without a 3 GB model.
    """

    def __init__(self, key: str = "hashing-1024", dimension: int = 1024) -> None:
        self.key = key
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        # Non-negative weights on purpose: similarity then grows monotonically
        # with shared vocabulary, so a test asserting "the topical chunk ranks
        # first" is meaningful. Signing the hash instead would let two unrelated
        # documents score positively by collision.
        vector = [0.0] * self.dimension
        for word in text.lower().split():
            digest = hashlib.sha256(f"{self.key}:{word}".encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0:
            # Cosine distance is undefined for a zero vector; anchor it.
            vector[0] = 1.0
            return vector
        return [x / norm for x in vector]


class BgeM3Provider:
    """Self-hosted BGE-M3 (MIT), 1024 dimensions — the ruled production model.

    Import and model load are deferred: torch is only installed in the embedding
    worker image, never in the API image or CI, so importing this class must not
    require it.
    """

    key = "bge-m3"
    dimension = 1024

    def __init__(self, model_name: str = "BAAI/bge-m3", *, batch_size: int = 8) -> None:
        self._model_name = model_name
        self._batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, device="cpu")
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,  # cosine distance assumes unit vectors
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Reference cosine, used by tests and by the in-memory store."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class RemoteEmbedder:
    """Client for the shared embedding service (``backend/embed``).

    Preferred over ``BgeM3Provider`` anywhere that is not the embedding service
    itself: one loaded model for ingestion and query, instead of 3.2 GiB in every
    container that needs a vector.

    The service's ``model`` key is checked against ``expected_key``, because a
    silent model mismatch would compare vectors from two different spaces and
    produce plausible-looking but meaningless rankings.
    """

    def __init__(self, url: str, *, key: str = "bge-m3", dimension: int = 1024,
                 timeout: float = 300.0, batch_size: int = 8) -> None:
        # Batches small enough that one request finishes well inside ``timeout``: a
        # real 727-token chunk costs 5-15 s on the 2-vCPU embed container, so a
        # 64-text batch would run for ~10 minutes and time out. The cap is 64, but
        # the useful ceiling is set by the timeout, not the payload limit.
        if batch_size > MAX_EMBED_BATCH:
            raise ValueError(
                f"batch_size {batch_size} exceeds the service cap {MAX_EMBED_BATCH};"
                " every request would 422"
            )
        self.key = key
        self.dimension = dimension
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One request. ``texts`` must already be within the service's cap."""
        import httpx

        response = httpx.post(
            f"{self._url}/embed", json={"texts": texts}, timeout=self._timeout
        )
        response.raise_for_status()
        body = response.json()
        if body.get("model") != self.key:
            raise ValueError(
                f"embedding service reports model {body.get('model')!r}, expected "
                f"{self.key!r}: vectors would be incomparable"
            )
        if int(body.get("dimension", 0)) != self.dimension:
            raise ValueError(
                f"embedding service reports dimension {body.get('dimension')}, "
                f"expected {self.dimension}"
            )
        return body["vectors"]


def default_embedder(*, key: str = "bge-m3", dimension: int = 1024):
    """The embedder a process should use, per config.

    ``EMBEDDING_QUERY_BACKEND=remote`` is the deployment default: the API talks to
    the shared service. ``local`` exists for the embedding service itself and for
    offline runs.
    """
    from app.config import EMBEDDING_QUERY_BACKEND, EMBEDDING_QUERY_URL

    if EMBEDDING_QUERY_BACKEND == "remote":
        if not EMBEDDING_QUERY_URL:
            raise RuntimeError(
                "EMBEDDING_QUERY_BACKEND=remote requires EMBEDDING_QUERY_URL"
            )
        return RemoteEmbedder(EMBEDDING_QUERY_URL, key=key, dimension=dimension)
    return BgeM3Provider()
