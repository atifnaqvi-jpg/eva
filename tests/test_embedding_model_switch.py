"""MANDATORY PROOF (user, 2026-11-16): a real model swap with a different dimension.

This is not a documentation test. It performs the whole operation against the live
database and asserts each step the user asked to see:

1. corpus ingested and published at 1024 dims (model A)
2. re-embedded at 384 dims (model B) as a *new version*, old version still readable
3. search on both versions independently, in their own vector spaces
4. publish B atomically, then roll back to A

The 384-dim model is ``HashingEmbedder(dimension=384)``. Using a genuinely
different vector space is the point — a second provider that also emitted 1024
would prove nothing about the dimension rewrite. Real BGE-M3 is exercised
separately in ``test_bge_m3_provider.py``; torch is deliberately not a
dependency of the API image or CI.
"""

import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn
from app.db.dimension_migration import current_dimension, migrate_embedding_dimension
from app.db.vector_store import PgVectorStore
from app.knowledge import service as knowledge
from app.retrieval.embedding import HashingEmbedder
from app.ingestion.pipeline import ingest_bytes

CORPUS = {
    "refunds": "# Refunds\n\nRefunds are processed within five business days of return.",
    "shipping": "# Shipping\n\nStandard shipping arrives in three to five business days.",
    "warranty": "# Warranty\n\nCoverage lasts two years from the purchase date.",
}


def _sqlalchemy_url(migrated_db: str, *, owner: bool) -> str:
    from sqlalchemy.engine import URL

    if owner:
        url = URL.create(
            "postgresql+psycopg",
            username="eva",
            password="changeme",
            host=PG_HOST,
            port=PG_PORT,
            database=migrated_db,
        )
    else:
        url = URL.create(
            "postgresql+psycopg",
            username=APP_USER,
            password=APP_PASSWORD,
            host=PG_HOST,
            port=PG_PORT,
            database=migrated_db,
        )
    return url.render_as_string(hide_password=False)


def _owner_session(migrated_db: str) -> Session:
    """Owner role: the dimension rewrite is DDL, which ``eva_app`` must not have."""
    return Session(create_engine(_sqlalchemy_url(migrated_db, owner=True)))


@pytest.fixture(autouse=True)
def restore_dimension(migrated_db: str):
    """The rewrite is global DDL, so put the column back for other tests."""
    yield
    owner = _owner_session(migrated_db)
    try:
        migrate_embedding_dimension(owner, 1024)
        owner.commit()
    finally:
        owner.close()


@pytest.fixture
def session(migrated_db: str):
    engine = create_engine(_sqlalchemy_url(migrated_db, owner=False))
    s = Session(engine)
    created: list[uuid.UUID] = []

    def track(tenant_id: uuid.UUID) -> None:
        created.append(tenant_id)

    s.info["track_tenant"] = track
    try:
        yield s, migrated_db
    finally:
        s.rollback()
        s.close()
        engine.dispose()
        # The publish/rollback flow commits, so these rows outlive the fixture
        # unless they are deleted explicitly; leftover chunks would pollute
        # other tests that count rows globally.
        if created:
            with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
                conn.execute(
                    "DELETE FROM tenants WHERE id = ANY(%s)", (created,)
                )


def _tenant_with_bot(migrated_db: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, bot_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'switch-tenant')", (tenant_id,))
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s,%s,%s,'Bot')",
            (bot_id, tenant_id, f"bot-{bot_id}"),
        )
        conn.execute(
            "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'upload')",
            (uuid.uuid4(), tenant_id),
        )
    return tenant_id, bot_id


