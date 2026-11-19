"""ADMN-17: rollback restores the previous published *set*, atomically or not at all.

HALT-10's ruling (PLAN §8.47, 2026-11-19) is the specification:

1. **record the edge** — publish stores the set the bot was serving, rather than
   letting rollback infer "previous" from ``created_at``, which two publishes in the
   same second would tie;
2. **atomic or nothing** — a rollback restores the whole set or refuses with a clear
   error; it must never leave the bot serving a subset it did not name;
3. **publish stays additive** — only rollback's meaning changed;
4. the UI control lands with this row, because a button that shrinks the knowledge base
   is worse than no button.

The defect being removed was measured, not read: publishing A then B and rolling back
to A served **1** document with a **200**, so four of five documents silently stopped
being answerable. The test that matters most here is therefore not "rollback returns
200" — the broken version returned 200 — but **"the restored set still holds every
document it held before"**, asserted through the retrieval path the widget uses.

Two traps this file is written against:

* a rollback test that publishes one document and restores it passes whether the
  implementation restores a set or a single version, so every case here publishes at
  least two;
* asserting the ``knowledge_sets`` row proves the row, not the effect. The assertions
  read ``pipeline.active_version_ids`` — the same call the turn loop makes — so a
  service that wrote a correct-looking set and forgot ``point_at`` is red.
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
    """A session as ``eva_app``, so RLS and the tenant predicate are both in force."""
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


def _tenant(conn, name: str = "rollback-tenant") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    _SEEDED.append(tenant_id)
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant_id, name))
    conn.commit()
    return tenant_id


def _bot(conn, tenant_id, *, threshold: float = 0.05) -> uuid.UUID:
    bot_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO bots (id, tenant_id, public_id, name, confidence_threshold)"
        " VALUES (%s,%s,%s,'Bot',%s)",
        (bot_id, tenant_id, f"bot-{bot_id.hex[:8]}", threshold),
    )
    conn.commit()
    return bot_id


def _version(conn, tenant_id, *, title: str, body: str) -> uuid.UUID:
    """A ready document version with one embedded chunk, through the production upsert."""
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
    version_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
        " content_hash, status) VALUES (%s,%s,%s,1,%s,'ready')",
        (version_id, tenant_id, document_id, f"h{version_id.hex[:6]}"),
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
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + conn.info.dbname
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


def _publish(session: Session, tenant_id, bot_id, version_id) -> uuid.UUID:
    """Publish exactly the way the router does: inherit + add, record the edge, point."""
    # The tenant GUC is transaction-local, and this helper commits, so every call
    # re-applies it. Forgetting that makes the insert fail as an RLS *denial*, which
    # looks like an isolation bug rather than a missing fixture step.
    _scope(session, tenant_id)
    was_serving = knowledge.active_set(session, tenant_id, bot_id)
    members = knowledge.resolve_publish_members(
        session, tenant_id, bot_id=bot_id, version_ids=[version_id]
    )
    set_id = knowledge.create_set(
        session, tenant_id, bot_id=bot_id, version_ids=members, label="Published",
        previous_set_id=was_serving,
    )
    knowledge.point_at(session, tenant_id, bot_id=bot_id, set_id=set_id)
    session.commit()
    return set_id


def _served(session: Session, tenant_id, bot_id) -> set[uuid.UUID]:
    _scope(session, tenant_id)
    return set(pipeline.active_version_ids(session, tenant_id, bot_id))


def _rollback(session: Session, tenant_id, bot_id) -> tuple[uuid.UUID, list[uuid.UUID]]:
    _scope(session, tenant_id)
    return knowledge.rollback_to_previous(session, tenant_id, bot_id=bot_id)


def _previous(session: Session, tenant_id, bot_id, set_id):
    _scope(session, tenant_id)
    return knowledge.previous_set(session, tenant_id, bot_id=bot_id, set_id=set_id)


def _active_set(session: Session, tenant_id, bot_id):
    _scope(session, tenant_id)
    return knowledge.active_set(session, tenant_id, bot_id)


# --- the edge -----------------------------------------------------------------


def test_publish_records_the_set_it_replaced(admin, app_session: Session) -> None:
    """The edge is written by publish, which is the only thing that can know it.

    Inferring "previous" from ``created_at`` is what the ruling forbids: two publishes
    in the same second tie, and the tie decides which documents a restore serves.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="Standard shipping takes 3 days.")
    b = _version(admin, tenant_id, title="B", body="Refunds take five days.")

    _scope(app_session, tenant_id)
    first = _publish(app_session, tenant_id, bot_id, a)
    assert _previous(app_session, tenant_id, bot_id, first) is None, "the first publish replaced nothing, so it records no edge"

    second = _publish(app_session, tenant_id, bot_id, b)
    assert _previous(app_session, tenant_id, bot_id, second) == first, "the second publish must name exactly the set the bot was serving"


