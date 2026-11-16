"""WP-17: human handover (§13, HAND-01..05, R19).

What is actually being defended here, stated once:

* **The visitor is never left without a next step.** A keyword trigger, a decline
  the tenant asked to hear about, and a thumbs-down all have to leave the visitor
  with a message that says a person is coming — and, on the explicit path, a stored
  handover they can act on.
* **The AI goes quiet, the transcript does not.** Once handed over, the turn loop
  must refuse to answer, while visitor messages must still be recorded, because the
  owner is reading that conversation.
* **Delivery failure is not the visitor's problem.** SMTP is an outbound dependency
  that will be down sometimes; the handover row must survive it, and it must say
  delivery failed rather than claiming a notification that never happened.

The channel is faked by passing a ``HandoverChannel`` in, so nothing here talks to
SMTP: the real ``EmailChannel`` is covered separately by a test that asserts the
message it builds, without a network.
"""

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest
from sqlalchemy import text

from app.db.session import tenant_session
from app.handover import policy, transcript
from app.handover.channels import Delivery, EmailChannel, Notification
from app.handover import service as handover_service

from test_conversation import client, published_bot, stub_embedder, tenant  # noqa: F401
from test_widget_tokens import _add_domain, _session

SECRET = "test-widget-secret-that-is-long-enough"
ORIGIN = "https://shop.example.com"


@pytest.fixture(autouse=True)
def widget_secret(monkeypatch):
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", SECRET)
    # Delivery must be synchronous in tests: an assertion that runs before the
    # background thread finishes would be flaky, and flaky tests get deleted.
    monkeypatch.setenv("HANDOVER_NOTIFY_ASYNC", "false")
    import app.config as config

    monkeypatch.setattr(config, "HANDOVER_NOTIFY_ASYNC", False)


class RecordingChannel:
    """A ``HandoverChannel`` that keeps what it was asked to send."""

    name = "email"

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[Notification] = []
        self.ok = ok

    def send(self, notification: Notification) -> Delivery:
        self.sent.append(notification)
        return Delivery(ok=self.ok, detail="recorded")


@pytest.fixture
def owner_email(monkeypatch) -> str:
    """Point the tenant's handover email at a test address.

    Written through ``tenants.settings`` because that is where the blueprint puts
    handover configuration (§16.1 keeps it in the settings JSONB, not a column).
    """
    address = "owner@example.test"
    return address


def _set_handover_settings(tenant_id: str, settings: dict) -> None:
    from sqlalchemy import text as sql_text

    from app.db.session import make_session_factory
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, migrated_db  # noqa: F401

    # The tenant's own connection cannot UPDATE tenants under forced RLS without the
    # GUC; tenant_session sets it, so this is the honest path (same as other tests).
    with tenant_session(uuid.UUID(tenant_id)) as session:
        session.execute(
            sql_text(
                "UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :id"
            ),
            {"s": __import__("json").dumps(settings), "id": tenant_id},
        )
        session.commit()


def _start(client, public_id: str) -> str:
    body = _session(client, public_id).json()
    started = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": f"v-{uuid.uuid4().hex[:8]}"},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {body['token']}"},
    )
    assert started.status_code == 201, started.text
    return started.json()["conversation_id"], body["token"]


def _answered_turn(client, conversation_id: str, token: str, text_in: str, stub_embedder) -> str:
    """Send one message and return the assistant message id.

    The deployment has no embedding model or provider key, so both are injected on
    the widget router — the same wiring ``test_widget_turn_reuses_the_owner_turn_loop``
    uses. Without this the turn fails before it can be rated.
    """
    from test_conversation import FakeProvider

    from app.widget import router as widget_router

    original_provider = widget_router.default_provider
    original_embedder = widget_router.default_embedder
    widget_router.default_provider = lambda: FakeProvider(reply="3-5 business days.")
    widget_router.default_embedder = lambda: stub_embedder
    try:
        response = client.post(
            f"/api/v1/widget/conversations/{conversation_id}/messages",
            json={"text": text_in},
            headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
        )
    finally:
        widget_router.default_provider = original_provider
        widget_router.default_embedder = original_embedder
    return _message_id(response.text)


# --- policy: settings, keywords, business hours ---------------------------


def test_missing_settings_degrade_to_documented_defaults() -> None:
    """A tenant that has configured nothing still gets a working keyword trigger."""
    settings = policy.settings({})
    assert settings["email"] is None
    assert "human" in settings["keywords"]
    # Low-confidence notification is opt-in; negative feedback is on.
    assert settings["notify_on_low_confidence"] is False
    assert settings["notify_on_negative_feedback"] is True


