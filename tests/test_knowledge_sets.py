"""RAG-11: a bot serves a knowledge *set*, not one version.

The row implements HALT-07's OPTION 2 (PLAN §8.24 RULINGS, 2026-11-17) — "expectation is
a multi document knowledge base" — and the shape it names is
``knowledge_sets`` + ``knowledge_set_members`` with ``bots.active_knowledge_set_id`` as
the served pointer.

What the tests here are for, in the order the ruling argues them:

* **the defect is gone**: two documents published in sequence are both retrievable,
  which is what failed in §8.22 (a live shipping question declined at 0.4367 against a
  0.55 floor while the content sat indexed in the same tenant)
* **the migration moved the old pointer rather than stranding it**: a bot that pointed
  at a version before ``0008`` is served from a one-member set afterwards, and a bot
  that published nothing stays unpublished
* **the negative cases stay negative**: a draft version is still unreachable through the
  published path, another tenant's set is invisible, and an empty set is "nothing
  published" rather than "search the whole tenant"
* **rollback is still one pointer swap**, and the two pointers cannot disagree: writing
  the retired version column has no effect on what is served

The embedder is the deterministic ``HashingEmbedder``, so these assert behaviour and
scope, not model quality.
"""

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import _admin_dsn, app_dsn, seed_knowledge_set
from app.db.vector_store import PgVectorStore
from app.knowledge import service as knowledge
from app.retrieval import pipeline
from app.retrieval.embedding import HashingEmbedder
from app.retrieval.store import VectorRecord

DIM = 1024
_SEEDED: list[uuid.UUID] = []


@pytest.fixture
def admin(migrated_db: str) -> Iterator[psycopg.Connection]:
    _SEEDED.clear()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        yield conn
    with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(_SEEDED),))


@pytest.fixture
def app_session(migrated_db: str) -> Iterator[Session]:
    """A session as ``eva_app``, so RLS is in force, not bypassed."""
    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + migrated_db
    )
    session = Session(engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _tenant(conn, name: str = "sets-tenant") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    _SEEDED.append(tenant_id)
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, name))
    return tenant_id


def _bot(conn, tenant_id, *, threshold: float = 0.05) -> uuid.UUID:
    bot_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO bots (id, tenant_id, public_id, name, confidence_threshold)"
        " VALUES (%s,%s,%s,'Bot',%s)",
        (bot_id, tenant_id, f"bot-{bot_id.hex[:8]}", threshold),
    )
    return bot_id


def _document(conn, tenant_id, *, title: str) -> tuple[uuid.UUID, uuid.UUID]:
    source_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'upload')",
        (source_id, tenant_id),
    )
    document_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title) VALUES (%s,%s,%s,%s)",
        (document_id, tenant_id, source_id, title),
    )
    return document_id, source_id


def _version(conn, tenant_id, *, title: str, body: str, status: str = "ready",
             version_no: int = 1,
             document: tuple[uuid.UUID, uuid.UUID] | None = None) -> uuid.UUID:
    """A document version with one embedded chunk, via the production upsert.

    Both versions of one document share a ``document`` handle; a fresh one is made when
    omitted, which is the "new document" case.
    """
    document_id, _source_id = document or _document(conn, tenant_id, title=title)
    version_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
        " content_hash, status) VALUES (%s,%s,%s,%s,%s,%s)",
        (version_id, tenant_id, document_id, version_no, f"h{version_id.hex[:6]}", status),
    )
    chunk_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, metadata)"
        " VALUES (%s,%s,%s,0,%s,%s::jsonb)",
        (chunk_id, tenant_id, version_id, body,
         json.dumps({"section": "S", "ordinal": 0})),
    )
    conn.commit()

    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/"
        + conn.info.dbname
    )
    with Session(engine) as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenant_id)},
        )
        PgVectorStore(session, dimension=DIM).upsert(
            tenant_id,
            [
                VectorRecord(
                    chunk_id=chunk_id,
                    document_version_id=version_id,
                    text=body,
                    embedding=HashingEmbedder(dimension=DIM).embed([body])[0],
                )
            ],
        )
        session.commit()
    engine.dispose()
    return version_id


