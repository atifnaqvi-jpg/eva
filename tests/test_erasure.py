"""SEC-10: data-subject erasure, and the export that answers the access request.

ARCHITECTURE §19.3 defines the right in one line: *"deletion = delete conversation
records + visitor identifiers; data-export = transcript export (R32 doubles as the
SAR mechanism)"*. This suite defends both halves, and the three claims that make the
deletion half credible:

* **It deletes what it says it deletes.** Asserted by querying the child tables
  directly, not by reading the 204. A 204 from a route that commits nothing is the
  exact failure ``tenant_session`` invites (it closes without committing), and the
  response body cannot tell the two apart.
* **The visitor identifiers go with it.** ``visitor_session_id``, ``visitor_name``,
  ``visitor_email`` and ``page_url`` live on the conversation row, so a surviving
  conversation is a surviving subject record. The test searches the whole tenant for
  the identifiers rather than trusting the cascade to have reached them.
* **The erasure record does not undo the erasure.** ``audit_logs`` is append-only and
  never purged, so an audit row that quoted the name or address would keep the
  subject's data forever in the one table nothing can delete. The obvious way to
  write this route — "log the record we removed" — fails that test.

Fixtures are synthetic throughout, per the confidentiality charter.
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from fastapi.testclient import TestClient

from test_conversation import client  # noqa: F401  (module-scoped app/db fixture)

PASSWORD = "correct-horse-battery"
ERASURE_FIXTURE = "erase-"

VISITOR_NAME = "Synthetic Visitor"
VISITOR_EMAIL = "synthetic-subject@example.test"
VISITOR_SESSION = "vs-erasure-subject-0001"
PAGE_URL = "https://shop.example.test/checkout"


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def _purge_tenant(migrated_db: str, tenant_id: str) -> None:
    """Delete a synthetic tenant, working around the append-only audit log."""
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute("ALTER TABLE audit_logs DISABLE TRIGGER USER")
        conn.execute("DELETE FROM audit_logs WHERE tenant_id = %s", (tenant_id,))
        conn.execute("ALTER TABLE audit_logs ENABLE TRIGGER USER")
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def _session_token(response) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no session cookie")


def _register(client: TestClient) -> dict:
    email = f"{ERASURE_FIXTURE}{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": "Erasure"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return _session_token(response)


def _as(client: TestClient, token: str) -> None:
    """Switch identity explicitly.

    A TestClient's cookie follows the last login, so a cross-tenant probe written as
    "register A, register B, act as A" is silently acting as B.
    """
    client.cookies.clear()
    client.cookies.set("eva_session", token)


class Subject:
    """A tenant with one conversation holding every kind of visitor-linked row."""

    def __init__(self, tenant: dict, ids: dict) -> None:
        self.tenant = tenant
        self.ids = ids

    @property
    def conversation_id(self) -> str:
        return self.ids["conversation"]


@pytest.fixture
def subject(client: TestClient, migrated_db: str) -> Subject:
    """Seed through SQL: the visitor write paths need a verified domain (HALT-06)."""
    tenant = _register(client)
    tenant_id = tenant["tenant_id"]
    conversation_id = uuid.uuid4()
    start = datetime.now(UTC) - timedelta(hours=2)

    with psycopg.connect(_admin(migrated_db)) as conn:
        bot_id = conn.execute(
            "SELECT id FROM bots WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations"
            " (id, tenant_id, bot_id, visitor_session_id, status, page_url,"
            "  visitor_name, visitor_email, started_at, last_activity_at)"
            " VALUES (%s,%s,%s,%s,'handed_over',%s,%s,%s,%s,%s)",
            (
                str(conversation_id), tenant_id, str(bot_id), VISITOR_SESSION,
                PAGE_URL, VISITOR_NAME, VISITOR_EMAIL,
                start, start + timedelta(minutes=5),
            ),
        )
        message_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO messages (id, tenant_id, conversation_id, role, content)"
            " VALUES (%s,%s,%s,'visitor','Where is my order?')",
            (str(message_id), tenant_id, str(conversation_id)),
        )
        conn.execute(
            "INSERT INTO feedback (id, tenant_id, message_id, rating, comment)"
            " VALUES (%s,%s,%s,'down','It did not say')",
            (str(uuid.uuid4()), tenant_id, str(message_id)),
        )
        conn.execute(
            "INSERT INTO handovers"
            " (id, tenant_id, conversation_id, reason, channel, status, notified_at)"
            " VALUES (%s,%s,%s,'explicit','email','notified',%s)",
            (str(uuid.uuid4()), tenant_id, str(conversation_id), start),
        )
        conn.execute(
            "INSERT INTO visitor_files"
            " (id, tenant_id, conversation_id, storage_path, mime, size_bytes)"
            " VALUES (%s,%s,%s,%s,'image/png',2048)",
            (str(uuid.uuid4()), tenant_id, str(conversation_id), "synthetic/key"),
        )
        # Billing history must outlive the conversation, minus the link to it.
        usage_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO usage_records"
            " (id, tenant_id, conversation_id, kind, model, units, cost_micros)"
            " VALUES (%s,%s,%s,'chat','fake-model',42,7)",
            (str(usage_id), tenant_id, str(conversation_id)),
        )

    ids = {"conversation": str(conversation_id), "usage": str(usage_id)}
    try:
        yield Subject(tenant, ids)
    finally:
        _purge_tenant(migrated_db, tenant_id)


def _counts(migrated_db: str, conversation_id: str) -> dict:
    with psycopg.connect(_admin(migrated_db)) as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM conversations WHERE id = %s) AS conversations,"
            " (SELECT count(*) FROM messages WHERE conversation_id = %s) AS messages,"
            " (SELECT count(*) FROM feedback f JOIN messages m ON m.id = f.message_id"
            "   WHERE m.conversation_id = %s) AS feedback,"
            " (SELECT count(*) FROM handovers WHERE conversation_id = %s) AS handovers,"
            " (SELECT count(*) FROM visitor_files WHERE conversation_id = %s)"
            "   AS visitor_files",
            (conversation_id,) * 5,
        ).fetchone()
    return dict(
        zip(
            ("conversations", "messages", "feedback", "handovers", "visitor_files"),
            row,
        )
    )


def _identifiers_present(migrated_db: str, tenant_id: str) -> list[str]:
    """Every visitor identifier still findable anywhere in the tenant's rows."""
    found = []
    with psycopg.connect(_admin(migrated_db)) as conn:
        for value, column in (
            (VISITOR_EMAIL, "visitor_email"),
            (VISITOR_NAME, "visitor_name"),
            (VISITOR_SESSION, "visitor_session_id"),
            (PAGE_URL, "page_url"),
        ):
            hit = conn.execute(
                f"SELECT count(*) FROM conversations WHERE tenant_id = %s"
                f" AND {column} = %s",
                (tenant_id, value),
            ).fetchone()[0]
            if hit:
                found.append(column)
    return found


