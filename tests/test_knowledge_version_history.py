"""ADMN-15 evidence: the knowledge screen cannot name a "previous version".

The row asks for a "restore previous version" action beside publish. For that control to
be honest, the version it offers has to come from somewhere an owner can see. This file
proves it does not: `GET /knowledge/documents` — the only listing the screen reads — is
`LEFT JOIN LATERAL (... ORDER BY version_no DESC LIMIT 1)`, so a document with five
versions appears once, as the newest.

The other two halves of the blocker need a live stack and are measured by
`scripts/admn15_evidence.py`: `/knowledge/rollback` serves the named version *alone*
(discarding the rest of the set), and re-publishing an older version is additive rather
than a restore. Together the three mean a "restore previous version" button would either
offer nothing, or silently unpublish the owner's other documents.

Kept as a real test rather than a comment because the claim is an API shape: if a later
work package adds a version-history endpoint, this goes red and the halt should be
re-read.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from conftest import _admin_dsn
from app.knowledge import service as knowledge

_SEEDED: list[uuid.UUID] = []


@pytest.fixture
def admin(migrated_db: str):
    _SEEDED.clear()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        yield conn
    with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (list(_SEEDED),))


@pytest.fixture
def session(migrated_db: str):
    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + migrated_db
    )
    active = Session(engine)
    try:
        yield active
    finally:
        active.rollback()
        active.close()
        engine.dispose()


def _scope(session: Session, tenant_id: uuid.UUID) -> None:
    """RLS is in force as ``eva_app`` (§16.3), so the query needs the tenant set first.

    Without this the listing is empty for the *right* reason and every assertion here
    would pass on nothing — the vacuous-green trap §8.19 and §8.25 record.
    """
    session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
    )


def _tenant_with_document(conn, *, versions: int) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    _SEEDED.append(tenant_id)
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'history')", (tenant_id,))

    source_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'crawl')",
        (source_id, tenant_id),
    )
    document_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title, uri)"
        " VALUES (%s,%s,%s,'Shipping','shipping.txt')",
        (document_id, tenant_id, source_id),
    )
    for number in range(1, versions + 1):
        conn.execute(
            "INSERT INTO document_versions (id, tenant_id, document_id, version_no,"
            " content_hash, status) VALUES (%s,%s,%s,%s,%s,'ready')",
            (uuid.uuid4(), tenant_id, document_id, number, f"h{number}"),
        )
    # The app session is a separate connection, so the seed has to be visible to it.
    conn.commit()
    return tenant_id, document_id


def test_the_listing_reports_one_row_per_document_not_per_version(
    admin, session: Session
) -> None:
    """Three versions exist; the screen is offered one, and it is the newest.

    This is the affordance gap: a control needs an earlier version to name, and the only
    endpoint the knowledge screen reads cannot produce one.
    """
    tenant_id, document_id = _tenant_with_document(admin, versions=3)

    _scope(session, tenant_id)
    rows = knowledge.list_documents(session, tenant_id)
    mine = [row for row in rows if str(row["id"]) == str(document_id)]

    assert len(mine) == 1, "one row per document, whatever its version count"
    assert mine[0]["version_no"] == 3, "and it is the newest version"

    # The earlier versions do exist — the listing is hiding them, not the data.
    count = admin.execute(
        "SELECT count(*) FROM document_versions WHERE document_id = %s", (document_id,)
    ).fetchone()[0]
    assert count == 3


def test_the_only_version_id_the_screen_sees_is_the_newest(
    admin, session: Session
) -> None:
    """The id is what a control would post, so the *id* is the part that matters.

    Even if a screen invented a "previous" label, it has no id to send for the older
    version: the listing's `version_id` is the v3 row's id and v1/v2 never appear.
    """
    tenant_id, document_id = _tenant_with_document(admin, versions=2)

    _scope(session, tenant_id)
    rows = knowledge.list_documents(session, tenant_id)
    visible = {str(row["version_id"]) for row in rows if str(row["id"]) == str(document_id)}

    all_versions = {
        str(row[0])
        for row in admin.execute(
            "SELECT id FROM document_versions WHERE document_id = %s", (document_id,)
        ).fetchall()
    }
    assert len(visible) == 1
    # Exactly one of the two real versions is reachable through the listing.
    assert visible < all_versions
    assert len(all_versions - visible) == 1


def test_an_empty_document_still_appears_once(admin, session: Session) -> None:
    """The LEFT JOIN is why a document with no version is listed rather than dropped —
    the same join that hides the older versions. Recorded so a future "return all
    versions" change does not silently break this case."""
    tenant_id = uuid.uuid4()
    _SEEDED.append(tenant_id)
    admin.execute("INSERT INTO tenants (id, name) VALUES (%s,'empty')", (tenant_id,))
    source_id = uuid.uuid4()
    admin.execute(
        "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'upload')",
        (source_id, tenant_id),
    )
    document_id = uuid.uuid4()
    admin.execute(
        "INSERT INTO documents (id, tenant_id, source_id, title)"
        " VALUES (%s,%s,%s,'No versions')",
        (document_id, tenant_id, source_id),
    )
    admin.commit()

    _scope(session, tenant_id)
    rows = [row for row in knowledge.list_documents(session, tenant_id)
            if str(row["id"]) == str(document_id)]
    assert len(rows) == 1
    assert rows[0]["version_id"] is None
