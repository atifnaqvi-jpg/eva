"""KNOW-10: knowledge-source priority as a weighted boost on the fused score.

HALT-03 was ruled OPTION B (PLAN 8.24): a per-source multiplier on the fused score,
with the weights in tenant config, and "strong relevance must still beat tier
preference". Each test below pins one half of that sentence, because a boost is
easy to implement in a way that passes the first half and violates the second.

The floor interaction is tested separately and deliberately: priority multiplies
``rrf_score`` (ordering), never ``score`` (similarity), so a tier preference cannot
make the product more confident than the evidence. A boost applied to ``score``
would also break the documented meaning of the confidence floor.
"""

import json
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import _admin_dsn
from app.retrieval import pipeline
from app.retrieval.hybrid import RankedChunk
from app.retrieval.priority import (
    DEFAULT_SOURCE_WEIGHTS,
    apply_source_priority,
    source_kinds_for_versions,
    source_weights,
)


def _chunk(version_id, text_body: str, rrf: float, score: float, ordinal: int = 0):
    return RankedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=version_id,
        text=text_body,
        score=score,
        metadata={"ordinal": ordinal},
        rrf_score=rrf,
    )


# --- the arithmetic, no database needed -------------------------------------


def test_upload_outranks_crawl_at_equal_relevance() -> None:
    """R18's ordering, in the only case where a boost is unambiguous."""
    upload_version, crawl_version = uuid.uuid4(), uuid.uuid4()
    ranked = apply_source_priority(
        [_chunk(crawl_version, "crawl", rrf=0.02, score=0.9),
         _chunk(upload_version, "upload", rrf=0.02, score=0.9)],
        {upload_version: "upload", crawl_version: "crawl"},
        DEFAULT_SOURCE_WEIGHTS,
    )
    assert [c.text for c in ranked] == ["upload", "crawl"]
    assert ranked[0].source_kind == "upload" and ranked[0].priority_weight == 1.15


def test_strong_relevance_still_beats_tier_preference() -> None:
    """The ruling's constraint: a boost must not invert a clear relevance gap.

    Default weights top out at x1.15, so an upload only wins while the fused scores
    are close. A crawl chunk that the retrievers ranked clearly higher must stay
    higher, or the boost has become hard tiering (option A, rejected).
    """
    upload_version, crawl_version = uuid.uuid4(), uuid.uuid4()
    ranked = apply_source_priority(
        [_chunk(crawl_version, "crawl", rrf=0.030, score=0.95),
         _chunk(upload_version, "upload", rrf=0.020, score=0.95)],
        {upload_version: "upload", crawl_version: "crawl"},
        DEFAULT_SOURCE_WEIGHTS,
    )
    assert [c.text for c in ranked] == ["crawl", "upload"], (
        "0.030 crawl must still beat 0.020 * 1.15 upload"
    )


def test_priority_scales_the_fused_score_and_never_the_similarity() -> None:
    """The confidence floor reads ``score``; the boost must not touch it."""
    version = uuid.uuid4()
    [ranked] = apply_source_priority(
        [_chunk(version, "answer", rrf=0.016, score=0.42)],
        {version: "upload"},
        DEFAULT_SOURCE_WEIGHTS,
    )
    assert ranked.rrf_score == pytest.approx(0.016 * 1.15)
    assert ranked.score == 0.42, (
        "a boosted similarity would let priority clear the confidence floor"
    )


def test_unknown_source_kind_is_left_unboosted() -> None:
    """A chunk whose source row is missing must not be dropped or guessed at."""
    known, unknown = uuid.uuid4(), uuid.uuid4()
    ranked = apply_source_priority(
        [_chunk(unknown, "unknown", rrf=0.02, score=0.8),
         _chunk(known, "known", rrf=0.02, score=0.8)],
        {known: "upload"},
        DEFAULT_SOURCE_WEIGHTS,
    )
    unknown_chunk = next(c for c in ranked if c.text == "unknown")
    assert unknown_chunk.priority_weight == 1.0
    assert unknown_chunk.source_kind is None
    assert [c.text for c in ranked] == ["known", "unknown"], (
        "an unboosted unknown chunk loses the tie to the upload"
    )


def test_equal_boosted_scores_keep_the_rrf_order() -> None:
    """Total order: identical weights must not reshuffle the input."""
    versions = [uuid.uuid4() for _ in range(4)]
    chunks = [_chunk(v, f"t{n}", rrf=0.01, score=0.5, ordinal=n) for n, v in enumerate(versions)]
    ranked = apply_source_priority(
        chunks, {v: "faq" for v in versions}, DEFAULT_SOURCE_WEIGHTS
    )
    assert [c.text for c in ranked] == ["t0", "t1", "t2", "t3"]


def test_empty_candidates_are_handled() -> None:
    assert apply_source_priority([], {}, DEFAULT_SOURCE_WEIGHTS) == []


# --- configuration, not literals -------------------------------------------


def test_weights_default_when_the_tenant_says_nothing() -> None:
    assert source_weights(None) == DEFAULT_SOURCE_WEIGHTS
    assert source_weights({}) == DEFAULT_SOURCE_WEIGHTS
    assert source_weights({"retrieval": {}}) == DEFAULT_SOURCE_WEIGHTS


def test_tenant_weights_override_the_defaults() -> None:
    weights = source_weights({"retrieval": {"source_weights": {"upload": 1.5}}})
    assert weights["upload"] == 1.5
    assert weights["faq"] == DEFAULT_SOURCE_WEIGHTS["faq"], "unmentioned kinds keep defaults"