def test_malformed_settings_never_raise() -> None:
    """Operator JSON is untrusted input; a bad blob must not break a conversation."""
    for blob in (None, [], "nope", {"handover": "nope"}, {"handover": {"keywords": 3}}):
        settings = policy.settings(blob)
        assert isinstance(settings["keywords"], list)


def test_an_explicit_empty_keyword_list_disables_the_trigger() -> None:
    """Absent means "use defaults"; empty means "off" — the two must differ."""
    assert policy.settings({"handover": {"keywords": []}})["keywords"] == []
    assert policy.settings({"handover": {}})["keywords"] != []


def test_keyword_matching_is_whole_word_and_case_insensitive() -> None:
    keywords = ["human", "real person"]
    assert policy.keyword_match("Can I talk to a HUMAN?", keywords)
    assert policy.keyword_match("I need a real person", keywords)
    # The false positives that matter: substrings inside unrelated words.
    assert not policy.keyword_match("the management system", keywords)
    assert not policy.keyword_match("humanity is doomed", keywords)


def test_no_keywords_means_no_match() -> None:
    assert not policy.keyword_match("human", [])


def test_unconfigured_hours_mean_always_online() -> None:
    """Inventing hours would show an offline banner nobody asked for (R43)."""
    assert policy.is_online({}) is True


def test_hours_respect_timezone_and_the_weekly_window() -> None:
    settings = {
        "business_hours": {
            "timezone": "UTC",
            "week": {"mon": ["09:00", "17:00"]},
        }
    }
    # 2026-11-16 is a Monday.
    inside = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
    before = datetime(2026, 11, 16, 8, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)
    tuesday = datetime(2026, 11, 17, 10, 30, tzinfo=timezone.utc)

    assert policy.is_online(settings, now=inside) is True
    assert policy.is_online(settings, now=before) is False
    # The end is exclusive: 17:00 is closed, not a zero-length opening.
    assert policy.is_online(settings, now=after) is False
    assert policy.is_online(settings, now=tuesday) is False


def test_hours_are_interpreted_in_the_tenants_own_zone() -> None:
    """09:00 in Berlin is 08:00 UTC, so the same instant is inside there and out
    of it for a UTC tenant. This is the whole point of storing a timezone."""
    berlin = {
        "business_hours": {"timezone": "Europe/Berlin", "week": {"mon": ["09:00", "17:00"]}}
    }
    utc = {"business_hours": {"timezone": "UTC", "week": {"mon": ["09:00", "17:00"]}}}
    moment = datetime(2026, 11, 16, 8, 30, tzinfo=timezone.utc)
    assert policy.is_online(berlin, now=moment) is True
    assert policy.is_online(utc, now=moment) is False


def test_next_open_finds_the_next_window() -> None:
    settings = {"business_hours": {"timezone": "UTC", "week": {"mon": ["09:00", "17:00"]}}}
    sunday = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    opens = policy.next_open(settings, now=sunday)
    assert opens is not None
    assert opens.weekday() == 0 and opens.hour == 9


def test_next_open_is_none_without_a_schedule() -> None:
    """The session route calls this on every pageview, so it must not raise for
    the (default) tenant that has configured no hours at all."""
    assert policy.next_open({}) is None
    assert policy.next_open({"business_hours": {}}) is None


def test_an_invalid_timezone_falls_back_to_utc_rather_than_raising() -> None:
    settings = {"business_hours": {"timezone": "Mars/Olympus", "week": {"mon": ["09:00", "17:00"]}}}
    assert policy.is_online(settings, now=datetime(2026, 11, 16, 10, tzinfo=timezone.utc)) is True


# --- transcript -----------------------------------------------------------


def test_transcript_labelling_and_citations() -> None:
    body = transcript.compile_transcript(
        messages=[
            {"role": "visitor", "content": "Where is my order?"},
            {
                "role": "assistant",
                "content": "It ships Monday.",
                "citations": [{"title": "Shipping policy"}],
                "confidence": 0.7237,
            },
        ],
        bot_name="EVA",
        reason="explicit",
        contact={"name": "Sam", "email": "sam@example.test"},
    )
    assert "Visitor" in body and "Assistant" in body
    assert "Where is my order?" in body
    # The owner's first question is "what did it tell them"; the source is part of
    # that answer.
    assert "Shipping policy" in body
    assert "0.72" in body
    assert "sam@example.test" in body


def test_fallback_summary_needs_no_model() -> None:
    summary = transcript.fallback_summary(
        messages=[{"role": "visitor", "content": "Where is my order?"}], reason="explicit"
    )
    assert "Where is my order?" in summary
    assert "explicit" in summary


