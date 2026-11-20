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

import asyncio
import hashlib
import math
import os
from typing import Protocol, runtime_checkable
from uuid import UUID

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


# --- query embedding: off the event loop, and cached (OPS-14) ---------------
#
# §20.2's capacity model assumes retrieval costs single-digit milliseconds and that
# "the bottleneck is provider latency, not CPU". The query embed violated that: the
# remote embedder speaks HTTP with a *blocking* client, and the turn loop called it
# straight from an async generator, so one turn's embed round trip froze the event
# loop for every other connection in the process — the health probe included.
# Measured on the live stack (OPS-13, PLAN §8.50): 50 concurrent streams produced
# 21-41 finals, 6-24% refused requests and a p90 of 85-194 s.
#
# Two changes, each addressing a different half of the defect:
#
# * the call is **offloaded to a thread**, so the loop keeps serving while it waits.
#   The wait itself is still real — ``backend/embed/service.py`` holds a
#   ``threading.Lock`` around ``model.encode`` and there is one model on 2 vCPU, so
#   queries queue there by design. That is fine: a queue at the embed service is not
#   a queue at the API, and the difference is whether the rest of the process can
#   work while a query waits its turn.
# * a **repeated question costs no HTTP at all**. Support traffic repeats itself
#   ("where is my order"), and each repeat was buying the same vector again.

QUERY_CACHE_ENTRIES = int(os.environ.get("EMBEDDING_QUERY_CACHE_ENTRIES", "128"))


class QueryEmbeddingCache:
    """Bounded cache of question vectors, keyed by tenant, model, dimension and text.

    Tenant-scoped even though a vector is a pure function of (model, text): a shared
    key would let a cache *hit* tell one tenant that another had asked the same
    question, and a support question can itself be sensitive. The cost is that two
    tenants asking the same thing each pay once, which is the safe direction.

    Model key and dimension are in the key because §16.1 makes a model change a new
    vector space; a hit across spaces would return a plausible-looking vector that
    ranks nothing (the same reason ``RemoteEmbedder`` checks the service's model).

    In-process, like every other cache in this repo (``conversation/controls.py``
    says the same of the response cache): correct for one api worker, and §20.1
    specifies two, so each keeps its own. A shared cache would be a Redis round trip
    on every turn, which buys nothing for the loop stall this fixes.
    """

    max_entries: int = QUERY_CACHE_ENTRIES

    def __init__(self, max_entries: int | None = None) -> None:
        if max_entries is not None:
            self.max_entries = max_entries
        self._entries: dict[tuple[str, str, int, str], tuple[float, ...]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(
        tenant_id: str | UUID, model_key: str, dimension: int, text: str
    ) -> tuple[str, str, int, str]:
        return (
            str(tenant_id),
            model_key,
            int(dimension),
            hashlib.sha256(text.encode()).hexdigest(),
        )

    def get(self, key: tuple[str, str, int, str]) -> list[float] | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        # A copy per hit: the vector is handed to a query builder, and an aliased
        # list would let one turn's mutation corrupt every later hit.
        return list(entry)

    def put(self, key: tuple[str, str, int, str], vector: list[float]) -> None:
        if key not in self._entries and len(self._entries) >= self.max_entries:
            # Oldest first, like the response cache: a full flush would let a burst
            # of distinct questions evict every hot entry at once.
            self._entries.pop(next(iter(self._entries)), None)
        self._entries[key] = tuple(vector)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)


_query_cache = QueryEmbeddingCache()


def query_cache() -> QueryEmbeddingCache:
    return _query_cache


async def embed_query(
    provider: EmbeddingProvider, *, tenant_id: str | UUID, text: str
) -> list[float]:
    """Embed one question without blocking the event loop (OPS-14).

    The one call site that runs inside an async generator, so the one place a
    blocking HTTP client reached the loop. Sync routes (``knowledge`` preview,
    ``analytics`` gaps) do not need this: FastAPI already runs them in its
    threadpool, which is a thread — the same offload — chosen by the framework.

    The exception contract is unchanged: ``asyncio.to_thread`` re-raises whatever
    the provider raised, and the turn loop turns that into offline-plus-handover.
    """
    cache = _query_cache
    key = cache.key(tenant_id, provider.key, provider.dimension, text)
    hit = cache.get(key)
    if hit is not None:
        return hit
    vector = (await asyncio.to_thread(provider.embed, [text]))[0]
    cache.put(key, vector)
    return list(vector)


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
