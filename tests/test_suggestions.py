"""WP-12: proactive chips, dynamic follow-ups, CSAT (CONV-06, CONV-07, CONV-08).

The suggestion sanitisation tests are pure functions and need no stack. The turn
loop and HTTP tests reuse the WP-11 fixtures from ``test_conversation.py`` rather
than rebuilding them: same database, same published bot, same fake provider.

What is being defended, in the order the failures would hurt:

* a generated chip that repeats the question, runs to a paragraph, or carries a
  list marker is a broken widget, so ``sanitise`` is tested against each;
* a refusal must not get *generated* follow-ups — inventing questions from an
  answer that was never given is the same hallucination the floor refuses;
* CSAT must reject a visitor message, another tenant's message, and a bad rating,
  and a second vote must replace the first rather than skew the average.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest

from app.conversation import suggestions
from app.conversation.llm import Completion

from test_conversation import (  # noqa: F401 - fixtures are used via pytest
    FakeProvider,
    _bot_id,
    _events,
    _run_turn,
    _start,
    client,
    published_bot,
    stub_embedder,
    tenant,
)


class SuggestionProvider(FakeProvider):
    """Answers in prose, suggests in JSON — the two calls have different jobs."""

    def __init__(self, reply: str, chips: object) -> None:
        super().__init__(reply=reply)
        self.chips = chips
        self.suggestion_requests: list[list] = []

    def complete(self, messages, *, model, max_tokens=1024) -> Completion:
        self.suggestion_requests.append(list(messages))
        if self.chips is None:
            return Completion(error="suggestions_unavailable", model=model)
        import json

        return Completion(text=json.dumps(self.chips), model=model)


# --- sanitisation (pure) ---------------------------------------------------


def test_sanitise_strips_markers_and_trims_to_three() -> None:
    raw = [
        "- How long does standard shipping take?",
        "1. Can I track my order",
        "\u2022 What is the returns window",
        "Do you ship internationally?",
    ]
    out = suggestions.sanitise(raw, question="How long does shipping take?")
    assert out == [
        "How long does standard shipping take?",
        "Can I track my order?",
        "What is the returns window?",
    ]


def test_sanitise_adds_a_question_mark_and_drops_duplicates() -> None:
    out = suggestions.sanitise(
        ["Can I track my order", "Can I track my order?", "can i track my order"],
        question="Where is my parcel?",
    )
    assert out == []


def test_sanitise_never_repeats_the_question_just_asked() -> None:
    out = suggestions.sanitise(
        ["How long is shipping?", "Can I track my order?", "What about returns?"],
        question="How long is shipping?",
    )
    assert out == ["Can I track my order?", "What about returns?"]


def test_sanitise_rejects_a_single_fragment_rather_than_padding() -> None:
    """One chip looks broken; a fabricated second one is worse than none."""
    assert suggestions.sanitise(["Can I track my order?"], question="q") == []


def test_sanitise_collapses_multiline_and_truncates() -> None:
    long_chip = "What " + "very " * 60 + "long question is this"
    out = suggestions.sanitise(
        ["line one\nline two", long_chip, "Is delivery free?", "Next day?"],
        question="q",
    )
    assert all("\n" not in chip for chip in out)
    assert all(len(chip) <= suggestions.MAX_SUGGESTION_CHARS + 1 for chip in out)


def test_parse_accepts_json_fences_and_bullets() -> None:
    assert suggestions.parse('```json\n["A?", "B?"]\n```') == ["A?", "B?"]
    assert suggestions.parse("- A?\n- B?") == ["- A?", "- B?"]
    assert suggestions.parse("") == []


def test_settings_normalises_a_malformed_jsonb_row() -> None:
    assert suggestions.settings(None) == {"proactive": [], "dynamic": True}
    assert suggestions.settings({"proactive": "not a list"})["proactive"] == []
    assert suggestions.settings({"dynamic": False})["dynamic"] is False


# --- dynamic follow-ups in the turn loop -----------------------------------


def test_answer_carries_generated_follow_ups(client, tenant, published_bot) -> None:
    conversation_id = _start(client, published_bot["bot_id"])
    provider = SuggestionProvider(
        reply="Standard shipping takes 3-5 business days.",
        chips=["Can I track my order?", "Do you ship internationally?"],
    )
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    final = events[-1][1]
    assert final["suggestions"] == [
        "Can I track my order?",
        "Do you ship internationally?",
    ]
    # The suggestion prompt must not be the answer prompt: it is a second,
    # separately-shaped request with the answer quoted as untrusted data.
    assert provider.suggestion_requests
    assert "untrusted data" in provider.suggestion_requests[0][1].content


def test_suggestion_failure_falls_back_to_curated_chips(
    client, tenant, published_bot, migrated_db
) -> None:
    """A failed suggestion call must not cost the visitor the chip row."""
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text(
                "UPDATE bots SET suggestion_settings = CAST(:s AS jsonb) WHERE id = :b"
            ),
            {
                "s": '{"proactive": ["Where is my order?", "How do returns work?"]}',
                "b": published_bot["bot_id"],
            },
        )
        session.commit()

    conversation_id = _start(client, published_bot["bot_id"])
    provider = SuggestionProvider(
        reply="Standard shipping takes 3-5 business days.", chips=None
    )
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    assert events[-1][1]["suggestions"] == [
        "Where is my order?",
        "How do returns work?",
    ]


def test_refusal_does_not_generate_follow_ups(client, tenant, migrated_db) -> None:
    """Inventing questions from an answer that was never given is the failure mode."""
    bot_id = _bot_id(migrated_db, tenant["tenant_id"])
    conversation_id = _start(client, bot_id)
    provider = SuggestionProvider(
        reply="unused", chips=["Can I track my order?", "Do you ship internationally?"]
    )
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="What is the warranty on a 1998 model?",
        provider=provider,
    )
    final = events[-1][1]
    assert final["declined"] is True
    assert final["suggestions"] == []
    assert provider.suggestion_requests == [], (
        "a refusal must not ask the model to invent follow-ups"
    )


def test_suggestions_are_persisted_on_the_message(
    client, tenant, published_bot
) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    conversation_id = _start(client, published_bot["bot_id"])
    provider = SuggestionProvider(
        reply="Standard shipping takes 3-5 business days.",
        chips=["Can I track my order?", "Do you ship internationally?"],
    )
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    message_id = events[-1][1]["message_id"]
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        row = session.execute(
            text("SELECT suggestions FROM messages WHERE id = :id"),
            {"id": message_id},
        ).one()
    assert row.suggestions == [
        "Can I track my order?",
        "Do you ship internationally?",
    ]


# --- CONV-06: proactive chips via the config endpoint ----------------------


def test_bot_config_exposes_curated_proactive_chips(
    client, tenant, published_bot
) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text(
                "UPDATE bots SET suggestion_settings = CAST(:s AS jsonb),"
                " welcome_message = 'Hi there' WHERE id = :b"
            ),
            {
                "s": '{"proactive": ["Where is my order?", "How do returns work?",'
                     ' "Do you ship abroad?"]}',
                "b": published_bot["bot_id"],
            },
        )
        session.commit()

    response = client.get(f"/api/v1/conversations/bots/{published_bot['bot_id']}/config")
    assert response.status_code == 200
    body = response.json()
    assert body["welcome"] == "Hi there"
    assert body["suggestions"] == [
        "Where is my order?",
        "How do returns work?",
        "Do you ship abroad?",
    ]


def test_bot_config_is_tenant_scoped(client, tenant, published_bot) -> None:
    other = f"conv-{uuid.uuid4().hex[:10]}@example.test"
    client.cookies.clear()
    client.post(
        "/api/v1/auth/register",
        json={"email": other, "password": "correct-horse-battery", "tenant_name": "O"},
    )
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": other, "password": "correct-horse-battery"},
    )
    token = next(
        h.split("eva_session=", 1)[1].split(";", 1)[0]
        for h in login.headers.get_list("set-cookie")
        if h.startswith("eva_session=")
    )
    client.cookies.set("eva_session", token)

    response = client.get(f"/api/v1/conversations/bots/{published_bot['bot_id']}/config")
    assert response.status_code == 404


# --- CONV-08: CSAT --------------------------------------------------------


def _answer(client, tenant, published_bot) -> str:
    conversation_id = _start(client, published_bot["bot_id"])
    provider = SuggestionProvider(reply="Standard shipping takes 3-5 business days.", chips=[])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    return events[-1][1]["message_id"]


def test_feedback_records_a_rating(client, tenant, published_bot) -> None:
    message_id = _answer(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/conversations/messages/{message_id}/feedback",
        json={"rating": "up"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["rating"] == "up"
    assert response.json()["message_id"] == message_id


def test_feedback_replaces_a_previous_vote(client, tenant, published_bot) -> None:
    """Two rows per message would make every CSAT average wrong."""
    from sqlalchemy import text

    from app.db.session import tenant_session

    message_id = _answer(client, tenant, published_bot)
    client.post(
        f"/api/v1/conversations/messages/{message_id}/feedback",
        json={"rating": "down"},
    )
    client.post(
        f"/api/v1/conversations/messages/{message_id}/feedback",
        json={"rating": "up"},
    )
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        rows = session.execute(
            text("SELECT rating FROM feedback WHERE message_id = :id"),
            {"id": message_id},
        ).all()
    assert [r.rating for r in rows] == ["up"]


def test_feedback_rejects_a_visitor_message(client, tenant, published_bot) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    conversation_id = _start(client, published_bot["bot_id"])
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text(
                "INSERT INTO messages (tenant_id, conversation_id, role, content)"
                " VALUES (:t, :c, 'visitor', 'hello')"
            ),
            {"t": tenant["tenant_id"], "c": conversation_id},
        )
        session.commit()
        visitor_id = session.execute(
            text(
                "SELECT id FROM messages WHERE conversation_id = :c"
                " AND role = 'visitor'"
            ),
            {"c": conversation_id},
        ).scalar_one()

    response = client.post(
        f"/api/v1/conversations/messages/{visitor_id}/feedback",
        json={"rating": "up"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "not_an_answer"


def test_feedback_rejects_a_bad_rating(client, tenant, published_bot) -> None:
    message_id = _answer(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/conversations/messages/{message_id}/feedback",
        json={"rating": "maybe"},
    )
    assert response.status_code == 422


def test_feedback_cannot_rate_another_tenants_answer(
    client, tenant, published_bot
) -> None:
    message_id = _answer(client, tenant, published_bot)

    other = f"conv-{uuid.uuid4().hex[:10]}@example.test"
    client.cookies.clear()
    client.post(
        "/api/v1/auth/register",
        json={"email": other, "password": "correct-horse-battery", "tenant_name": "O"},
    )
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": other, "password": "correct-horse-battery"},
    )
    token = next(
        h.split("eva_session=", 1)[1].split(";", 1)[0]
        for h in login.headers.get_list("set-cookie")
        if h.startswith("eva_session=")
    )
    client.cookies.set("eva_session", token)

    response = client.post(
        f"/api/v1/conversations/messages/{message_id}/feedback",
        json={"rating": "down"},
    )
    assert response.status_code == 404


def test_feedback_rejects_an_unknown_message(client, tenant) -> None:
    response = client.post(
        f"/api/v1/conversations/messages/{uuid.uuid4()}/feedback",
        json={"rating": "up"},
    )
    assert response.status_code == 404
