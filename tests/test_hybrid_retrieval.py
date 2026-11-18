"""RAG-03/04/05/06/07: hybrid retrieval against real Postgres + pgvector.

The assertions are about *order and refusals*, not about model quality: the
embedder is the deterministic ``HashingEmbedder`` so dense ranking is stable
without torch. Dense cosine and sparse ``ts_rank_cd`` both run for real.

The three negative cases the WP requires are here by name:
  * cross-tenant candidate leakage
  * a draft version reached through the published path
  * a below-floor query that declines
"""

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import _admin_dsn
from app.db.vector_store import PgVectorStore
from app.knowledge import service as knowledge
from app.retrieval import pipeline
from app.retrieval.embedding import HashingEmbedder
from app.retrieval.hybrid import (
    Candidate,
    RankedChunk,
    dense_candidates,
    reciprocal_rank_fusion,
    sparse_candidates,
)
from app.retrieval.rerank import apply_rerank
from app.retrieval.store import VectorRecord

DIM = 1024
_SEEDED: list[uuid.UUID] = []


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
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(_SEEDED),))


def _tenant(conn, name: str = "hybrid-tenant") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    _SEEDED.append(tenant_id)
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, name))
    return tenant_id


def _bot(conn, tenant_id: uuid.UUID, *, threshold: float = 0.55) -> uuid.UUID:
    bot_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO bots (id, tenant_id, public_id, name, confidence_threshold)"
        " VALUES (%s,%s,%s,'Bot',%s)",
        (bot_id, tenant_id, f"bot-{bot_id.hex[:8]}", threshold),
    )
    return bot_id


def _source(conn, tenant_id: uuid.UUID, kind: str = "upload") -> uuid.UUID:
    source_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,%s)",
        (source_id, tenant_id, kind),
    )
    return source_id


def _version(
    conn, tenant_id: uuid.UUID, source_id: uuid.UUID, *,
    title: str = "Doc", status: str = "ready", version_no: int = 1,
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title) VALUES (%s,%s,%s,%s)",
        (document_id, tenant_id, source_id, title),
    )
    version_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
        " content_hash, status) VALUES (%s,%s,%s,%s,%s,%s)",
        (version_id, tenant_id, document_id, version_no, f"h{version_id.hex[:6]}", status),
    )
    return document_id, version_id


def _chunk(conn, tenant_id, version_id, ordinal: int, body: str) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, metadata)"
        " VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
        (chunk_id, tenant_id, version_id, ordinal, body,
         json.dumps({"section": "S", "ordinal": ordinal})),
    )
    return chunk_id


def _embed(session: Session, tenant_id, version_id, pairs) -> None:
    """Give the chunks real vectors through the production upsert path."""
    embedder = HashingEmbedder(dimension=DIM)
    store = PgVectorStore(session, dimension=DIM)
    session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )
    store.upsert(
        tenant_id,
        [
            VectorRecord(
                chunk_id=chunk_id,
                document_version_id=version_id,
                text=body,
                embedding=embedder.embed([body])[0],
            )
            for chunk_id, body in pairs
        ],
    )


class _FixedReranker:
    """Deterministic reranker: scores by presence of a marker token."""

    def __init__(self, marker: str, high: float = 0.9, low: float = 0.1):
        self.marker, self.high, self.low = marker, high, low

    def score(self, question: str, texts: list[str]) -> list[float]:
        return [self.high if self.marker in t else self.low for t in texts]


class _BrokenReranker:
    def score(self, question: str, texts: list[str]) -> list[float]:
        raise RuntimeError("reranker provider is down")


# --- fusion arithmetic (no database) ----------------------------------------


def test_rrf_rewards_agreement_between_retrievers() -> None:
    shared = Candidate(uuid.uuid4(), uuid.uuid4(), "shared", 0.9, {})
    shared_sparse = Candidate(shared.chunk_id, shared.document_version_id, "shared", 4.2, {})
    dense_only = Candidate(uuid.uuid4(), uuid.uuid4(), "dense", 0.95, {})
    sparse_only = Candidate(uuid.uuid4(), uuid.uuid4(), "sparse", 3.0, {})

    ranked = reciprocal_rank_fusion([dense_only, shared], [sparse_only, shared_sparse])
    assert ranked[0].chunk_id == shared.chunk_id, (
        "a chunk both retrievers return must outrank one only a single list returns"
    )
    assert ranked[0].dense_score == 0.9 and ranked[0].sparse_score == 4.2


