"""OPS-11: conversation-level debugging is owner-only and leaves a receipt (§22).

§22's last bullet has two halves and they fail independently:

* **owner role** — the naive reading is "gate the transcript route", which would
  break the product (a viewer reads conversations by design, ADMN-06) and would also
  be the wrong control, because the transcript is not the observability surface. The
  facts §22 calls observability are which model served a turn, how slow it was and
  what it cost in tokens. This suite pins that those move behind the owner role, on
  **both** routes that can serve them, because a control with a second door is
  decorative.
* **is itself audit-logged** — the receipt must survive the request. A route that
  writes the audit row and never commits answers 200 and records nothing, which is
  the failure ``tenant_session`` invites.

Three assertions here are written to be able to fail rather than to look thorough:

* the audit row is read back through ``GET /audit-logs`` (a second request, own
  transaction) as well as straight from the table, so a row that exists only inside
  the uncommitted session is red;
* the audit detail is searched for the seeded visitor identifiers and for the message
  content, because ``audit_logs`` is append-only and never purged (R29) — a receipt
  quoting the conversation would deposit it in the one table nothing can delete, the
  same trap SEC-10's erasure route is built around;
* the export is asserted to *still* carry the telemetry for a non-owner, because
  widening the gate to the SAR mechanism (R32) would answer a legal request with a
  partial record.

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
FIXTURE = "obs-"

# Distinctive values so a leak is unambiguous rather than plausible.
VISITOR_NAME = "Synthetic Debug Visitor"
VISITOR_EMAIL = "debug-subject@example.test"
VISITOR_SESSION = "vs-debug-subject-0001"
PAGE_URL = "https://shop.example.test/debug-page"
CONTENT_CANARY = "CANARY-message-content-must-not-be-audited"

DEBUG_ACTION = "conversation.debug_viewed"


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def _purge_tenant(migrated_db: str, tenant_id: str) -> None:
    """Delete a synthetic tenant, working around the append-only audit log.

    ``audit_logs`` holds a FK to ``tenants`` (ON DELETE RESTRICT since 0003) and its
    trigger forbids DELETE, so the tenant cannot go while audit rows reference it.
    """
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
    email = f"{FIXTURE}{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": "Observability"},
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
    """Switch identity explicitly: a TestClient's cookie follows the last login."""
    client.cookies.clear()
    client.cookies.set("eva_session", token)


class Subject:
    def __init__(self, tenant: dict, ids: dict) -> None:
        self.tenant = tenant
        self.ids = ids

    @property
    def conversation_id(self) -> str:
        return self.ids["conversation"]

    @property
    def message_id(self) -> str:
        return self.ids["message"]

    @property
    def usage_id(self) -> str:
        return self.ids["usage"]


@pytest.fixture
def subject(client: TestClient, migrated_db: str) -> Subject:
    """One handed-over conversation holding an answered turn and a usage row.

    Seeded through SQL because the widget write path needs a verified domain
    (HALT-06) and because the turn loop cannot produce a real provider token count in
    this environment. The *reads* under test go through the real HTTP stack and RLS.
    """
    tenant = _register(client)
    tenant_id = tenant["tenant_id"]
    conversation_id, message_id, usage_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    start = datetime.now(UTC) - timedelta(hours=1)

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
                PAGE_URL, VISITOR_NAME, VISITOR_EMAIL, start, start + timedelta(minutes=2),
            ),
        )
        conn.execute(
            "INSERT INTO messages"
            " (id, tenant_id, conversation_id, role, content, citations, confidence,"
            "  latency_ms, prompt_tokens, completion_tokens, model, created_at)"
            " VALUES (%s,%s,%s,'assistant',%s,%s,0.82,412,120,40,'synthetic-model',%s)",
            (
                str(message_id), tenant_id, str(conversation_id), CONTENT_CANARY,
                json.dumps([{"title": "Shipping Policy"}]), start + timedelta(minutes=1),
            ),
        )
        conn.execute(
            "INSERT INTO usage_records"
            " (id, tenant_id, conversation_id, kind, provider, model, units, cost_micros)"
            " VALUES (%s,%s,%s,'chat','synthetic-provider','synthetic-model',160,3)",
            (str(usage_id), tenant_id, str(conversation_id)),
        )

    ids = {
        "conversation": str(conversation_id),
        "message": str(message_id),
        "usage": str(usage_id),
    }
    try:
        yield Subject(tenant, ids)
    finally:
        _purge_tenant(migrated_db, tenant_id)


def _set_role(migrated_db: str, user_id: str, role: str) -> None:
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        conn.commit()


