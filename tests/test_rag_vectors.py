"""RAG-01 / RAG-03: VectorStore behaviour against real Postgres + pgvector.

No mocks: these run the actual pgvector column, cosine operator and HNSW index.
The embedding model is the deterministic ``HashingEmbedder`` so the assertions are
stable; the production BGE-M3 provider is verified separately by the throughput
probe, because torch is deliberately absent from the API image and CI.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import _admin_dsn
from app.db.vector_store import PgVectorStore
from app.retrieval.embedding import HashingEmbedder


@pytest.fixture
def app_session(migrated_db: str) -> Iterator[Session]:
    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + migrated_db
    )
    session = Session(engine)
    _SEEDED.clear()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()
        # upsert() writes through this session, which commits, so the seeded
        # tenants outlive the test unless they are removed explicitly.
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(_SEEDED),))


_SEEDED: list[uuid.UUID] = []


def _seed_tenant_with_version(conn, dim: int = 1024) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a tenant, source, document and version; return their ids."""
    """Insert a tenant, source, document and version; return their ids."""
    tenant_id, source_id, document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _SEEDED.append(tenant_id)
    conn.execute(
        "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, "vector-tenant")
    )
    conn.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s, %s, 'upload')",
        (source_id, tenant_id),
    )
    conn.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title) VALUES (%s,%s,%s,'Doc')",
        (document_id, tenant_id, source_id),
    )
    version_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
        " content_hash, status) VALUES (%s,%s,%s,1,'h','ready')",
        (version_id, tenant_id, document_id),
    )
    return tenant_id, source_id, version_id


def _insert_chunk(conn, tenant_id, version_id, ordinal: int, body: str) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text)"
        " VALUES (%s,%s,%s,%s,%s)",
        (chunk_id, tenant_id, version_id, ordinal, body),
    )
    return chunk_id


def test_upsert_stores_vectors_and_search_ranks_nearest_first(migrated_db: str, app_session) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id, _src, version_id = _seed_tenant_with_version(conn)
        ids = {
            "refund": _insert_chunk(conn, tenant_id, version_id, 0, "refund policy for shoes"),
            "shipping": _insert_chunk(conn, tenant_id, version_id, 1, "shipping windows and delivery"),
            "warranty": _insert_chunk(conn, tenant_id, version_id, 2, "warranty coverage period"),
        }

    embedder = HashingEmbedder(dimension=1024)
    store = PgVectorStore(app_session, dimension=1024)
    app_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})

    records = [
        VectorRecordFactory(chunk_id=cid, body=body, version_id=version_id, embedder=embedder)
        for cid, body in (
            (ids["refund"], "refund policy for shoes"),
            (ids["shipping"], "shipping windows and delivery"),
            (ids["warranty"], "warranty coverage period"),
        )
    ]
    assert store.upsert(tenant_id, records) == 3

    hits = store.search(
        tenant_id,
        embedder.embed(["refund policy shoes"])[0],
        version_ids=[version_id],
        limit=3,
    )
    assert hits, "search must return the upserted chunks"
    assert hits[0].chunk_id == ids["refund"], "the topical chunk must rank first"
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_search_never_crosses_tenant_boundaries(migrated_db: str, app_session) -> None:
    """The store filters explicitly, and RLS backs it up."""
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_a, _sa, version_a = _seed_tenant_with_version(conn)
        tenant_b, _sb, version_b = _seed_tenant_with_version(conn)
        _insert_chunk(conn, tenant_a, version_a, 0, "secret refund terms for tenant a")
        _insert_chunk(conn, tenant_b, version_b, 0, "secret refund terms for tenant b")

    embedder = HashingEmbedder(dimension=1024)
    store = PgVectorStore(app_session, dimension=1024)
    app_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)})

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        row = conn.execute(
            "SELECT id, text FROM chunks WHERE tenant_id = %s", (tenant_a,)
        ).fetchone()
        other = conn.execute(
            "SELECT id, text FROM chunks WHERE tenant_id = %s", (tenant_b,)
        ).fetchone()
    store.upsert(tenant_a, [VectorRecordFactory(row[0], row[1], version_a, embedder)])
    store.upsert(tenant_b, [VectorRecordFactory(other[0], other[1], version_b, embedder)])

    hits = store.search(
        tenant_a, embedder.embed(["secret refund terms"])[0],
        version_ids=[version_a, version_b], limit=10,
    )
    assert hits, "tenant A should see its own chunk"
    assert all(h.document_version_id == version_a for h in hits)
    assert not any("tenant b" in h.text for h in hits)


def test_search_is_scoped_to_the_requested_versions(migrated_db: str, app_session) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id, _src, version_a = _seed_tenant_with_version(conn)
        chunk_a = _insert_chunk(conn, tenant_id, version_a, 0, "old refund policy")
        version_b = uuid.uuid4()
        conn.execute(
            "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
            " content_hash, status) SELECT %s, tenant_id, document_id, 2, 'h2', 'ready'"
            " FROM document_versions WHERE id = %s",
            (version_b, version_a),
        )
        chunk_b = _insert_chunk(conn, tenant_id, version_b, 0, "new refund policy")

    embedder = HashingEmbedder(dimension=1024)
    store = PgVectorStore(app_session, dimension=1024)
    app_session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    store.upsert(tenant_id, [
        VectorRecordFactory(chunk_a, "old refund policy", version_a, embedder),
        VectorRecordFactory(chunk_b, "new refund policy", version_b, embedder),
    ])

    only_b = store.search(
        tenant_id, embedder.embed(["refund policy"])[0], version_ids=[version_b], limit=10
    )
    assert [h.document_version_id for h in only_b] == [version_b]


def test_upsert_rejects_a_vector_of_the_wrong_dimension(app_session) -> None:
    store = PgVectorStore(app_session, dimension=1024)
    record = VectorRecordFactory(uuid.uuid4(), "text", uuid.uuid4(), HashingEmbedder(dimension=384))
    with pytest.raises(ValueError, match="dims"):
        store.upsert(uuid.uuid4(), [record])


def VectorRecordFactory(chunk_id, body, version_id, embedder):  # noqa: N802
    from app.retrieval.store import VectorRecord

    return VectorRecord(
        chunk_id=chunk_id,
        document_version_id=version_id,
        text=body,
        embedding=embedder.embed([body])[0],
    )