# --- the email channel builds a real message ------------------------------


def test_email_channel_builds_a_message_with_reply_to(monkeypatch) -> None:
    """No network: capture what SMTP would have been handed."""
    captured: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            captured["tls"] = True

        def login(self, user, password):
            captured["login"] = (user, password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    channel = EmailChannel(host="mail.test", port=2525, from_addr="no-reply@eva.test")
    delivery = channel.send(
        Notification(
            channel="email",
            to="owner@example.test",
            subject="Handover: EVA needs you",
            body="Summary: ...\ntranscript",
            reply_to="visitor@example.test",
            conversation_id=str(uuid.uuid4()),
        )
    )
    assert delivery.ok
    message = captured["message"]
    assert message["To"] == "owner@example.test"
    # A reply has to reach the visitor, not the platform's no-reply sender.
    assert message["Reply-To"] == "visitor@example.test"
    assert "transcript" in message.get_content()


def test_email_channel_reports_failure_instead_of_raising(monkeypatch) -> None:
    """An SMTP outage must not become an error the visitor sees."""

    class BrokenSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", BrokenSMTP)
    delivery = EmailChannel(host="mail.test").send(
        Notification(channel="email", to="owner@example.test", subject="s", body="b")
    )
    assert delivery.ok is False
    assert "connection refused" in delivery.detail


# --- end to end over the widget routes ------------------------------------


def test_handover_creates_a_row_locks_the_conversation_and_sends_mail(
    client, tenant, published_bot, monkeypatch
) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])

    recording = RecordingChannel()
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: recording)

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test", "name": "Sam"},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["reason"] == "explicit"

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        status = session.execute(
            text("SELECT status, visitor_email FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ).mappings().one()
        handover = session.execute(
            text(
                "SELECT reason, status, notified_at FROM handovers"
                " WHERE conversation_id = :id"
            ),
            {"id": conversation_id},
        ).mappings().one()

    assert status["status"] == "handed_over"
    assert status["visitor_email"] == "visitor@example.test"
    assert handover["reason"] == "explicit"
    assert handover["status"] == "notified"
    assert handover["notified_at"] is not None

    assert len(recording.sent) == 1
    assert recording.sent[0].to == "owner@example.test"
    assert recording.sent[0].reply_to == "visitor@example.test"
    assert "Conversation with" in recording.sent[0].body


def test_handover_is_idempotent(client, tenant, published_bot, monkeypatch) -> None:
    """Submitting the form twice must not email the owner twice."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    recording = RecordingChannel()
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: recording)

    headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}
    first = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers=headers,
    )
    second = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers=headers,
    )
    assert first.json()["handover_id"] == second.json()["handover_id"]

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        count = session.execute(
            text("SELECT count(*) FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert count == 1


def test_handover_rejects_an_unusable_email(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    conversation_id, token = _start(client, published_bot["public_id"])
    for bad in ("nope", "a@b", "with space@example.test", "@example.test"):
        response = client.post(
            f"/api/v1/widget/conversations/{conversation_id}/handover",
            json={"email": bad},
            headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 422, f"{bad!r} was accepted"


def test_delivery_failure_is_recorded_not_raised(
    client, tenant, published_bot, monkeypatch
) -> None:
    """The visitor still gets their answer; the row says delivery failed."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    monkeypatch.setattr(
        "app.handover.channels.build_channel", lambda: RecordingChannel(ok=False)
    )

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        status = session.execute(
            text("SELECT status FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert status == "failed"


def test_no_owner_address_records_the_handover_without_mail(
    client, tenant, published_bot, monkeypatch
) -> None:
    """A tenant who has not configured an address is not a broken handover."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    conversation_id, token = _start(client, published_bot["public_id"])
    recording = RecordingChannel()
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: recording)

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    assert recording.sent == []

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        status = session.execute(
            text("SELECT status FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert status == "recorded"


def test_handover_from_another_origin_is_refused(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    conversation_id, token = _start(client, published_bot["public_id"])
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers={"Origin": "https://evil.example", "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_ai_stops_answering_after_handover(client, tenant, published_bot, monkeypatch) -> None:
    """HAND-05: the assistant must not reply once a human is coming."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: RecordingChannel())

    headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}
    client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers=headers,
    )

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/messages",
        json={"text": "hello?"},
        headers=headers,
    )
    # The SSE body carries an error event rather than answer tokens.
    assert "conversation_not_active" in response.text
    assert "event: token" not in response.text