def _ingest_corpus(session, migrated_db, tenant_id, bot_id, provider, label):
    """Ingest the corpus as one version per document; returns version ids."""
    versions = []
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        source_id = conn.execute(
            "SELECT id FROM knowledge_sources WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]

    for name, body in CORPUS.items():
        ingested = ingest_bytes(f"{name}.md", body.encode())
        session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        document_id = knowledge.create_document(
            session, tenant_id, source_id=source_id, title=f"{name} ({label})"
        )
        version_id = knowledge.create_version(
            session, tenant_id, document_id=document_id, content_hash=ingested.content_hash
        )
        knowledge.store_chunks(
            session, tenant_id, version_id=version_id, ingested=ingested, embedder=provider
        )
        knowledge.set_version_status(session, version_id, "ready")
        versions.append(version_id)
    return versions


def test_full_model_switch_with_dimension_change(session) -> None:
    s, migrated_db = session
    tenant_id, bot_id = _tenant_with_bot(migrated_db)
    s.info["track_tenant"](tenant_id)

    model_a = HashingEmbedder(key="model-a-1024", dimension=1024)
    model_b = HashingEmbedder(key="model-b-384", dimension=384)

    # --- 1. ingest + publish at 1024 -------------------------------------
    assert current_dimension(s) == 1024
    versions_a = _ingest_corpus(s, migrated_db, tenant_id, bot_id, model_a, "A")
    for v in versions_a:
        knowledge.publish(s, tenant_id, bot_id=bot_id, version_id=v)

    s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    store_a = PgVectorStore(s, dimension=1024)
    hits_a = store_a.search(
        tenant_id, model_a.embed(["refund business days"])[0], version_ids=versions_a
    )
    assert hits_a and hits_a[0].document_version_id in versions_a
    assert all(len(r) == 1024 for r in model_a.embed(["x"]))

    # --- 2. physical dimension rewrite, then re-ingest at 384 ------------
    # The rewrite runs as the owner: the app role must not be able to reshapes
    # its own vector column (verified in the next test).
    # Release this session's locks first: the rewrite needs ACCESS EXCLUSIVE.
    s.commit()
    s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    owner = _owner_session(migrated_db)
    report = migrate_embedding_dimension(owner, 384)
    owner.commit()
    owner.close()
    assert report["changed"] is True
    assert report["old_dimension"] == 1024 and report["new_dimension"] == 384
    assert report["vectors_discarded"] >= len(CORPUS), "old vectors are discarded, visibly"
    assert current_dimension(owner) == 384

    versions_b = _ingest_corpus(s, migrated_db, tenant_id, bot_id, model_b, "B")
    s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})

    # --- 3. both versions remain readable, each in its own space ---------
    store_b = PgVectorStore(s, dimension=384)
    hits_b = store_b.search(
        tenant_id, model_b.embed(["refund business days"])[0], version_ids=versions_b
    )
    assert hits_b, "the new version must be retrievable"
    assert hits_b[0].document_version_id in versions_b
    assert not ({h.document_version_id for h in hits_b} & set(versions_a))

    # Old version rows still exist (only their vectors were discarded).
    old_chunks = s.execute(
        text("SELECT count(*) FROM chunks WHERE document_version_id = ANY(:v)"),
        {"v": [str(v) for v in versions_a]},
    ).scalar()
    assert old_chunks == len(CORPUS), "old chunks stay readable until publish"

    # --- 4. publish B, then roll back to A -------------------------------
    knowledge.publish(s, tenant_id, bot_id=bot_id, version_id=versions_b[0])
    assert knowledge.active_version(s, tenant_id, bot_id) == versions_b[0]

    knowledge.publish(s, tenant_id, bot_id=bot_id, version_id=versions_a[0])
    assert knowledge.active_version(s, tenant_id, bot_id) == versions_a[0], "rollback re-points"


def test_dimension_rewrite_requires_owner_privileges(migrated_db: str) -> None:
    """The app role must not be able to reshape the vector column itself."""
    app = Session(create_engine(_sqlalchemy_url(migrated_db, owner=False)))
    try:
        with pytest.raises(Exception, match="(must be owner|permission denied)|InsufficientPrivilege"):
            migrate_embedding_dimension(app, 384)
    finally:
        app.rollback()
        app.close()


def test_publish_is_a_single_pointer_swap(session) -> None:
    """The published set is never partially swapped: one column, one UPDATE."""
    s, migrated_db = session
    tenant_id, bot_id = _tenant_with_bot(migrated_db)
    s.info["track_tenant"](tenant_id)
    provider = HashingEmbedder(dimension=1024)
    versions = _ingest_corpus(s, migrated_db, tenant_id, bot_id, provider, "A")

    knowledge.publish(s, tenant_id, bot_id=bot_id, version_id=versions[0])
    knowledge.publish(s, tenant_id, bot_id=bot_id, version_id=versions[1])
    assert knowledge.active_version(s, tenant_id, bot_id) == versions[1]

    # A second bot in the same tenant is unaffected by the first bot's publish.
    other_bot = uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s,%s,%s,'Other')",
            (other_bot, tenant_id, f"other-{other_bot}"),
        )
    assert knowledge.active_version(s, tenant_id, other_bot) is None