def test_rrf_order_is_total_and_deterministic() -> None:
    """Identical inputs in a different dict order must produce one order."""
    ids = [uuid.uuid4() for _ in range(3)]
    dense = [Candidate(i, uuid.uuid4(), f"t{n}", 0.5, {"ordinal": n}) for n, i in enumerate(ids)]
    first = [c.chunk_id for c in reciprocal_rank_fusion(dense, [])]
    second = [c.chunk_id for c in reciprocal_rank_fusion(list(dense), [])]
    assert first == second == ids


def test_rrf_ties_break_on_dense_rank_then_ordinal() -> None:
    a = Candidate(uuid.uuid4(), uuid.uuid4(), "a", 0.5, {"ordinal": 5})
    b = Candidate(uuid.uuid4(), uuid.uuid4(), "b", 0.5, {"ordinal": 1})
    ranked = reciprocal_rank_fusion([a, b], [])
    assert [c.text for c in ranked] == ["a", "b"], "dense rank 1 must win the tie"


def test_rrf_handles_one_empty_list() -> None:
    only = Candidate(uuid.uuid4(), uuid.uuid4(), "only", 0.7, {})
    assert [c.text for c in reciprocal_rank_fusion([], [only])] == ["only"]
    assert [c.text for c in reciprocal_rank_fusion([only], [])] == ["only"]
    assert reciprocal_rank_fusion([], []) == []


# --- the negative cases -----------------------------------------------------


def test_candidates_never_leak_across_tenants(migrated_db: str, app_session) -> None:
    """Tenant B's chunks must not appear in A's candidates, even when A passes
    B's version id — the tenant predicate is independent of the version filter."""
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_a = _tenant(conn, "tenant-a")
        tenant_b = _tenant(conn, "tenant-b")
        src_a = _source(conn, tenant_a)
        src_b = _source(conn, tenant_b)
        _, version_a = _version(conn, tenant_a, src_a)
        _, version_b = _version(conn, tenant_b, src_b)
        chunk_a = _chunk(conn, tenant_a, version_a, 0, "refund policy for tenant a")
        chunk_b = _chunk(conn, tenant_b, version_b, 0, "refund policy for tenant b")

    _embed(app_session, tenant_a, version_a, [(chunk_a, "refund policy for tenant a")])
    _embed(app_session, tenant_b, version_b, [(chunk_b, "refund policy for tenant b")])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)}
    )

    vector = HashingEmbedder(dimension=DIM).embed(["refund policy"])[0]
    dense = dense_candidates(
        app_session, tenant_a, vector, version_ids=[version_a, version_b], limit=10
    )
    sparse = sparse_candidates(
        app_session, tenant_a, "refund policy", version_ids=[version_a, version_b], limit=10
    )

    assert dense, "tenant A must still see its own chunk"
    assert {c.chunk_id for c in dense} == {chunk_a}
    assert all(c.chunk_id != chunk_b for c in sparse)
    assert not any("tenant b" in c.text for c in dense + sparse)


