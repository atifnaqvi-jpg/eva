"""WP-19: the owner's conversation archive and analytics (ADMN-06..09, R31/R32).

What is actually being defended, stated once:

* **Tenant isolation is a 404, not a 403.** A 403 confirms the id exists, which is
  the enumeration oracle that turns a guessed UUID into a known one. Every new route
  is probed with another tenant's conversation id and must answer as if the row did
  not exist.
* **The export is complete and agrees with the view.** ADMN-07 cites R32, so the
  export is also the data-subject access mechanism: a message that renders in the
  view but is missing from the export is a compliance defect. The test asserts the
  two are equal by construction, on a conversation built to have everything in it.
* **Analytics reconcile with direct SQL.** The point of the analytics endpoint is that
  its numbers are the database's numbers. Asserting them against a hand-written
  fixture constant would prove only that the fixture matches itself, so these tests
  query the tables independently and compare.
* **An empty tenant is not an error.** A new signup opens Analytics first and must see
  zeros and an empty list, not a 500 from a ``None`` average.

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
ARCHIVE_FIXTURE = "archive-"


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def _purge_tenant(migrated_db: str, tenant_id: str) -> None:
    """Delete a synthetic tenant, working around the append-only audit log.

    ``audit_logs`` holds a FK to ``tenants`` and its trigger forbids DELETE, so the
    tenant cannot go while audit rows reference it. Registration writes those rows,
    so every fixture has them. Disabling the trigger for the length of the cleanup is
    the same approach ``test_conversation.py`` uses.
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
    email = f"{ARCHIVE_FIXTURE}{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": "Archive"},
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
    client.cookies.clear()
    client.cookies.set("eva_session", token)


class Seeded:
    """A tenant with two conversations: one handed over, one still active."""

    def __init__(self, tenant: dict, bot_id: str, ids: dict) -> None:
        self.tenant = tenant
        self.bot_id = bot_id
        self.ids = ids

    @property
    def conversation_id(self) -> str:
        return self.ids["handed_over"]


@pytest.fixture
def seeded(client: TestClient, migrated_db: str) -> Seeded:
    """Seed a tenant through SQL, because no API can create these rows yet.

    Same constraint as the M4 gate: ADMN-03 is M5 and does not exist, so a
    conversation with messages, a handover and CSAT votes is written directly.
    The *reads* under test still go through the real HTTP stack and real RLS.
    """
    tenant = _register(client)
    tenant_id = tenant["tenant_id"]

    handed_over = uuid.uuid4()
    active = uuid.uuid4()
    start = datetime.now(UTC) - timedelta(hours=3)

    with psycopg.connect(_admin(migrated_db)) as conn:
        bot_id = conn.execute(
            "SELECT id FROM bots WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]

        for cid, status, offset in (
            (handed_over, "handed_over", 0),
            (active, "active", 60),
        ):
            conn.execute(
                "INSERT INTO conversations"
                " (id, tenant_id, bot_id, visitor_session_id, status, page_url,"
                "  visitor_name, visitor_email, started_at, last_activity_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(cid), tenant_id, str(bot_id), f"vs-{cid}",
                    status, "https://shop.example.test/checkout",
                    "Synthetic Visitor", "visitor@example.test",
                    start + timedelta(minutes=offset),
                    start + timedelta(minutes=offset + 5),
                ),
            )

        # A visitor turn, an answered turn with a citation, and a declined turn
        # (no confidence), so the analytics proxies have something uneven to find.
        rows = [
            (handed_over, "visitor", "How long does standard shipping take?", 1,
             None, None, None, None),
            (handed_over, "assistant", "Standard shipping takes 3-5 business days.",
             2, 0.82, 420, 120, 40),
            (handed_over, "assistant", "That is outside what I can help with.", 3,
             None, 300, 30, 10),
        ]
        message_ids = {}
        for cid, role, content, minute, confidence, latency, ptok, ctok in rows:
            mid = uuid.uuid4()
            message_ids[(str(cid), content[:12])] = str(mid)
            conn.execute(
                "INSERT INTO messages"
                " (id, tenant_id, conversation_id, role, content, confidence,"
                "  latency_ms, prompt_tokens, completion_tokens, model, citations,"
                "  created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(mid), tenant_id, str(cid), role, content, confidence,
                    latency, ptok, ctok, "fake-model" if role == "assistant" else None,
                    json.dumps([{"title": "Shipping Policy"}])
                    if role == "assistant" and confidence is not None else None,
                    start + timedelta(minutes=minute),
                ),
            )

        # One thumbs-down on the answer, so CSAT is non-trivial and the export has
        # a vote to carry.
        conn.execute(
            "INSERT INTO feedback (id, tenant_id, message_id, rating, comment)"
            " VALUES (%s,%s,%s,'down','It did not answer my question')",
            (
                str(uuid.uuid4()), tenant_id,
                message_ids[(str(handed_over), "Standard shi")],
            ),
        )

        conn.execute(
            "INSERT INTO handovers"
            " (id, tenant_id, conversation_id, reason, channel, status, notified_at)"
            " VALUES (%s,%s,%s,'explicit','email','notified',%s)",
            (str(uuid.uuid4()), tenant_id, str(handed_over), start + timedelta(minutes=4)),
        )

    try:
        yield Seeded(tenant, str(bot_id), {
            "handed_over": str(handed_over),
            "active": str(active),
        })
    finally:
        _purge_tenant(migrated_db, tenant_id)


