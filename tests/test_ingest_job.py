"""KNOW-09: the arq ingestion job is idempotent and never hides a failure.

These exercise the real job coroutine against the real database. The embedding
provider is swapped for the deterministic test embedder, because loading BGE-M3
costs ~33 s and ~3.2 GiB and would make the suite unusable; the production
provider itself is covered by ``test_bge_m3_provider.py``.
"""

import asyncio
import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn
from app.knowledge import service as knowledge
from app.retrieval.embedding import HashingEmbedder


def _url(db: str, *, owner: bool) -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg",
        username="eva" if owner else APP_USER,
        password="changeme" if owner else APP_PASSWORD,
        host=PG_HOST,
        port=PG_PORT,
        database=db,
    ).render_as_string(hide_password=False)


@pytest.fixture
def workspace(migrated_db: str, monkeypatch):
    """Point the job's session factory at the test DB; swap in the test embedder."""
    import app.db.session as db_session

    engine = create_engine(_url(migrated_db, owner=False))
    # Use the real factory so the tenant GUC hook is exercised too.
    monkeypatch.setattr(db_session, "SessionLocal", db_session.make_session_factory(engine))

    import worker.ingest_job as job

    monkeypatch.setattr(job, "_provider", HashingEmbedder(dimension=1024), raising=False)
    monkeypatch.setattr(job, "_get_provider", lambda: HashingEmbedder(dimension=1024))

    tenant_id, version_id = uuid.uuid4(), None
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'job-tenant')", (tenant_id,))
        source_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'upload')",
            (source_id, tenant_id),
        )
        document_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO documents (id, tenant_id, source_id, title) VALUES (%s,%s,%s,'Doc')",
            (document_id, tenant_id, source_id),
        )
        version_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
            " content_hash, status, filename, payload)"
            " VALUES (%s,%s,%s,1,'h','pending','policy.md',%s)",
            (version_id, tenant_id, document_id, b"# Refunds\n\nRefunds take five business days."),
        )

    yield job, tenant_id, version_id, migrated_db

    for table, key in (("tenants", "id"),):
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute(f"DELETE FROM {table} WHERE {key} = %s", (tenant_id,))
    engine.dispose()


def _status_and_chunks(migrated_db: str, version_id) -> tuple[str, int]:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        status = conn.execute(
            "SELECT status FROM document_versions WHERE id = %s", (version_id,)
        ).fetchone()[0]
        count = conn.execute(
            "SELECT count(*) FROM chunks WHERE document_version_id = %s", (version_id,)
        ).fetchone()[0]
    return status, count


def test_job_ingests_and_marks_ready(workspace) -> None:
    job, tenant_id, version_id, migrated_db = workspace

    result = asyncio.run(job.ingest_document({}, str(tenant_id), str(version_id), "policy.md"))

    status, count = _status_and_chunks(migrated_db, version_id)
    assert status == "ready"
    assert count == result["chunks"] > 0


def test_rerunning_the_job_does_not_duplicate_chunks(workspace) -> None:
    """arq retries; a retry must converge, not append."""
    job, tenant_id, version_id, migrated_db = workspace

    first = asyncio.run(job.ingest_document({}, str(tenant_id), str(version_id), "policy.md"))
    second = asyncio.run(job.ingest_document({}, str(tenant_id), str(version_id), "policy.md"))

    _, first_count = _status_and_chunks(migrated_db, version_id)
    assert first_count == first["chunks"] == second["chunks"]


def test_failure_is_recorded_and_reraised(workspace, monkeypatch) -> None:
    """A failure must be visible to the owner *and* propagate so arq retries."""
    job, tenant_id, version_id, migrated_db = workspace

    def explode(filename, payload):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(job, "ingest_bytes", explode)

    with pytest.raises(RuntimeError, match="parser exploded"):
        asyncio.run(job.ingest_document({}, str(tenant_id), str(version_id), "policy.md"))

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        status, error = conn.execute(
            "SELECT status, error FROM document_versions WHERE id = %s", (version_id,)
        ).fetchone()
    assert status == "failed"
    assert "parser exploded" in error


def test_unsupported_format_fails_visibly(workspace) -> None:
    job, tenant_id, version_id, migrated_db = workspace

    with pytest.raises(Exception):
        asyncio.run(job.ingest_document({}, str(tenant_id), str(version_id), "deck.pptx"))

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        status = conn.execute(
            "SELECT status FROM document_versions WHERE id = %s", (version_id,)
        ).fetchone()[0]
    assert status == "failed"