def test_explicit_tenant_predicate_holds_even_without_rls(
    migrated_db: str, app_session
) -> None:
    """The explicit ``tenant_id`` predicate is defence in depth, and must work alone.

    RLS is the actual enforcement for the app role, which means a test through
    ``app_session`` passes even if the predicate is deleted — it proves RLS, not
    the query. This connects as the *owner* (superuser), which bypasses RLS, so
    the only thing that can keep tenant B out is the predicate itself.
    """
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_a = _tenant(conn, "owner-tenant-a")
        tenant_b = _tenant(conn, "owner-tenant-b")
        _, version_a = _version(conn, tenant_a, _source(conn, tenant_a))
        _, version_b = _version(conn, tenant_b, _source(conn, tenant_b))
        chunk_a = _chunk(conn, tenant_a, version_a, 0, "alpha refund policy")
        chunk_b = _chunk(conn, tenant_b, version_b, 0, "beta refund policy")

    embedder = HashingEmbedder(dimension=DIM)
    owner_engine = create_engine(
        "postgresql+psycopg://eva:changeme@127.0.0.1:5432/" + migrated_db
    )
    # The owner connection writes the vectors directly (no RLS on a superuser).
    with owner_engine.begin() as conn:
        for chunk_id, body, tenant_id, version_id in (
            (chunk_a, "alpha refund policy", tenant_a, version_a),
            (chunk_b, "beta refund policy", tenant_b, version_b),
        ):
            conn.execute(
                text(
                    "UPDATE chunks SET embedding = CAST(:e AS vector)"
                    " WHERE id = :c AND tenant_id = :t"
                ),
                {
                    "e": "[" + ",".join(f"{x:.6f}" for x in embedder.embed([body])[0]) + "]",
                    "c": str(chunk_id),
                    "t": str(tenant_id),
                },
            )

    with Session(owner_engine) as owner:
        dense = dense_candidates(
            owner, tenant_a, embedder.embed(["refund policy"])[0],
            version_ids=[version_a, version_b], limit=10,
        )
        sparse = sparse_candidates(
            owner, tenant_a, "refund policy", version_ids=[version_a, version_b], limit=10,
        )
    owner_engine.dispose()

    assert [c.chunk_id for c in dense] == [chunk_a], (
        "with RLS bypassed, the explicit tenant predicate alone must exclude tenant B"
    )
    assert [c.chunk_id for c in sparse] == [chunk_a]


def test_draft_version_is_unreachable_through_the_published_path(
    migrated_db: str, app_session
) -> None:
    """Publish v1, leave v2 as a draft, then retrieve through the bot pointer."""
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        bot_id = _bot(conn, tenant_id)
        source_id = _source(conn, tenant_id)
        _doc, published = _version(conn, tenant_id, source_id, title="Live", version_no=1)
        _doc2, draft = _version(conn, tenant_id, source_id, title="Draft", version_no=1)
        live_chunk = _chunk(conn, tenant_id, published, 0, "refunds within thirty days")
        draft_chunk = _chunk(conn, tenant_id, draft, 0, "refunds within two days")

    _embed(app_session, tenant_id, published, [(live_chunk, "refunds within thirty days")])
    _embed(app_session, tenant_id, draft, [(draft_chunk, "refunds within two days")])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )
    # The served scope is the bot's active knowledge set (RAG-11), not a version column.
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[published], label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)
    # No commit here on purpose: the tenant GUC is transaction-local
    # (`set_config(..., true)`), so committing would drop the scope this test reads in.

    versions = pipeline.active_version_ids(app_session, tenant_id, bot_id)
    assert versions == [published], "only the published version is served"

    vector = HashingEmbedder(dimension=DIM).embed(["refunds"])[0]
    result = pipeline.retrieve(
        app_session, tenant_id, vector, "refunds",
        version_ids=versions,
        config=pipeline.BotRetrievalConfig(confidence_threshold=0.0, rerank_enabled=False),
    )
    texts = [c.text for c in result.chunks]
    assert any("thirty days" in t for t in texts)
    assert not any("two days" in t for t in texts), (
        "a draft version must never be visible through the published pointer"
    )


def test_below_floor_query_declines_instead_of_answering(
    migrated_db: str, app_session
) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        bot_id = _bot(conn, tenant_id, threshold=0.99)
        source_id = _source(conn, tenant_id)
        _doc, version_id = _version(conn, tenant_id, source_id)
        chunk = _chunk(conn, tenant_id, version_id, 0, "refunds within thirty days")

    _embed(app_session, tenant_id, version_id, [(chunk, "refunds within thirty days")])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )

    config = pipeline.bot_config(app_session, tenant_id, bot_id)
    assert config.confidence_threshold == 0.99

    vector = HashingEmbedder(dimension=DIM).embed(["refunds"])[0]
    result = pipeline.retrieve(
        app_session, tenant_id, vector, "refunds",
        version_ids=[version_id], config=config,
    )
    assert result.declined, "an unmeetable floor must decline, never answer"
    assert result.reason == pipeline.DECLINE_BELOW_FLOOR
    assert result.best_score < result.threshold


def test_no_candidates_declines_with_a_distinct_reason(migrated_db: str, app_session) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        bot_id = _bot(conn, tenant_id)

    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == []

    result = pipeline.retrieve(
        app_session, tenant_id, HashingEmbedder(dimension=DIM).embed(["anything"])[0],
        "anything", version_ids=[],
    )
    assert result.declined and result.reason == pipeline.DECLINE_NO_CANDIDATES


