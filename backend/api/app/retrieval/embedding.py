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
from typing import Protocol, runtime_checkable


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