def _scope(session: Session, tenant_id) -> None:
    session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


# --- the set is the served scope -------------------------------------------


def test_a_set_serves_every_member_not_only_the_newest(
    admin, app_session: Session, migrated_db: str
) -> None:
    """The HALT-07 defect: publishing the second document must not retire the first."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    shipping = _version(admin, tenant_id, title="Shipping", body="Standard shipping takes 3-5 business days.")
    refunds = _version(admin, tenant_id, title="Refunds", body="Refunds are processed within five business days.")

    _scope(app_session, tenant_id)
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[shipping], label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)

    # Publish the second document the way the router does: inherit + add.
    members = knowledge.resolve_publish_members(
        app_session, tenant_id, bot_id=bot_id, version_ids=[refunds]
    )
    second = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=members, label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=second)

    served = pipeline.active_version_ids(app_session, tenant_id, bot_id)
    assert set(served) == {shipping, refunds}, (
        "the second document must be added to the live set, not replace it"
    )

    # And both are actually retrievable, which is the user-visible claim. Retrieval is
    # asked with the served versions and a zero floor, so the assertion is about scope
    # rather than about the floor another test owns.
    config = pipeline.BotRetrievalConfig(confidence_threshold=0.0, rerank_enabled=False)
    shipping_hits = pipeline.retrieve(
        app_session, tenant_id, HashingEmbedder(dimension=DIM).embed(["shipping"])[0],
        "how long does shipping take", version_ids=served, config=config,
    )
    assert any("3-5 business days" in c.text for c in shipping_hits.chunks)
    refund_hits = pipeline.retrieve(
        app_session, tenant_id, HashingEmbedder(dimension=DIM).embed(["refund"])[0],
        "refund processing time", version_ids=served, config=config,
    )
    assert any("five business days" in c.text for c in refund_hits.chunks)


def test_a_newer_version_of_one_document_replaces_only_that_document(
    admin, app_session: Session
) -> None:
    """Two versions of one document must not be served side by side.

    Otherwise an owner who corrects a page is answered from the text they replaced.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    document = _document(admin, tenant_id, title="Refunds")
    old = _version(admin, tenant_id, title="Refunds", body="Refunds take thirty days.",
                   document=document)
    new = _version(admin, tenant_id, title="Refunds", body="Refunds take two days.",
                   version_no=2, document=document)

    _scope(app_session, tenant_id)
    # Force both into the candidate pool, then verify the collapse rule.
    both = knowledge.newest_version_per_document(app_session, tenant_id, [old, new])
    assert both == [new], "only the highest version_no survives per document"

    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=both, label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == [new]