def test_malformed_or_out_of_range_weights_degrade_to_a_usable_value() -> None:
    weights = source_weights(
        {
            "retrieval": {
                "source_weights": {
                    "upload": "very",   # not a number
                    "faq": True,        # bool, not a weight
                    "crawl": 99.0,      # outside the band: clamped, not obeyed
                    "cloud": 1.9,       # not an MVP kind: ignored
                }
            }
        }
    )
    assert weights["upload"] == DEFAULT_SOURCE_WEIGHTS["upload"]
    assert weights["faq"] == DEFAULT_SOURCE_WEIGHTS["faq"]
    assert weights["crawl"] == 2.0, "an out-of-band weight is clamped to the band"
    assert "cloud" not in weights


# --- the source lookup, against the real schema ----------------------------


def _seed_version(conn, tenant_id, kind: str):
    source_id, document_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,%s)",
        (source_id, tenant_id, kind),
    )
    conn.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title) VALUES (%s,%s,%s,'D')",
        (document_id, tenant_id, source_id),
    )
    conn.execute(
        "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
        " content_hash, status) VALUES (%s,%s,%s,1,'h','ready')",
        (version_id, tenant_id, document_id),
    )
    return version_id


def _app_session(dbname: str) -> Session:
    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + dbname
    )
    return Session(engine)


def _drop(dbname: str, tenants: list[uuid.UUID]) -> None:
    with psycopg.connect(_admin_dsn(dbname), autocommit=True) as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (tenants,))


def test_source_kind_comes_from_the_join(migrated_db: str) -> None:
    tenant_id = uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'p')", (tenant_id,))
        upload = _seed_version(conn, tenant_id, "upload")
        faq = _seed_version(conn, tenant_id, "faq")

    session = _app_session(migrated_db)
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
        )
        assert source_kinds_for_versions(session, tenant_id, [upload, faq]) == {
            upload: "upload", faq: "faq",
        }
    finally:
        session.close()
        _drop(migrated_db, [tenant_id])


def test_source_lookup_refuses_another_tenant(migrated_db: str) -> None:
    """The join reaches three tables, so the predicate is stated, not assumed.

    Passing a foreign version id must return nothing rather than a source kind:
    a boost derived from another tenant's row would be a cross-tenant read.
    """
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'mine')", (mine,))
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'theirs')", (theirs,))
        foreign = _seed_version(conn, theirs, "upload")

    session = _app_session(migrated_db)
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(mine)}
        )
        assert source_kinds_for_versions(session, mine, [foreign]) == {}
    finally:
        session.close()
        _drop(migrated_db, [mine, theirs])


def test_bot_config_reads_priority_weights_from_tenant_settings(migrated_db: str) -> None:
    """Weights live in tenant config and are read by the same function prod uses."""
    tenant_id, bot_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'c')", (tenant_id,))
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s,%s,%s,'B')",
            (bot_id, tenant_id, f"bot-{bot_id.hex[:8]}"),
        )
        conn.execute(
            "UPDATE tenants SET settings = CAST(%s AS jsonb) WHERE id = %s",
            (json.dumps({"retrieval": {"source_weights": {"faq": 1.4}}}), str(tenant_id)),
        )

    session = _app_session(migrated_db)
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
        )
        config = pipeline.bot_config(session, tenant_id, bot_id)
        assert config.source_weights["faq"] == 1.4
        assert config.source_weights["upload"] == DEFAULT_SOURCE_WEIGHTS["upload"]
    finally:
        session.close()
        _drop(migrated_db, [tenant_id])


# --- end to end through the pipeline ---------------------------------------


def test_retrieve_orders_by_priority_at_equal_fusion(migrated_db: str) -> None:
    """The boost must be reachable through ``retrieve``, not only through the unit.

    Two chunks with an identical dense score fuse in dense-rank order, so without
    priority the upload is second. The upload is the one published version, so the
    test also proves ``source_kinds_for_versions`` is wired into the real path.
    """
    from app.retrieval.embedding import HashingEmbedder
    from app.retrieval.store import VectorRecord
    from app.db.vector_store import PgVectorStore

    tenant_id = uuid.uuid4()
    question = "refund policy"
    body = "refund policy details"
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'e2e')", (tenant_id,))
        crawl_version = _seed_version(conn, tenant_id, "crawl")
        upload_version = _seed_version(conn, tenant_id, "upload")
        crawl_chunk, upload_chunk = uuid.uuid4(), uuid.uuid4()
        # Identical text means identical embeddings, so dense similarity is a genuine
        # tie and only the priority stage can break it.
        for chunk_id, version in ((crawl_chunk, crawl_version), (upload_chunk, upload_version)):
            conn.execute(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text)"
                " VALUES (%s,%s,%s,0,%s)",
                (chunk_id, tenant_id, version, body),
            )

    session = _app_session(migrated_db)
    try:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
        )
        embedder = HashingEmbedder(dimension=1024)
        store = PgVectorStore(session, dimension=1024)
        store.upsert(
            tenant_id,
            [
                VectorRecord(chunk_id=cid, document_version_id=version,
                             text=body, embedding=embedder.embed([body])[0])
                for cid, version in ((crawl_chunk, crawl_version), (upload_chunk, upload_version))
            ],
        )
        result = pipeline.retrieve(
            session, tenant_id, embedder.embed([question])[0], question,
            version_ids=[crawl_version, upload_version],
            config=pipeline.BotRetrievalConfig(confidence_threshold=0.0, rerank_enabled=False),
        )
        assert [c.chunk_id for c in result.chunks][:2] == [upload_chunk, crawl_chunk], (
            "at equal fusion the upload must lead; order proves the stage ran"
        )
        assert result.chunks[0].source_kind == "upload"
    finally:
        session.close()
        _drop(migrated_db, [tenant_id])