def _debug_rows(migrated_db: str, tenant_id: str) -> list[dict]:
    """Every debug receipt for this tenant, read from the table as the owner role."""
    with psycopg.connect(_admin(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT action, actor_user_id, entity_type, entity_id, detail, ip"
            " FROM audit_logs WHERE tenant_id = %s AND action = %s"
            " ORDER BY created_at",
            (tenant_id, DEBUG_ACTION),
        ).fetchall()
    return [
        {
            "action": r[0],
            "actor_user_id": str(r[1]) if r[1] else None,
            "entity_type": r[2],
            "entity_id": str(r[3]) if r[3] else None,
            "detail": r[4],
            "ip": str(r[5]) if r[5] else None,
        }
        for r in rows
    ]


# --- the owner gets the diagnostics -----------------------------------------


def test_owner_reads_model_latency_and_tokens(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    response = client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["conversation_id"] == subject.conversation_id
    assert body["status"] == "handed_over"
    assert body["message_count"] == 1
    turn = body["turns"][0]
    assert turn["message_id"] == subject.message_id
    assert turn["model"] == "synthetic-model"
    assert turn["latency_ms"] == 412
    assert turn["prompt_tokens"] == 120
    assert turn["completion_tokens"] == 40


def test_owner_reads_the_usage_ledger_for_the_conversation(
    client: TestClient, subject: Subject
) -> None:
    """R31's ledger, scoped to this conversation rather than to the month."""
    _as(client, _login(client, subject.tenant["email"]))
    body = client.get(
        f"/api/v1/conversations/{subject.conversation_id}/debug"
    ).json()

    assert len(body["usage_records"]) == 1
    usage = body["usage_records"][0]
    assert usage["kind"] == "chat"
    assert usage["provider"] == "synthetic-provider"
    assert usage["model"] == "synthetic-model"
    assert usage["units"] == 160
    assert usage["cost_micros"] == 3


def test_debug_view_carries_no_content_and_no_visitor_identifiers(
    client: TestClient, subject: Subject
) -> None:
    """The debugging view is operational, so it must not become a second PII surface."""
    _as(client, _login(client, subject.tenant["email"]))
    raw = client.get(f"/api/v1/conversations/{subject.conversation_id}/debug").text

    for secret in (CONTENT_CANARY, VISITOR_NAME, VISITOR_EMAIL, VISITOR_SESSION, PAGE_URL):
        assert secret not in raw, f"debug view leaked {secret!r}"


# --- the role gate ----------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_non_owner_cannot_read_the_debug_view(
    client: TestClient, subject: Subject, migrated_db: str, role: str
) -> None:
    token = _login(client, subject.tenant["email"])
    _set_role(migrated_db, subject.tenant["user_id"], role)

    _as(client, token)
    response = client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"]["code"] == "forbidden"

    # And the refusal left no receipt: §22 asks for debugging access to be audited,
    # and nothing was granted. This pins the stated limit rather than hiding it.
    assert _debug_rows(migrated_db, subject.tenant["tenant_id"]) == []


def test_debug_view_requires_authentication(
    client: TestClient, subject: Subject
) -> None:
    client.cookies.clear()
    response = client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_transcript_view_hides_telemetry_below_owner(
    client: TestClient, subject: Subject, migrated_db: str, role: str
) -> None:
    """The second door: the transcript route served the same facts to any viewer.

    Content, citations, confidence and CSAT stay visible — that is the product
    (ADMN-06). Only the observability fields move.
    """
    token = _login(client, subject.tenant["email"])
    _set_role(migrated_db, subject.tenant["user_id"], role)

    _as(client, token)
    response = client.get(f"/api/v1/conversations/{subject.conversation_id}")
    assert response.status_code == 200, response.text
    turn = response.json()["turns"][0]

    assert turn["model"] is None
    assert turn["latency_ms"] is None
    assert turn["prompt_tokens"] is None
    assert turn["completion_tokens"] is None
    # Not gated, and asserted so the narrowing above cannot drift into the product.
    assert turn["content"] == CONTENT_CANARY
    assert turn["confidence"] == 0.82
    assert turn["citations"][0]["title"] == "Shipping Policy"


def test_transcript_view_keeps_telemetry_for_the_owner(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    turn = client.get(f"/api/v1/conversations/{subject.conversation_id}").json()[
        "turns"
    ][0]
    assert turn["model"] == "synthetic-model"
    assert turn["latency_ms"] == 412


def test_export_still_carries_telemetry_for_a_viewer(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """The SAR export (R32) is deliberately not narrowed by this row.

    Gating it would answer a data-subject access request with a partial record. If a
    later change routes the export through the same role check, this goes red and
    the conflict gets a ruling instead of being discovered by a subject.
    """
    token = _login(client, subject.tenant["email"])
    _set_role(migrated_db, subject.tenant["user_id"], "viewer")

    _as(client, token)
    payload = client.get(
        f"/api/v1/conversations/{subject.conversation_id}/export",
        params={"format": "json"},
    ).json()

    message = payload["messages"][0]
    assert message["model"] == "synthetic-model"
    assert message["latency_ms"] == 412
    assert message["prompt_tokens"] == 120
    assert payload["conversation"]["visitor_email"] == VISITOR_EMAIL


# --- tenant isolation -------------------------------------------------------


def test_another_tenants_conversation_is_404_not_403(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """A 403 would confirm the id exists, turning a guess into a known UUID."""
    other = _register(client)
    try:
        _set_role(migrated_db, other["user_id"], "owner")
        _as(client, _login(client, other["email"]))
        response = client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"]["code"] == "conversation_not_found"
        # No receipt for a read that returned nothing: the row it names is not this
        # tenant's, and recording the attempt would put another tenant's id in this
        # tenant's trail.
        assert _debug_rows(migrated_db, other["tenant_id"]) == []
    finally:
        _purge_tenant(migrated_db, other["tenant_id"])


def test_malformed_id_is_400_like_the_read_path(
    client: TestClient, subject: Subject
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    assert (
        client.get("/api/v1/conversations/not-a-uuid/debug").status_code == 400
    )


def test_rls_is_what_refuses_the_cross_tenant_row(
    subject: Subject, migrated_db: str
) -> None:
    """The unscoped read, as ``eva_app``, in a tenant scope that owns nothing.

    This is the assertion behind the tenant predicate's docstring. Dropping
    ``AND tenant_id = :tenant_id`` from the debug query leaves every HTTP test green,
    because RLS has already refused the row — so the isolation this suite relies on is
    a policy, and a lost policy would make the query *succeed and return too much*
    rather than fail. Same reasoning as the OPS-09 restore drill's unscoped read.
    """
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, dbname=migrated_db,
        user=APP_USER, password=APP_PASSWORD,
    ) as conn:
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, true)",
            (str(uuid.uuid4()),),  # a tenant that owns nothing
        )
        visible = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
        by_id = conn.execute(
            "SELECT count(*) FROM conversations WHERE id = %s",
            (subject.conversation_id,),
        ).fetchone()[0]

    assert visible == 0, "RLS leaked every conversation to a tenant scope that owns none"
    assert by_id == 0, "RLS leaked one specific conversation across the tenant boundary"


# --- the route is audit-logged, and the receipt is committed -----------------


def test_debug_read_is_audited_with_actor_and_target(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    _as(client, _login(client, subject.tenant["email"]))
    assert (
        client.get(f"/api/v1/conversations/{subject.conversation_id}/debug").status_code
        == 200
    )

    rows = _debug_rows(migrated_db, subject.tenant["tenant_id"])
    assert len(rows) == 1, "every granted debug read must leave exactly one receipt"
    row = rows[0]
    assert row["actor_user_id"] == subject.tenant["user_id"]
    assert row["entity_type"] == "conversation"
    assert row["entity_id"] == subject.conversation_id
    assert row["detail"] == {"turns": 1, "usage_records": 1}


def test_the_receipt_survives_the_request(client: TestClient, subject: Subject) -> None:
    """Read back through a second request: an uncommitted row dies with its session."""
    _as(client, _login(client, subject.tenant["email"]))
    client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")

    logs = client.get("/api/v1/audit-logs").json()
    assert any(
        entry["action"] == DEBUG_ACTION
        and entry["entity_id"] == subject.conversation_id
        for entry in logs
    ), "the audit row exists only inside the request's session"


def test_two_reads_leave_two_receipts(client: TestClient, subject: Subject) -> None:
    """A receipt per access, not per conversation: the trail is a history."""
    _as(client, _login(client, subject.tenant["email"]))
    for _ in range(2):
        client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")

    logs = client.get("/api/v1/audit-logs").json()
    assert len([e for e in logs if e["action"] == DEBUG_ACTION]) == 2


def test_the_receipt_does_not_copy_the_conversation_into_the_append_only_log(
    client: TestClient, subject: Subject, migrated_db: str
) -> None:
    """`audit_logs` is never purged, so a receipt quoting the record would undo erasure.

    Same rule SEC-10's erasure route follows. Asserted on the stored JSONB, not on
    the API's rendering of it.
    """
    _as(client, _login(client, subject.tenant["email"]))
    client.get(f"/api/v1/conversations/{subject.conversation_id}/debug")

    rows = _debug_rows(migrated_db, subject.tenant["tenant_id"])
    assert len(rows) == 1
    detail = json.dumps(rows[0]["detail"], sort_keys=True, default=str)

    for secret in (CONTENT_CANARY, VISITOR_NAME, VISITOR_EMAIL, VISITOR_SESSION, PAGE_URL):
        assert secret not in detail, f"audit detail recorded {secret!r}"
    assert set(rows[0]["detail"]) == {"turns", "usage_records"}