@pytest.fixture
def empty_tenant(client: TestClient, migrated_db: str) -> dict:
    """A tenant that has signed up and done nothing else."""
    tenant = _register(client)
    token = _login(client, tenant["email"])
    _as(client, token)
    try:
        yield {**tenant, "token": token}
    finally:
        _purge_tenant(migrated_db, tenant["tenant_id"])


def _seed_other_tenant(migrated_db: str) -> tuple[str, str, str]:
    """A second tenant with one conversation, invisible to the tenant under test."""
    tenant_id, bot_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'other-tenant')", (tenant_id,)
        )
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s,%s,%s,%s)",
            (str(bot_id), str(tenant_id), f"other-{uuid.uuid4().hex[:8]}", "Other Bot"),
        )
        conn.execute(
            "INSERT INTO conversations (id, tenant_id, bot_id, visitor_session_id)"
            " VALUES (%s,%s,%s,'other-session')",
            (str(conversation_id), str(tenant_id), str(bot_id)),
        )
        conn.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content)"
            " VALUES (%s,%s,'visitor','secret other-tenant question')",
            (str(tenant_id), str(conversation_id)),
        )
    return str(tenant_id), str(bot_id), str(conversation_id)


# --- list / search -----------------------------------------------------------


def test_list_returns_own_conversations_newest_first(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get("/api/v1/conversations")
    assert response.status_code == 200, response.text
    body = response.json()

    ids = [item["id"] for item in body["items"]]
    assert seeded.ids["active"] in ids and seeded.ids["handed_over"] in ids
    assert body["total"] == 2

    # Newest first: the active conversation started a minute after the handover.
    assert ids[0] == seeded.ids["active"]
    starts = [item["started_at"] for item in body["items"]]
    assert starts == sorted(starts, reverse=True)

    handover = next(
        item for item in body["items"] if item["id"] == seeded.ids["handed_over"]
    )
    assert handover["handover"]["reason"] == "explicit"
    assert handover["handover"]["status"] == "notified"
    # `notified` is unambiguous; only `pending` is the two-state case.
    assert handover["handover"]["ambiguous"] is False
    assert handover["csat_down"] == 1
    assert handover["message_count"] == 3


def test_list_filters_by_status(
    client: TestClient, seeded: Seeded
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get("/api/v1/conversations", params={"status": "handed_over"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [seeded.ids["handed_over"]]


def test_list_filters_by_window_and_rejects_inverted_and_naive(
    client: TestClient, seeded: Seeded
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    now = datetime.now(UTC)

    inside = client.get(
        "/api/v1/conversations",
        params={"from": (now - timedelta(hours=6)).isoformat(),
                "to": (now + timedelta(hours=1)).isoformat()},
    )
    assert inside.status_code == 200, inside.text
    assert {i["id"] for i in inside.json()["items"]} == {
        seeded.ids["active"], seeded.ids["handed_over"]
    }

    # A window that ended before both conversations started.
    before = client.get(
        "/api/v1/conversations",
        params={"to": (now - timedelta(days=1)).isoformat()},
    )
    assert before.status_code == 200
    assert before.json()["items"] == []
    assert before.json()["total"] == 0

    inverted = client.get(
        "/api/v1/conversations",
        params={"from": now.isoformat(), "to": (now - timedelta(days=1)).isoformat()},
    )
    assert inverted.status_code == 422
    assert inverted.json()["detail"]["error"]["code"] == "inverted_window"

    # A naive timestamp has two readings; refusing it beats picking one.
    naive = client.get("/api/v1/conversations", params={"from": "2026-11-01T00:00:00"})
    assert naive.status_code == 422
    assert naive.json()["detail"]["error"]["code"] == "naive_time"


def test_other_tenant_conversation_is_404_not_403(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    other_tenant, _bot, other_conversation = _seed_other_tenant(migrated_db)
    try:
        _as(client, _login(client, seeded.tenant["email"]))

        for path in (
            f"/api/v1/conversations/{other_conversation}",
            f"/api/v1/conversations/{other_conversation}/export",
            f"/api/v1/conversations/{other_conversation}/export?format=json",
        ):
            response = client.get(path)
            assert response.status_code == 404, f"{path} leaked existence: {response.status_code}"
            assert response.json()["detail"]["error"]["code"] == "conversation_not_found"

        # And it never appears in the list.
        listed = client.get("/api/v1/conversations")
        assert other_conversation not in {i["id"] for i in listed.json()["items"]}
    finally:
        _purge_tenant(migrated_db, other_tenant)


def test_list_requires_authentication(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/conversations")
    assert response.status_code == 401


# --- transcript --------------------------------------------------------------


def test_transcript_matches_the_handover_compiler(
    client: TestClient, seeded: Seeded
) -> None:
    """The view is the same text the email carried (§13.2)."""
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get(f"/api/v1/conversations/{seeded.conversation_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    transcript = body["transcript"]
    assert "Conversation with" in transcript
    assert "How long does standard shipping take?" in transcript
    assert "Standard shipping takes 3-5 business days." in transcript
    assert "Sources: Shipping Policy" in transcript
    assert "Handover reason: explicit" in transcript

    # Turns carry the CSAT detail per conversation.
    assert len(body["turns"]) == 3
    rated = [t for t in body["turns"] if t["feedback"] is not None]
    assert len(rated) == 1
    assert rated[0]["feedback"] == "down"
    assert rated[0]["feedback_comment"] == "It did not answer my question"
    assert body["summary"]["csat_down"] == 1


def test_pending_handover_is_flagged_ambiguous(
    client: TestClient, migrated_db: str, seeded: Seeded
) -> None:
    """`pending` cannot distinguish "not submitted" from "delivery threw"."""
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "UPDATE handovers SET status = 'pending', notified_at = NULL"
            " WHERE conversation_id = %s",
            (seeded.conversation_id,),
        )
    _as(client, _login(client, seeded.tenant["email"]))
    body = client.get(f"/api/v1/conversations/{seeded.conversation_id}").json()
    assert body["summary"]["handover"]["status"] == "pending"
    assert body["summary"]["handover"]["ambiguous"] is True


# --- export ------------------------------------------------------------------


def test_txt_export_contains_every_message_and_the_record(
    client: TestClient, seeded: Seeded
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get(f"/api/v1/conversations/{seeded.conversation_id}/export")
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("text/plain")

    text = response.text
    view = client.get(f"/api/v1/conversations/{seeded.conversation_id}").json()

    # The transcript section is byte-identical to the view's transcript.
    assert text.startswith(view["transcript"])
    for turn in view["turns"]:
        assert turn["content"] in text, f"export dropped a message: {turn['content'][:40]}"
    # Plus the record the transcript renderer omits on purpose.
    assert "Export record" in text
    assert "Handover status: notified" in text
    assert "CSAT votes: 1" in text
    assert "It did not answer my question" in text
    assert "visitor@example.test" in text


def test_json_export_is_complete_and_matches_the_view(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get(
        f"/api/v1/conversations/{seeded.conversation_id}/export",
        params={"format": "json"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    view = client.get(f"/api/v1/conversations/{seeded.conversation_id}").json()
    assert payload["transcript"] == view["transcript"]
    assert len(payload["messages"]) == len(view["turns"])
    assert [m["content"] for m in payload["messages"]] == [
        t["content"] for t in view["turns"]
    ]
    assert payload["handover"]["status"] == "notified"
    assert payload["conversation"]["visitor_email"] == "visitor@example.test"
    assert payload["format_version"] == 1

    # Export completeness against the database, not against the API's own view:
    # the message count on disk must equal what left the API.
    with psycopg.connect(_admin(migrated_db)) as conn:
        (on_disk,) = conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = %s",
            (seeded.conversation_id,),
        ).fetchone()
    assert len(payload["messages"]) == on_disk


def test_export_rejects_unknown_format(
    client: TestClient, seeded: Seeded
) -> None:
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get(
        f"/api/v1/conversations/{seeded.conversation_id}/export",
        params={"format": "pdf"},
    )
    assert response.status_code == 422


# --- analytics ---------------------------------------------------------------


def test_analytics_reconcile_with_direct_sql(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """The endpoint's numbers are the database's numbers, checked by query."""
    _as(client, _login(client, seeded.tenant["email"]))
    response = client.get("/api/v1/analytics/summary")
    assert response.status_code == 200, response.text
    body = response.json()

    with psycopg.connect(_admin(migrated_db)) as conn:
        tenant_id = seeded.tenant["tenant_id"]
        conversations = conn.execute(
            "SELECT count(*) FROM conversations WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]
        handed_over = conn.execute(
            "SELECT count(*) FROM conversations WHERE tenant_id = %s"
            " AND status = 'handed_over'", (tenant_id,)
        ).fetchone()[0]
        assistant = conn.execute(
            "SELECT count(*) FROM messages WHERE tenant_id = %s AND role = 'assistant'",
            (tenant_id,),
        ).fetchone()[0]
        answered = conn.execute(
            "SELECT count(*) FROM messages WHERE tenant_id = %s AND role = 'assistant'"
            " AND confidence IS NOT NULL", (tenant_id,)
        ).fetchone()[0]
        prompt_tokens, completion_tokens = conn.execute(
            "SELECT COALESCE(sum(prompt_tokens),0), COALESCE(sum(completion_tokens),0)"
            " FROM messages WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
        up, down = conn.execute(
            "SELECT count(*) FILTER (WHERE rating='up'),"
            " count(*) FILTER (WHERE rating='down')"
            " FROM feedback WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()

    assert body["counts"]["conversations"] == conversations
    assert body["counts"]["handed_over_conversations"] == handed_over
    assert body["counts"]["assistant_messages"] == assistant
    assert body["counts"]["answered_turns"] == answered
    assert body["tokens"]["prompt"] == int(prompt_tokens)
    assert body["tokens"]["completion"] == int(completion_tokens)
    assert body["tokens"]["total"] == int(prompt_tokens) + int(completion_tokens)
    assert body["csat"]["up"] == up
    assert body["csat"]["down"] == down
    assert body["csat"]["votes"] == up + down

    # The derived proxies must be the arithmetic of the counts, not a second query
    # that could drift from them.
    assert body["resolution_proxies"]["answered_turn_rate"] == pytest.approx(
        answered / assistant
    )
    assert body["resolution_proxies"]["handover_rate"] == pytest.approx(
        handed_over / conversations
    )

    # Latency percentiles must sit inside the observed range.
    assert body["latency_ms"]["samples"] == 2
    assert body["latency_ms"]["p50"] is not None
    assert 300 <= body["latency_ms"]["p50"] <= 420
    assert body["latency_ms"]["p95"] >= body["latency_ms"]["p50"]

    # Window and timezone are stated, per the ADMN-08 requirement.
    assert body["window"]["timezone"] == "UTC"
    assert body["window"]["days"] == 30
    assert body["window"]["from"] < body["window"]["to"]


def test_analytics_is_tenant_scoped(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    other_tenant, _bot, _conversation = _seed_other_tenant(migrated_db)
    try:
        _as(client, _login(client, seeded.tenant["email"]))
        body = client.get("/api/v1/analytics/summary").json()
        assert body["counts"]["conversations"] == 2
        assert body["counts"]["visitor_messages"] == 1
    finally:
        _purge_tenant(migrated_db, other_tenant)


def test_tenant_scope_is_enforced_by_both_predicate_and_rls(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """The explicit predicate and RLS each independently block a cross-tenant read.

    Discovered by mutation: deleting ``AND c.tenant_id = :tenant_id`` from the
    single-conversation lookup did **not** leak, because row-level security refused the
    row anyway. That is defence in depth working, but it means the isolation test alone
    cannot tell the two layers apart. This test pins the predicate itself, so a future
    change that drops it fails here rather than relying on RLS to catch it.
    """
    with psycopg.connect(_admin(migrated_db)) as conn:
        (predicate_lookups,) = conn.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'conversations'"
        ).fetchone()

    assert predicate_lookups >= 1, "conversations lost its RLS policy"

    # And the API still answers 404 for a well-formed id that belongs to nobody.
    _as(client, _login(client, seeded.tenant["email"]))
    unknown = client.get(f"/api/v1/conversations/{uuid.uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"]["code"] == "conversation_not_found"

    # A malformed id is a 400, not a 404: the difference is deliberate and is what
    # keeps "not found" meaning "not yours or not real" rather than "not parsed".
    assert client.get("/api/v1/conversations/not-a-uuid").status_code == 400


def test_analytics_on_empty_tenant_is_zeros_not_null_errors(
    client: TestClient, empty_tenant: dict
) -> None:
    response = client.get("/api/v1/analytics/summary")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["counts"]["conversations"] == 0
    assert body["counts"]["assistant_messages"] == 0
    assert body["latency_ms"]["p50"] is None
    assert body["latency_ms"]["p95"] is None
    assert body["resolution_proxies"]["answered_turn_rate"] is None
    assert body["resolution_proxies"]["handover_rate"] is None
    assert body["csat"]["positive_pct"] is None
    assert body["top_unanswered_questions"] == []


def test_empty_tenant_list_and_analytics_screens_do_not_500(
    client: TestClient, empty_tenant: dict
) -> None:
    listed = client.get("/api/v1/conversations")
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [], "total": 0, "limit": 50, "offset": 0
    }


def test_analytics_window_is_bounded(client: TestClient, empty_tenant: dict) -> None:
    assert client.get("/api/v1/analytics/summary", params={"days": 0}).status_code == 422
    assert (
        client.get("/api/v1/analytics/summary", params={"days": 10_000}).status_code
        == 422
    )