def test_the_first_publish_has_nothing_to_restore(admin, app_session: Session) -> None:
    """Refusal is a first-class outcome: "no earlier set" is not "restored nothing"."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="Only document.")

    _scope(app_session, tenant_id)
    _publish(app_session, tenant_id, bot_id, a)

    with pytest.raises(knowledge.RollbackRefused) as refused:
        _rollback(app_session, tenant_id, bot_id)
    assert refused.value.code == "no_previous_set"
    assert _served(app_session, tenant_id, bot_id) == {a}, (
        "a refused rollback must not move the pointer"
    )


def test_nothing_published_is_refused_with_its_own_code(admin, app_session: Session) -> None:
    """``nothing_published`` and ``no_previous_set`` send the owner to different places."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    _version(admin, tenant_id, title="Indexed", body="Never published.")

    _scope(app_session, tenant_id)
    with pytest.raises(knowledge.RollbackRefused) as refused:
        _rollback(app_session, tenant_id, bot_id)
    assert refused.value.code == "nothing_published"


# --- the set comes back whole --------------------------------------------------


def test_rollback_restores_every_document_of_the_previous_set(
    admin, app_session: Session
) -> None:
    """The defect, inverted: three documents must come back together, not one.

    Before this row the rollback took a ``version_id`` and built a one-member set, so a
    five-document bot served **one** afterwards with a 200. This asserts the count and
    the membership of the restored set.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    docs = [
        _version(admin, tenant_id, title=f"D{i}", body=f"Body of document {i}.")
        for i in range(4)
    ]

    # Three publishes build a three-member set, then a fourth publish replaces it with a
    # four-member one. The previous set holds **3** documents, so a restore that returns
    # one (the defect) and a restore that returns the whole thing are different numbers,
    # not merely different orders.
    for version in docs:
        _publish(app_session, tenant_id, bot_id, version)
    assert _served(app_session, tenant_id, bot_id) == set(docs)

    set_id, members = _rollback(app_session, tenant_id, bot_id)
    app_session.commit()

    assert set(members) == set(docs[:3]), members
    assert len(members) == 3, "a restore that returns one document is the defect"
    assert _served(app_session, tenant_id, bot_id) == set(docs[:3])
    assert _active_set(app_session, tenant_id, bot_id) == set_id


def test_the_restored_documents_actually_answer(admin, app_session: Session) -> None:
    """The pointer is not the product: retrieval must find the restored documents.

    A restore that wrote a set and forgot to re-point, or that re-pointed at a set whose
    versions are not servable, passes every row-level assertion and answers nothing.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    shipping = _version(
        admin, tenant_id, title="Shipping", body="Standard shipping takes 3-5 business days."
    )
    refunds = _version(
        admin, tenant_id, title="Refunds", body="Refunds are processed within five business days."
    )
    warranty = _version(admin, tenant_id, title="Warranty", body="Twelve month warranty on electronics.")

    _scope(app_session, tenant_id)
    _publish(app_session, tenant_id, bot_id, shipping)
    _publish(app_session, tenant_id, bot_id, refunds)
    _publish(app_session, tenant_id, bot_id, warranty)
    _rollback(app_session, tenant_id, bot_id)
    app_session.commit()

    served = _served(app_session, tenant_id, bot_id)
    assert served == {shipping, refunds}, "the third publish is the one undone"

    config = pipeline.BotRetrievalConfig(confidence_threshold=0.0, rerank_enabled=False)
    for question, answer in (
        ("how long does shipping take", "3-5 business days"),
        ("refund processing time", "five business days"),
    ):
        result = pipeline.retrieve(
            app_session, tenant_id, HashingEmbedder(dimension=DIM).embed([question])[0],
            question, version_ids=list(served), config=config,
        )
        assert any(answer in chunk.text for chunk in result.chunks), question

    # And the undone document really is gone from the served scope, so the assertion
    # above is not "everything still answers".
    warranty_hits = pipeline.retrieve(
        app_session, tenant_id, HashingEmbedder(dimension=DIM).embed(["warranty"])[0],
        "what warranty do electronics have", version_ids=list(served), config=config,
    )
    assert not any("Twelve month" in chunk.text for chunk in warranty_hits.chunks)


