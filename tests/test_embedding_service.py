"""The shared embedding service: contract, and the model-mismatch guard.

The FastAPI app is exercised with a stub model, because loading BGE-M3 costs
~30 s and 3.2 GiB; the real model is verified in ``test_bge_m3_provider.py`` and
was measured in PLAN 8.2. What matters here is the wire contract that keeps
ingestion and query in the same vector space.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE_PATH = Path(__file__).resolve().parents[1] / "backend" / "embed" / "service.py"


@pytest.fixture(scope="module")
def service():
    spec = importlib.util.spec_from_file_location("eva_embed_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["eva_embed_service"] = module
    spec.loader.exec_module(module)

    class _StubModel:
        def get_sentence_embedding_dimension(self) -> int:
            return 1024

        def encode(self, texts, **_kwargs):
            # Real encode() returns objects with .tolist(), so the stub must too.
            class _Vector(list):
                def tolist(self):
                    return list(self)

            vectors = []
            for text in texts:
                vector = _Vector([0.0] * 1024)
                vector[sum(text.encode()) % 1024] = 1.0
                vectors.append(vector)
            return vectors

    # Installed after import: the lifespan hook loads the real model on startup,
    # so the stub must be in place before TestClient enters.
    module._model = _StubModel()
    module._load = lambda: module._model
    return module


@pytest.fixture
def client(service):
    with TestClient(service.app) as test_client:
        yield test_client


def test_health_reports_the_model_key(client) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model"] == "bge-m3"


def test_embed_returns_the_configured_dimension(client) -> None:
    response = client.post("/embed", json={"texts": ["hello", "world"]})
    assert response.status_code == 200
    body = response.json()
    assert body["dimension"] == 1024
    assert len(body["vectors"]) == 2
    assert all(len(v) == 1024 for v in body["vectors"])


def test_blank_text_is_rejected(client) -> None:
    response = client.post("/embed", json={"texts": ["   "]})
    assert response.status_code == 422


def test_empty_batch_is_rejected(client) -> None:
    response = client.post("/embed", json={"texts": []})
    assert response.status_code == 422


def test_remote_embedder_rejects_a_model_mismatch() -> None:
    """Comparing vectors from two models is a silent correctness bug."""
    from app.retrieval.embedding import RemoteEmbedder

    embedder = RemoteEmbedder("http://embed.test:8080", key="bge-m3", dimension=1024)

    import httpx

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "some-other-model", "dimension": 1024, "vectors": [[0.0] * 1024]}

    original = httpx.post
    httpx.post = lambda *a, **k: _Response()
    try:
        with pytest.raises(ValueError, match="incomparable"):
            embedder.embed(["hello"])
    finally:
        httpx.post = original


def test_remote_embedder_rejects_a_dimension_mismatch() -> None:
    from app.retrieval.embedding import RemoteEmbedder

    embedder = RemoteEmbedder("http://embed.test:8080", key="bge-m3", dimension=1024)

    import httpx

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "bge-m3", "dimension": 384, "vectors": [[0.0] * 384]}

    original = httpx.post
    httpx.post = lambda *a, **k: _Response()
    try:
        with pytest.raises(ValueError, match="dimension"):
            embedder.embed(["hello"])
    finally:
        httpx.post = original


def test_remote_embedder_batches_above_the_service_cap() -> None:
    """A document with more chunks than the service cap must still ingest.

    ``store_chunks`` hands every chunk text to ``embed`` in one call, and the
    service rejects any request with more than ``MAX_BATCH`` texts (422). Before
    batching, that 422 was the *whole* document failing — measured on a 100-chunk
    (500-page) upload, which failed permanently with
    'Client error 422 Unprocessable Entity for url http://embed:8080/embed'.
    """
    from app.retrieval.embedding import MAX_EMBED_BATCH, RemoteEmbedder

    embedder = RemoteEmbedder("http://embed.test:8080", key="bge-m3", dimension=4,
                              batch_size=MAX_EMBED_BATCH)
    batches: list[int] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "bge-m3", "dimension": 4, "vectors": [[1.0, 0.0, 0.0, 0.0]] * last[0]}

    import httpx

    last = [0]

    def _post(url, **kwargs):
        n = len(kwargs["json"]["texts"])
        assert n <= MAX_EMBED_BATCH, (
            f"client sent {n} texts; the service cap is {MAX_EMBED_BATCH} and it 422s"
        )
        batches.append(n)
        last[0] = n
        return _Response()

    original = httpx.post
    httpx.post = _post
    try:
        vectors = embedder.embed([f"chunk {i}" for i in range(100)])
    finally:
        httpx.post = original

    assert len(vectors) == 100, "every chunk needs a vector"
    assert batches == [64, 36], f"expected two batches, got {batches}"


def test_remote_embedder_default_batch_is_within_the_service_cap() -> None:
    """The default must not be a value the service rejects on every request."""
    from app.retrieval.embedding import MAX_EMBED_BATCH, RemoteEmbedder

    embedder = RemoteEmbedder("http://embed.test:8080")
    assert 0 < embedder._batch_size <= MAX_EMBED_BATCH


def test_remote_embedder_refuses_a_batch_size_above_the_cap() -> None:
    """Configuring a too-large batch must fail loudly, not 422 per document."""
    import pytest as _pytest

    from app.retrieval.embedding import MAX_EMBED_BATCH, RemoteEmbedder

    with _pytest.raises(ValueError, match="exceeds the service cap"):
        RemoteEmbedder("http://embed.test:8080", batch_size=MAX_EMBED_BATCH + 1)


def test_remote_embedder_batch_order_is_preserved() -> None:
    """Batching must not reorder vectors: chunk N keeps chunk N's text."""
    from app.retrieval.embedding import RemoteEmbedder

    embedder = RemoteEmbedder("http://embed.test:8080", key="bge-m3", dimension=1,
                              batch_size=2)

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"model": "bge-m3", "dimension": 1,
                    "vectors": [[float(t.split()[-1])] for t in current[0]]}

    import httpx

    current = [[]]

    def _post(url, **kwargs):
        current[0] = kwargs["json"]["texts"]
        return _Response()

    original = httpx.post
    httpx.post = _post
    try:
        vectors = embedder.embed([f"chunk {i}" for i in range(5)])
    finally:
        httpx.post = original

    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0]