# --- the positive path ------------------------------------------------------


def test_hybrid_retrieval_returns_both_lexical_and_semantic_hits(
    migrated_db: str, app_session
) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        source_id = _source(conn, tenant_id)
        _doc, version_id = _version(conn, tenant_id, source_id, title="Handbook")
        pairs = [
            ("returns", "Customers may request a refund within thirty days"),
            ("shipping", "Standard delivery takes three to five business days"),
            ("warranty", "All electronics carry a twelve month warranty"),
        ]
        ids = [(name, _chunk(conn, tenant_id, version_id, n, body))
               for n, (name, body) in enumerate(pairs)]

    _embed(app_session, tenant_id, version_id, [(cid, body) for (_, cid), (_, body) in zip(ids, pairs)])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )

    vector = HashingEmbedder(dimension=DIM).embed(["refund thirty days"])[0]
    result = pipeline.retrieve(
        app_session, tenant_id, vector, "refund thirty days",
        version_ids=[version_id],
        config=pipeline.BotRetrievalConfig(confidence_threshold=0.0, rerank_enabled=False),
    )
    assert not result.declined
    assert result.chunks[0].text.startswith("Customers may request a refund"), (
        "the lexical+semantic match must rank first"
    )

    cite = pipeline.citations(app_session, tenant_id, result.chunks[:1])
    assert cite[0]["document"] == "Handbook"
    assert cite[0]["section"] == "S"


def test_reranker_reorders_and_marks_the_response(migrated_db: str, app_session) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        source_id = _source(conn, tenant_id)
        _doc, version_id = _version(conn, tenant_id, source_id)
        first = _chunk(conn, tenant_id, version_id, 0, "refunds within thirty days")
        second = _chunk(conn, tenant_id, version_id, 1, "warranty covers electronics")

    _embed(app_session, tenant_id, version_id, [
        (first, "refunds within thirty days"), (second, "warranty covers electronics"),
    ])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )

    vector = HashingEmbedder(dimension=DIM).embed(["refunds"])[0]
    result = pipeline.retrieve(
        app_session, tenant_id, vector, "refunds",
        version_ids=[version_id],
        config=pipeline.BotRetrievalConfig(confidence_threshold=0.0),
        reranker=_FixedReranker(marker="warranty"),
    )
    assert result.reranked is True
    assert result.chunks[0].text.startswith("warranty"), "rerank order must be honoured"


def test_broken_reranker_degrades_to_fused_order(migrated_db: str, app_session) -> None:
    """A rerank outage must not fail a customer's question (RETRIEVAL.md 6)."""
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        source_id = _source(conn, tenant_id)
        _doc, version_id = _version(conn, tenant_id, source_id)
        chunk = _chunk(conn, tenant_id, version_id, 0, "refunds within thirty days")

    _embed(app_session, tenant_id, version_id, [(chunk, "refunds within thirty days")])
    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )

    vector = HashingEmbedder(dimension=DIM).embed(["refunds"])[0]
    result = pipeline.retrieve(
        app_session, tenant_id, vector, "refunds",
        version_ids=[version_id],
        config=pipeline.BotRetrievalConfig(confidence_threshold=0.0),
        reranker=_BrokenReranker(),
    )
    assert result.chunks, "the fused order must survive a reranker outage"
    assert result.reranked is False, "the degradation must be visible to callers"


def test_rerank_can_be_disabled_per_tenant(migrated_db: str, app_session) -> None:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant_id = _tenant(conn)
        bot_id = _bot(conn, tenant_id)

    app_session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )
    assert pipeline.bot_config(app_session, tenant_id, bot_id).rerank_enabled is True

    app_session.execute(
        text("UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
        {"s": json.dumps({"retrieval": {"rerank": False}}), "t": str(tenant_id)},
    )
    assert pipeline.bot_config(app_session, tenant_id, bot_id).rerank_enabled is False


def test_apply_rerank_ignores_a_short_score_list() -> None:
    chunks = [RankedChunk(uuid.uuid4(), uuid.uuid4(), f"t{n}", 0.5, {}) for n in range(3)]

    class Short:
        def score(self, question, texts):
            return [0.9]

    kept, reranked = apply_rerank(Short(), "q", chunks, keep=3)
    assert reranked is False
    assert [c.text for c in kept] == ["t0", "t1", "t2"]