def test_a_second_rollback_walks_one_more_set_back(admin, app_session: Session) -> None:
    """History is the record of publishes, so repeated restores are well defined.

    The alternative — writing a new set on each rollback — would make the second click
    restore the set the first click came from, and an owner could never walk back
    further than one step.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="First.")
    b = _version(admin, tenant_id, title="B", body="Second.")
    c = _version(admin, tenant_id, title="C", body="Third.")

    _scope(app_session, tenant_id)
    for version in (a, b, c):
        _publish(app_session, tenant_id, bot_id, version)
    assert len(_served(app_session, tenant_id, bot_id)) == 3

    _rollback(app_session, tenant_id, bot_id)
    app_session.commit()
    assert _served(app_session, tenant_id, bot_id) == {a, b}

    _rollback(app_session, tenant_id, bot_id)
    app_session.commit()
    assert _served(app_session, tenant_id, bot_id) == {a}

    with pytest.raises(knowledge.RollbackRefused) as refused:
        _rollback(app_session, tenant_id, bot_id)
    assert refused.value.code == "no_previous_set"
    assert _served(app_session, tenant_id, bot_id) == {a}


def test_a_rollback_writes_no_new_set(admin, app_session: Session) -> None:
    """Restore re-points at history; it does not append to it.

    Counted in SQL because the claim is about what is stored. A rollback that wrote a
    "Rolled back" set is what shipped before, and it is how a *subsequent* restore got
    its wrong answer.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="First.")
    b = _version(admin, tenant_id, title="B", body="Second.")

    _scope(app_session, tenant_id)
    _publish(app_session, tenant_id, bot_id, a)
    _publish(app_session, tenant_id, bot_id, b)
    app_session.commit()
    before = admin.execute(
        "SELECT count(*) FROM knowledge_sets WHERE bot_id = %s", (bot_id,)
    ).fetchone()[0]

    _rollback(app_session, tenant_id, bot_id)
    app_session.commit()

    after = admin.execute(
        "SELECT count(*) FROM knowledge_sets WHERE bot_id = %s", (bot_id,)
    ).fetchone()[0]
    assert after == before == 2, (before, after)
    labels = {
        row[0]
        for row in admin.execute(
            "SELECT label FROM knowledge_sets WHERE bot_id = %s", (bot_id,)
        ).fetchall()
    }
    assert "Rolled back" not in labels


# --- refusals ------------------------------------------------------------------


def test_a_previous_set_whose_documents_are_gone_is_refused(
    admin, app_session: Session
) -> None:
    """Restoring an empty set is the unpublish this row removes, wearing a new label."""
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="First.")
    b = _version(admin, tenant_id, title="B", body="Second.")

    _scope(app_session, tenant_id)
    first = _publish(app_session, tenant_id, bot_id, a)
    _publish(app_session, tenant_id, bot_id, b)
    app_session.commit()

    # The document behind the earlier set is deleted between the two publishes.
    admin.execute("DELETE FROM knowledge_set_members WHERE set_id = %s", (first,))
    admin.commit()

    _scope(app_session, tenant_id)
    with pytest.raises(knowledge.RollbackRefused) as refused:
        _rollback(app_session, tenant_id, bot_id)
    assert refused.value.code == "previous_set_empty"
    assert _served(app_session, tenant_id, bot_id) == {a, b}, (
        "the refusal must leave the live set exactly as it was"
    )