def test_publish_inherits_the_live_set_and_is_idempotent_per_document(
    admin, app_session: Session
) -> None:
    """Resolving a publish twice names each document once, not twice."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    one = _version(admin, tenant_id, title="One", body="First document body.")
    two = _version(admin, tenant_id, title="Two", body="Second document body.")

    _scope(app_session, tenant_id)
    first = knowledge.resolve_publish_members(
        app_session, tenant_id, bot_id=bot_id, version_ids=[one]
    )
    assert first == [one], "nothing was live, so the publish serves only what it names"
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=first, label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)

    again = knowledge.resolve_publish_members(
        app_session, tenant_id, bot_id=bot_id, version_ids=[one]
    )
    assert again == [one], "re-publishing a live document must not duplicate the member"

    both = knowledge.resolve_publish_members(
        app_session, tenant_id, bot_id=bot_id, version_ids=[two]
    )
    assert set(both) == {one, two}


# --- the migration's claim -------------------------------------------------


def test_migration_wrapped_the_existing_pointer_in_a_one_member_set(
    admin, app_session: Session
) -> None:
    """Migration 0008's backfill, asserted on the real schema rather than by reading it.

    A fresh database has no pre-0008 rows, so the backfill is reproduced here at the SQL
    level (the same three statements the migration runs) and then read through the
    production retrieval path. The scratch-database drill that exercises alembic itself
    lives in ``tests/test_ops09_backup.py``-style probes and in the WP report.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    version_id = _version(admin, tenant_id, title="Legacy", body="Legacy document body.")

    # The column the migration read is still present, so this is a faithful stand-in.
    admin.execute(
        "UPDATE bots SET active_knowledge_version_id = %s WHERE id = %s",
        (version_id, bot_id),
    )
    admin.execute(
        "INSERT INTO knowledge_sets (tenant_id, bot_id, label)"
        " SELECT tenant_id, id, 'Migrated from a single published version'"
        "   FROM bots WHERE id = %s AND active_knowledge_version_id IS NOT NULL",
        (bot_id,),
    )
    admin.execute(
        "INSERT INTO knowledge_set_members (tenant_id, set_id, document_version_id)"
        " SELECT s.tenant_id, s.id, b.active_knowledge_version_id"
        "   FROM bots b JOIN knowledge_sets s"
        "     ON s.bot_id = b.id AND s.tenant_id = b.tenant_id"
        "  WHERE b.id = %s AND b.active_knowledge_version_id IS NOT NULL",
        (bot_id,),
    )
    admin.execute(
        "UPDATE bots SET active_knowledge_set_id = s.id"
        "  FROM knowledge_sets s"
        " WHERE s.bot_id = bots.id AND s.tenant_id = bots.tenant_id"
        "   AND bots.id = %s AND bots.active_knowledge_version_id IS NOT NULL",
        (bot_id,),
    )
    admin.execute(
        "UPDATE bots SET active_knowledge_version_id = NULL"
        " WHERE id = %s AND active_knowledge_set_id IS NOT NULL",
        (bot_id,),
    )
    admin.commit()

    _scope(app_session, tenant_id)
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == [version_id], (
        "a bot that pointed at a version must be served from a one-member set"
    )


def test_a_bot_that_published_nothing_stays_unpublished(admin, app_session: Session) -> None:
    """The backfill must not invent a set for a bot that had no pointer."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)

    _scope(app_session, tenant_id)
    assert pipeline.active_set_id(app_session, tenant_id, bot_id) is None
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == []


# --- the retired column cannot disagree with the set -----------------------


def test_writing_the_retired_version_column_does_not_change_what_is_served(
    admin, app_session: Session
) -> None:
    """One served truth.

    ``bots.active_knowledge_version_id`` still exists (§16.1 lists it) but nothing reads
    it. A test that seeded it and saw the old behaviour would silently reintroduce the
    two-pointer disagreement migration 0008 removed.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    served = _version(admin, tenant_id, title="Served", body="The served document.")
    unrelated = _version(admin, tenant_id, title="Unrelated", body="A different document.")

    _scope(app_session, tenant_id)
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[served], label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)

    # Commit before the other connection writes: a held row lock would block it, and the
    # tenant GUC is transaction-local so it is re-applied below.
    app_session.commit()
    admin.execute(
        "UPDATE bots SET active_knowledge_version_id = %s WHERE id = %s",
        (unrelated, bot_id),
    )
    admin.commit()

    _scope(app_session, tenant_id)
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == [served]


# --- the negative cases stay negative --------------------------------------


