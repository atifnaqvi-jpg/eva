"""KNOW-06: manual FAQ entry becomes exactly one chunk per Q&A pair."""

import asyncio
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn
from app.knowledge import service as knowledge
from app.retrieval.embedding import HashingEmbedder


def _url(db: str) -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg",
        username=APP_USER,
        password=APP_PASSWORD,
        host=PG_HOST,
        port=PG_PORT,
        database=db,
    ).render_as_string(hide_password=False)


@pytest.fixture
def tenant(migrated_db: str):
    tenant_id = uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'faq-tenant')", (tenant_id,))
        source_id = conn.execute(
            "INSERT INTO knowledge_sources (tenant_id, type) VALUES (%s,'faq') RETURNING id",
            (tenant_id,),
        ).fetchone()[0]
    from app.config import TENANT_SETTING
    from app.db.session import make_session_factory

    engine = create_engine(_url(migrated_db))
    # Factory attaches the tenant-GUC hook, so the context survives the commits
    # this workflow makes. Setting it by hand would be lost at the first commit.
    session = make_session_factory(engine)()
    session.info[TENANT_SETTING] = str(tenant_id)
    try:
        yield session, tenant_id, source_id, migrated_db
    finally:
        session.close()
        engine.dispose()
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def test_faq_becomes_exactly_one_chunk(tenant) -> None:
    session, tenant_id, source_id, _ = tenant
    embedder = HashingEmbedder(dimension=1024)

    document_id, version_id = knowledge.create_faq_version(
        session,
        tenant_id,
        source_id=source_id,
        question="Do you ship internationally?",
        answer="Yes, to twelve countries.",
        embedder=embedder,
    )
    session.commit()

    assert knowledge.version_chunk_count(session, version_id) == 1
    row = session.execute(
        text("SELECT text, metadata FROM chunks WHERE document_version_id = :v"),
        {"v": str(version_id)},
    ).mappings().one()
    assert "Q: Do you ship internationally?" in row["text"]
    assert "A: Yes, to twelve countries." in row["text"]
    assert row["metadata"]["faq"] is True


def test_a_long_faq_answer_is_not_split(tenant) -> None:
    """ARCHITECTURE 10.2: one chunk per Q&A pair, regardless of length."""
    session, tenant_id, source_id, _ = tenant
    embedder = HashingEmbedder(dimension=1024)

    _, version_id = knowledge.create_faq_version(
        session,
        tenant_id,
        source_id=source_id,
        question="Why is the sky blue?",
        answer=" ".join(["Rayleigh scattering explains it."] * 200),
        embedder=embedder,
    )
    session.commit()
    assert knowledge.version_chunk_count(session, version_id) == 1


def test_faq_is_tenant_scoped(tenant, migrated_db) -> None:
    session, tenant_id, source_id, _ = tenant
    knowledge.create_faq_version(
        session,
        tenant_id,
        source_id=source_id,
        question="Scoped?",
        answer="Yes.",
        embedder=HashingEmbedder(dimension=1024),
    )
    session.commit()

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        mine = conn.execute(
            "SELECT count(*) FROM chunks WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
    assert mine == 1


def test_documents_listing_surfaces_status_and_chunks(tenant) -> None:
    session, tenant_id, source_id, _ = tenant
    knowledge.create_faq_version(
        session,
        tenant_id,
        source_id=source_id,
        question="How do I reset my password?",
        answer="Use the reset link.",
        embedder=HashingEmbedder(dimension=1024),
    )
    session.commit()

    docs = knowledge.list_documents(session, tenant_id)
    assert len(docs) == 1
    assert docs[0]["status"] == "ready"
    assert docs[0]["chunk_count"] == 1
    assert docs[0]["title"] == "How do I reset my password?"


def test_invalid_crawl_schedule_is_rejected(tenant) -> None:
    session, tenant_id, _, _ = tenant
    with pytest.raises(ValueError, match="schedule"):
        knowledge.create_crawl_source(
            session, tenant_id, url="http://example.test", schedule="hourly"
        )