def test_an_edge_that_names_another_bots_set_is_not_followed(
    admin, app_session: Session
) -> None:
    """The FK cannot say "same bot", so the reader says it.

    Without the ``p.bot_id`` predicate, a corrupt or hand-edited edge would restore a
    sibling bot's documents into this one — and RLS would not object, because both bots
    belong to the same tenant.
    """
    tenant_id = _tenant(admin)
    mine = _bot(admin, tenant_id)
    sibling = _bot(admin, tenant_id)
    mine_version = _version(admin, tenant_id, title="Mine", body="My policy.")
    foreign_version = _version(admin, tenant_id, title="Sibling", body="Their policy.")

    _scope(app_session, tenant_id)
    _publish(app_session, tenant_id, mine, mine_version)
    current = _publish(app_session, tenant_id, mine, mine_version)
    foreign = seed_knowledge_set(admin, tenant_id, sibling, [foreign_version])
    admin.execute(
        "UPDATE knowledge_sets SET previous_set_id = %s WHERE id = %s", (foreign, current)
    )
    admin.commit()

    _scope(app_session, tenant_id)
    assert _previous(app_session, tenant_id, mine, current) is None
    with pytest.raises(knowledge.RollbackRefused):
        _rollback(app_session, tenant_id, mine)
    assert _served(app_session, tenant_id, mine) == {mine_version}


def test_another_tenants_set_is_never_resolved_as_the_edge(
    admin, app_session: Session
) -> None:
    """Both halves of the join carry the tenant predicate, and RLS is not the only guard.

    Measured as ``eva_app`` with the caller's tenant set, because that is the context the
    service runs in. ``eva`` is a superuser with BYPASSRLS, so reading through it would
    prove nothing.
    """
    mine = _tenant(admin, "mine")
    theirs = _tenant(admin, "theirs")
    their_bot = _bot(admin, theirs)
    their_version = _version(admin, theirs, title="Theirs", body="Their private policy.")
    with psycopg.connect(_admin_dsn(admin.info.dbname), autocommit=True) as other:
        their_set = seed_knowledge_set(other, theirs, their_bot, [their_version])

    my_bot = _bot(admin, mine)
    my_version = _version(admin, mine, title="Mine", body="My policy.")
    _scope(app_session, mine)
    current = _publish(app_session, mine, my_bot, my_version)
    admin.execute(
        "UPDATE knowledge_sets SET previous_set_id = %s WHERE id = %s",
        (their_set, current),
    )
    admin.commit()

    _scope(app_session, mine)
    assert _previous(app_session, mine, my_bot, current) is None


# --- publish's meaning is unchanged --------------------------------------------


def test_publish_is_still_additive(admin, app_session: Session) -> None:
    """The ruling's consequence 3: only rollback's meaning changed.

    Pinned next to the rollback tests so a later "restore = publish the previous set"
    shortcut cannot quietly make publishing replace instead of add.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="First.")
    b = _version(admin, tenant_id, title="B", body="Second.")

    _scope(app_session, tenant_id)
    _publish(app_session, tenant_id, bot_id, a)
    _publish(app_session, tenant_id, bot_id, b)
    assert _served(app_session, tenant_id, bot_id) == {a, b}


def test_the_pointer_is_still_one_column(admin, app_session: Session) -> None:
    """Atomic by construction (§10.5): the restore is one UPDATE, not a member rewrite.

    Asserted by writing an unrelated pointer value first and confirming the served set
    follows *only* ``bots.active_knowledge_set_id`` — a restore that mutated membership
    would still pass a count-based test.
    """
    tenant_id = _tenant(admin)
    bot_id = _bot(admin, tenant_id)
    a = _version(admin, tenant_id, title="A", body="First.")
    b = _version(admin, tenant_id, title="B", body="Second.")

    _scope(app_session, tenant_id)
    first = _publish(app_session, tenant_id, bot_id, a)
    _publish(app_session, tenant_id, bot_id, b)
    _rollback(app_session, tenant_id, bot_id)
    app_session.commit()

    assert _active_set(app_session, tenant_id, bot_id) == first
    assert _served(app_session, tenant_id, bot_id) == {a}