def test_a_draft_version_is_unreachable_through_the_set(
    admin, app_session: Session
) -> None:
    """Membership is not enough: a version still ingesting must not be served."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    ready = _version(admin, tenant_id, title="Ready", body="Finished document.")
    draft = _version(admin, tenant_id, title="Draft", body="Still processing.",
                     status="processing")

    _scope(app_session, tenant_id)
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[ready, draft], label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)

    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == [ready], (
        "a set names candidates, but only a ready version may be served"
    )


def _live_set(conn, tenant_id) -> uuid.UUID:
    """Give ``tenant_id`` one published set of its own, for the positive direction."""
    bot_id = _bot(conn, tenant_id)
    version_id = _version(conn, tenant_id, title="Mine", body="My own policy.")
    set_id = seed_knowledge_set(conn, tenant_id, bot_id, [version_id])
    conn.commit()
    return set_id


def test_another_tenants_set_is_invisible(admin, app_session: Session) -> None:
    """A set id from another tenant must resolve to nothing, not to their documents."""
    mine = _tenant(admin, "mine")
    theirs = _tenant(admin, "theirs")
    their_bot = _bot(admin, theirs)
    their_version = _version(admin, theirs, title="Theirs", body="Their private policy.")

    with psycopg.connect(_admin_dsn(admin.info.dbname), autocommit=True) as other:
        their_set = seed_knowledge_set(other, theirs, their_bot, [their_version])

    _scope(app_session, mine)
    assert pipeline.set_version_ids(app_session, mine, their_set) == [], (
        "a foreign set id must return no candidates, never another tenant's versions"
    )

    # Mutation-checked: dropping ``AND m.tenant_id = :tenant_id`` from the membership
    # query leaves this test green, because RLS refuses the row first. The predicate is
    # defence in depth, not the guard -- so the policy itself is asserted separately
    # below, unscoped, because a lost policy makes the query *succeed and return too
    # much*, which is the failure mode a happy-path test cannot see.
    #
    # As ``eva_app``, not the admin role: ``eva`` is a superuser with BYPASSRLS, so an
    # unscoped read through it would prove nothing about the policy. And both
    # directions are asserted: a table with RLS forced and *no* policy denies everything,
    # so "the foreign set is invisible" alone stays green when the policy is deleted.
    # Only the positive half separates a correct policy from a fail-closed table.
    own_set = _live_set(admin, mine)
    with psycopg.connect(app_dsn(admin.info.dbname)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(mine),))
        visible = conn.execute(
            "SELECT count(*) FROM knowledge_set_members WHERE set_id = %s", (their_set,)
        ).fetchone()[0]
        mine_rows = conn.execute("SELECT count(*) FROM knowledge_sets").fetchone()[0]
        members = conn.execute("SELECT count(*) FROM knowledge_set_members").fetchone()[0]
    assert visible == 0, "RLS must hide another tenant's set members even unscoped"
    assert mine_rows == 1, "the tenant's own set must be visible -- RLS is a filter, not a wall"
    assert members == 1, "the tenant's own member row must be visible too"
    assert own_set != their_set


def test_an_empty_set_is_nothing_published_not_search_everything(
    admin, app_session: Session
) -> None:
    """The invariant that predates sets: no pointer means no candidates."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    _version(admin, tenant_id, title="Indexed", body="Nobody published this.")

    _scope(app_session, tenant_id)
    empty = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[], label="empty"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=empty)

    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == []
    result = pipeline.retrieve(
        app_session, tenant_id, HashingEmbedder(dimension=DIM).embed(["anything"])[0],
        "any question at all", version_ids=[],
        config=pipeline.BotRetrievalConfig(confidence_threshold=0.0),
    )
    assert result.chunks == [], "an empty set must not fall back to the whole corpus"


def test_a_foreign_version_cannot_be_pulled_into_a_set(
    admin, app_session: Session
) -> None:
    """``newest_version_per_document`` scopes by tenant, so it cannot resolve a
    foreign version id even when one is named."""
    mine = _tenant(admin, "mine2")
    theirs = _tenant(admin, "theirs2")
    their_version = _version(admin, theirs, title="Theirs", body="Private.")

    _scope(app_session, mine)
    assert knowledge.newest_version_per_document(app_session, mine, [their_version]) == []


# --- the pointer survives a delete without breaking the bot ---------------


def test_deleting_the_live_set_clears_the_pointer_rather_than_the_bot(
    admin, app_session: Session
) -> None:
    """FK is SET NULL, not CASCADE: emptying knowledge must not delete the bot."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    version_id = _version(admin, tenant_id, title="Doc", body="Body.")

    _scope(app_session, tenant_id)
    set_id = knowledge.create_set(
        app_session, tenant_id, bot_id=bot_id, version_ids=[version_id], label="live"
    )
    knowledge.point_at(app_session, tenant_id, bot_id=bot_id, set_id=set_id)
    app_session.commit()

    admin.execute("DELETE FROM knowledge_sets WHERE id = %s", (set_id,))
    admin.commit()

    _scope(app_session, tenant_id)
    assert pipeline.active_set_id(app_session, tenant_id, bot_id) is None
    assert pipeline.active_version_ids(app_session, tenant_id, bot_id) == []
    assert knowledge.active_bot_exists(app_session, tenant_id, bot_id) is True, (
        "the bot must survive its knowledge being deleted"
    )