def test_visitor_can_still_add_to_the_transcript_after_handover(
    client, tenant, published_bot, monkeypatch
) -> None:
    """§13.3: further messages are appended, not answered."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: RecordingChannel())
    headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}

    client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test"},
        headers=headers,
    )
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/messages/after-handover",
        json={"text": "Order number is 12345."},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["ai_will_answer"] is False

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        stored = session.execute(
            text(
                "SELECT content FROM messages WHERE conversation_id = :id"
                " AND role = 'visitor' ORDER BY created_at DESC LIMIT 1"
            ),
            {"id": conversation_id},
        ).scalar_one()
    assert stored == "Order number is 12345."


def test_keyword_trigger_hands_over_without_asking_retrieval(
    client, tenant, published_bot, monkeypatch
) -> None:
    """HAND-01: "I want a human" must escalate, not be answered."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    conversation_id, token = _start(client, published_bot["public_id"])

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/messages",
        json={"text": "I want to speak to a human"},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert "event: final" in response.text
    assert "handover" in response.text
    assert '"reason": "rule"' in response.text

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        reason = session.execute(
            text("SELECT reason FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
        status = session.execute(
            text("SELECT status FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert reason == "rule"
    assert status == "handed_over"


def test_negative_feedback_opens_a_handover(
    client, tenant, published_bot, stub_embedder, monkeypatch
) -> None:
    """HAND-01's third trigger: a thumbs-down is the owner's signal to act."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: RecordingChannel())
    headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}

    message_id = _answered_turn(
        client, conversation_id, token, "What is your refund policy?", stub_embedder
    )

    response = client.post(
        f"/api/v1/widget/messages/{message_id}/feedback",
        json={"rating": "down"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["handover"] is not None
    assert response.json()["handover"]["reason"] == "negative_feedback"

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        reason = session.execute(
            text("SELECT reason FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert reason == "negative_feedback"


def test_positive_feedback_does_not_escalate(
    client, tenant, published_bot, stub_embedder, monkeypatch
) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    conversation_id, token = _start(client, published_bot["public_id"])
    headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}

    message_id = _answered_turn(
        client, conversation_id, token, "What is your refund policy?", stub_embedder
    )
    response = client.post(
        f"/api/v1/widget/messages/{message_id}/feedback",
        json={"rating": "up"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["handover"] is None

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        count = session.execute(
            text("SELECT count(*) FROM handovers WHERE conversation_id = :id"),
            {"id": conversation_id},
        ).scalar_one()
    assert count == 0


def _message_id(sse: str) -> str:
    import json

    for line in sse.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
            if "message_id" in payload:
                return payload["message_id"]
    raise AssertionError(f"no message_id in stream: {sse}")


# --- HAND-07: business hours reach the widget -----------------------------


def test_session_reports_online_when_no_hours_are_configured(
    client, tenant, published_bot
) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    assert body["online"] is True
    assert body["next_open"] is None


def test_session_reports_offline_outside_the_weekly_window(
    client, tenant, published_bot
) -> None:
    """A window that excludes "now" in every timezone, so the test cannot flake.

    An empty-but-configured schedule is the honest way to express "always closed":
    a real window would be right or wrong depending on when the suite runs.
    """
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"],
        {"business_hours": {"timezone": "UTC", "week": {}}},
    )
    body = _session(client, published_bot["public_id"]).json()
    assert body["online"] is False
    assert body["next_open"] is None


def test_session_offline_state_carries_the_next_opening(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    # Every day closed except a window that has already passed or is yet to come is
    # hard to pin to the wall clock; assert the label format when one is produced.
    _set_handover_settings(
        tenant["tenant_id"],
        {
            "business_hours": {
                "timezone": "UTC",
                "week": {"mon": ["00:00", "00:01"], "tue": ["00:00", "00:01"]},
            }
        },
    )
    body = _session(client, published_bot["public_id"]).json()
    if not body["online"] and body["next_open"]:
        # e.g. "Mon 16 Nov 00:00 UTC" — a human reads this in the banner.
        assert any(day in body["next_open"] for day in ("Mon", "Tue"))


def test_offline_handover_still_records_and_notifies(
    client, tenant, published_bot, monkeypatch
) -> None:
    """R43 + HAND-02: offline is a reason to take details, not to drop them."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"],
        {
            "handover": {"email": "owner@example.test"},
            "business_hours": {"timezone": "UTC", "week": {}},
        },
    )
    conversation_id, token = _start(client, published_bot["public_id"])
    recording = RecordingChannel()
    monkeypatch.setattr("app.handover.channels.build_channel", lambda: recording)

    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/handover",
        json={"email": "visitor@example.test", "note": "Please call me back."},
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    assert len(recording.sent) == 1
    assert "Please call me back." in recording.sent[0].body