# --- erasure deletes what it claims -----------------------------------------


def test_erasure_removes_the_conversation_and_every_child_row(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    before = _counts(migrated_db, subject.conversation_id)
    assert before == {
        "conversations": 1, "messages": 1, "feedback": 1,
        "handovers": 1, "visitor_files": 1,
    }

    response = client.delete(f"/api/v1/conversations/{subject.conversation_id}")
    assert response.status_code == 204, response.text

    assert _counts(migrated_db, subject.conversation_id) == {
        "conversations": 0, "messages": 0, "feedback": 0,
        "handovers": 0, "visitor_files": 0,
    }


def test_erasure_removes_every_visitor_identifier(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """§19.3's "visitor identifiers", checked one column at a time.

    The identifiers are columns on the conversation, so this passes only if the row
    itself went. It is asserted separately anyway: a future "scrub in place" refactor
    would keep the test above green while leaving the subject's address behind.
    """
    _as(client, _login(client, subject.tenant["email"]))
    assert _identifiers_present(migrated_db, subject.tenant["tenant_id"]) == [
        "visitor_email", "visitor_name", "visitor_session_id", "page_url",
    ]

    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204

    assert _identifiers_present(migrated_db, subject.tenant["tenant_id"]) == []


def test_erasure_keeps_billing_totals_without_the_conversation_link(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """``usage_records.conversation_id`` is ON DELETE SET NULL, deliberately.

    Token and cost totals are the tenant's own accounting data and carry no visitor
    identifier, so erasure must not rewrite them — but the link to the erased
    conversation must not survive either.
    """
    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204

    with psycopg.connect(_admin(migrated_db)) as conn:
        row = conn.execute(
            "SELECT conversation_id, units, cost_micros FROM usage_records"
            " WHERE id = %s",
            (subject.ids["usage"],),
        ).fetchone()
    assert row is not None, "erasure deleted the tenant's billing record"
    assert row[0] is None, "the erased conversation is still linked from usage_records"
    assert (row[1], row[2]) == (42, 7)


def test_erased_conversation_is_gone_from_view_and_export(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204

    for path in (
        f"/api/v1/conversations/{subject.conversation_id}",
        f"/api/v1/conversations/{subject.conversation_id}/export",
        f"/api/v1/conversations/{subject.conversation_id}/export?format=json",
    ):
        response = client.get(path)
        assert response.status_code == 404, f"{path} still served the erased record"

    listed = client.get("/api/v1/conversations").json()
    assert listed["total"] == 0
    assert subject.conversation_id not in {i["id"] for i in listed["items"]}


def test_active_conversation_is_erasable(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """The retention purge skips live conversations; erasure must not.

    A subject request overrides the retention floor. Refusing while the visitor is
    still typing would deny the right for exactly as long as they keep talking.
    """
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "UPDATE conversations SET status = 'active', ended_at = NULL WHERE id = %s",
            (subject.conversation_id,),
        )
        conn.commit()

    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204
    assert _counts(migrated_db, subject.conversation_id)["conversations"] == 0


def test_second_erasure_is_404(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204
    again = client.delete(f"/api/v1/conversations/{subject.conversation_id}")
    assert again.status_code == 404
    assert again.json()["detail"]["error"]["code"] == "conversation_not_found"


# --- the erasure record ------------------------------------------------------


def test_erasure_is_audited_with_counts_and_no_visitor_data(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """The trail must say what went without keeping what was erased.

    ``audit_logs`` is append-only by trigger and never purged, so PII written here
    outlives the erasure it describes. The counts are the evidence; the name and
    address are exactly what the subject asked to be removed.
    """
    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete(
        f"/api/v1/conversations/{subject.conversation_id}"
    ).status_code == 204

    with psycopg.connect(_admin(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT entity_id, actor_user_id, detail::text FROM audit_logs"
            " WHERE tenant_id = %s AND action = 'conversation.erased'",
            (subject.tenant["tenant_id"],),
        ).fetchall()

    assert len(rows) == 1, "an erasure with no audit row is not auditable"
    entity_id, actor_user_id, detail = rows[0]
    assert str(entity_id) == subject.conversation_id
    assert str(actor_user_id) == subject.tenant["user_id"]
    assert json.loads(detail) == {
        "messages": 1, "feedback": 1, "handovers": 1, "visitor_files": 1,
    }

    for value in (VISITOR_NAME, VISITOR_EMAIL, VISITOR_SESSION, PAGE_URL):
        assert value not in detail, f"the erasure record retained {value!r}"


def test_failed_erasure_writes_no_audit_row(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """A 404 must not claim an erasure happened."""
    _as(client, _login(client, subject.tenant["email"]))
    response = client.delete(f"/api/v1/conversations/{uuid.uuid4()}")
    assert response.status_code == 404

    with psycopg.connect(_admin(migrated_db)) as conn:
        (rows,) = conn.execute(
            "SELECT count(*) FROM audit_logs"
            " WHERE tenant_id = %s AND action = 'conversation.erased'",
            (subject.tenant["tenant_id"],),
        ).fetchone()
    assert rows == 0


# --- isolation and access control -------------------------------------------


def test_other_tenant_conversation_is_404_not_403(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """A 403 would confirm the id exists, which is the enumeration oracle.

    Mutation-tested, and the result is worth stating: deleting the
    ``AND tenant_id = :tenant_id`` predicate from the DELETE does **not** leak,
    because RLS refuses the row anyway. So this test proves isolation holds, not
    which layer holds it — the same finding WP-19 recorded for the read path. The
    predicate stays because defence in depth is the point, not because this test
    can tell the two layers apart, and a change that relies on RLS alone should be
    a deliberate decision rather than an unnoticed one.
    """
    other_tenant, other_conversation = _seed_other_tenant(migrated_db)
    try:
        _as(client, _login(client, subject.tenant["email"]))
        response = client.delete(f"/api/v1/conversations/{other_conversation}")
        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "conversation_not_found"

        # And the other tenant's record is untouched, read straight from the table.
        with psycopg.connect(_admin(migrated_db)) as conn:
            (survived,) = conn.execute(
                "SELECT count(*) FROM conversations WHERE id = %s",
                (other_conversation,),
            ).fetchone()
        assert survived == 1, "another tenant's conversation was deleted"
    finally:
        _purge_tenant(migrated_db, other_tenant)


def test_viewer_cannot_erase(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """Reading the archive is not consent to destroy it."""
    token = _login(client, subject.tenant["email"])
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "UPDATE users SET role = 'viewer' WHERE id = %s",
            (subject.tenant["user_id"],),
        )
        conn.commit()

    _as(client, token)
    response = client.delete(f"/api/v1/conversations/{subject.conversation_id}")
    assert response.status_code == 403
    assert _counts(migrated_db, subject.conversation_id)["conversations"] == 1


def test_erasure_requires_authentication(
    client: TestClient, subject: Subject
) -> None:
    client.cookies.clear()
    response = client.delete(f"/api/v1/conversations/{subject.conversation_id}")
    assert response.status_code == 401


def test_malformed_id_is_400_the_same_as_on_the_read_path(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    assert client.delete("/api/v1/conversations/not-a-uuid").status_code == 400
    assert client.get("/api/v1/conversations/not-a-uuid").status_code == 400


def _seed_other_tenant(migrated_db: str) -> tuple[str, str]:
    tenant_id, bot_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'other-erasure-tenant')",
            (tenant_id,),
        )
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s,%s,%s,%s)",
            (str(bot_id), str(tenant_id), f"other-{uuid.uuid4().hex[:8]}", "Other Bot"),
        )
        conn.execute(
            "INSERT INTO conversations (id, tenant_id, bot_id, visitor_session_id)"
            " VALUES (%s,%s,%s,'other-subject-session')",
            (str(conversation_id), str(tenant_id), str(bot_id)),
        )
        conn.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content)"
            " VALUES (%s,%s,'visitor','another tenant is not yours to erase')",
            (str(tenant_id), str(conversation_id)),
        )
    return str(tenant_id), str(conversation_id)


# --- the export half of the right (R32 / SAR) --------------------------------


def test_json_export_carries_the_visitor_identifiers_and_file_metadata(
    client: TestClient, subject: Subject
) -> None:
    """The SAR answer must name every identifier erasure removes, and any file held.

    ``format_version`` is bumped because the payload grew: a consumer that reads
    version 1 keys is not silently given a different shape.
    """
    _as(client, _login(client, subject.tenant["email"]))
    response = client.get(
        f"/api/v1/conversations/{subject.conversation_id}/export",
        params={"format": "json"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["format_version"] == 2
    conversation = payload["conversation"]
    assert conversation["visitor_session_id"] == VISITOR_SESSION
    assert conversation["visitor_name"] == VISITOR_NAME
    assert conversation["visitor_email"] == VISITOR_EMAIL
    assert conversation["page_url"] == PAGE_URL

    # One upload exists in the fixture, so an empty list here would be a bug rather
    # than "no files". Metadata only — the bytes were never stored.
    assert len(payload["visitor_files"]) == 1
    file_record = payload["visitor_files"][0]
    assert file_record["mime"] == "image/png"
    assert file_record["size_bytes"] == 2048
    assert file_record["scan_status"] == "pending"
    assert "storage_path" not in file_record


def test_txt_export_names_the_visitor_session_and_file_count(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    text = client.get(
        f"/api/v1/conversations/{subject.conversation_id}/export"
    ).text

    assert f"Visitor session: {VISITOR_SESSION}" in text
    assert f"Visitor email: {VISITOR_EMAIL}" in text
    assert "Visitor files: 1" in text
    assert "image/png 2048 bytes" in text


def test_export_file_list_is_empty_not_absent_for_a_conversation_without_uploads(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """ "We hold no files" must be a statement the export makes, not an absent key."""
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "DELETE FROM visitor_files WHERE conversation_id = %s",
            (subject.conversation_id,),
        )
        conn.commit()

    _as(client, _login(client, subject.tenant["email"]))
    payload = client.get(
        f"/api/v1/conversations/{subject.conversation_id}/export",
        params={"format": "json"},
    ).json()
    assert payload["visitor_files"] == []
