"""OPS-15: the knowledge and conversation command layers wrote no audit row at all.

The measured defect, from OPS-13's recorded walk: ``audit.mutating_modules`` read
``conversation/router.py: false`` and ``knowledge/router.py: false`` while every other
mutating module read ``true``. Publishing or rolling back a document, previewing a
draft and starting a conversation therefore left no trail — so an owner correcting or
retracting published content, or reading unpublished content, was invisible to R29.

Three kinds of test here, and the split is deliberate:

* **the row exists** — the action is observed in ``audit_logs`` for the tenant, read
  back over a *second* connection. ``tenant_session`` closes without committing, so a
  route that records and forgets ``session.commit()`` answers 200 and persists
  nothing; asserting on the response body cannot see that.
* **the vocabulary is closed** — every emitted action is a member of ``Action``. A
  string literal at a call site passes at runtime and quietly makes the enum a
  suggestion, which is how a "closed vocabulary" stops being closed.
* **the row carries no content** — ``audit_logs`` is append-only and never purged, and
  a conversation's visitor identifiers are erased on request (SEC-10). A receipt that
  kept the visitor's session id or page URL would outlive the erasure it describes,
  exactly as SEC-10's own audit row was designed to avoid.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import importlib
import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import _admin_dsn


@pytest.fixture(scope="module")
def client(migrated_db: str) -> TestClient:
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    os.environ["APP_DATABASE_URL"] = (
        f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{migrated_db}"
    )
    from app import config

    importlib.reload(config)
    from app.db import session as db_session

    importlib.reload(db_session)
    from app.security import passwords

    importlib.reload(passwords)
    from app.auth import dependencies, service

    importlib.reload(service)
    importlib.reload(dependencies)
    from app.retrieval import embedding

    importlib.reload(embedding)
    from app.db import vector_store

    importlib.reload(vector_store)
    from app.knowledge import service as knowledge_service

    importlib.reload(knowledge_service)
    from app.knowledge import router as knowledge_router

    importlib.reload(knowledge_router)
    from app.conversation import service as conversation_service

    importlib.reload(conversation_service)
    from app.conversation import router as conversation_router

    importlib.reload(conversation_router)
    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client

    with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
        conn.execute("ALTER TABLE audit_logs DISABLE TRIGGER USER")
        conn.execute(
            "DELETE FROM audit_logs WHERE tenant_id IN"
            " (SELECT tenant_id FROM users WHERE email LIKE %s)",
            ("ops15-%@example.test",),
        )
        conn.execute("ALTER TABLE audit_logs ENABLE TRIGGER USER")
        conn.execute(
            "DELETE FROM tenants WHERE id IN"
            " (SELECT tenant_id FROM users WHERE email LIKE %s)",
            ("ops15-%@example.test",),
        )


@pytest.fixture(autouse=True)
def stub_embedding_and_queue(monkeypatch):
    from app.knowledge import queue as queue_module
    from app.retrieval.embedding import HashingEmbedder

    embedder = HashingEmbedder(dimension=1024)
    monkeypatch.setattr("app.knowledge.router._embedder", lambda: embedder, raising=False)
    monkeypatch.setattr(queue_module, "enqueue_ingestion", lambda *a, **k: None)
    monkeypatch.setattr(queue_module, "enqueue_crawl", lambda *a, **k: None)


@pytest.fixture
def owner(client: TestClient, migrated_db: str) -> dict:
    email = f"ops15-{uuid.uuid4().hex[:10]}@example.test"
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "A"},
    )
    assert registered.status_code == 201, registered.text
    body = registered.json()
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    token = [
        header.split("eva_session=", 1)[1].split(";", 1)[0]
        for header in login.headers.get_list("set-cookie")
        if header.startswith("eva_session=")
    ][0]
    client.cookies.set("eva_session", token)
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        bot_id = str(
            conn.execute(
                "SELECT id FROM bots WHERE tenant_id = %s", (body["tenant_id"],)
            ).fetchone()[0]
        )
    return {**body, "bot_id": bot_id, "client": client, "db": migrated_db}


def _rows(db: str, tenant_id: str, action: str) -> list[tuple]:
    """Read the trail over its own connection: an uncommitted record is not a row."""
    with psycopg.connect(_admin_dsn(db)) as conn:
        return conn.execute(
            "SELECT entity_id, actor_user_id, detail FROM audit_logs"
            " WHERE tenant_id = %s AND action = %s",
            (tenant_id, action),
        ).fetchall()


def _faq(owner: dict, question: str = "Returns?", answer: str = "Thirty days.") -> dict:
    response = owner["client"].post(
        "/api/v1/knowledge/faqs", json={"question": question, "answer": answer}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- knowledge: publish ----------------------------------------------------


def test_publish_is_audited_with_the_set_it_now_serves(owner) -> None:
    version = _faq(owner)
    response = owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": version["version_id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    rows = _rows(owner["db"], owner["tenant_id"], "knowledge.published")
    assert len(rows) == 1, "a publish with no audit row is not auditable (OPS-15)"
    entity_id, actor_user_id, detail = rows[0]
    assert str(entity_id) == body["active_knowledge_set_id"]
    assert str(actor_user_id) == owner["user_id"]
    assert detail["version_id"] == version["version_id"]
    # The set, not just the named version: the HALT-07 ruling makes publish additive,
    # and a receipt that named one version would describe a different operation.
    assert detail["version_ids"] == body["active_knowledge_version_ids"]
    assert detail["previous_set_id"] is None, "the first publish replaced nothing"


def test_a_second_publish_records_the_set_it_replaced(owner) -> None:
    first = _faq(owner, question="First?", answer="One.")
    _faq(owner, question="Second?", answer="Two.")
    first_publish = owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": first["version_id"]},
    ).json()
    owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": first["version_id"]},
    )

    rows = _rows(owner["db"], owner["tenant_id"], "knowledge.published")
    assert len(rows) == 2
    # A publish whose previous_set_id was always null would look identical to the
    # first one, and ADMN-17's rollback walks that edge.
    assert rows[-1][2]["previous_set_id"] == first_publish["active_knowledge_set_id"]


def test_a_refused_publish_writes_no_row(owner) -> None:
    """A 409 must not claim a publish happened — the error path is the audit's edge."""
    response = owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert _rows(owner["db"], owner["tenant_id"], "knowledge.published") == []


# --- knowledge: rollback ---------------------------------------------------


def test_rollback_is_audited_with_the_set_it_restored(owner) -> None:
    version = _faq(owner)
    owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": version["version_id"]},
    )
    owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": version["version_id"]},
    )
    restored = owner["client"].post(
        "/api/v1/knowledge/rollback", json={"bot_id": owner["bot_id"]}
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()

    rows = _rows(owner["db"], owner["tenant_id"], "knowledge.rolled_back")
    assert len(rows) == 1, "a rollback writes no set row, so this row is the only trace"
    entity_id, actor_user_id, detail = rows[0]
    assert str(entity_id) == body["active_knowledge_set_id"]
    assert str(actor_user_id) == owner["user_id"]
    assert detail["version_ids"] == body["active_knowledge_version_ids"]


def test_a_refused_rollback_writes_no_row(owner) -> None:
    """Nothing published: a 409, and no receipt for a restore that did not happen."""
    response = owner["client"].post(
        "/api/v1/knowledge/rollback", json={"bot_id": owner["bot_id"]}
    )
    assert response.status_code == 409
    assert _rows(owner["db"], owner["tenant_id"], "knowledge.rolled_back") == []


# --- knowledge: preview ----------------------------------------------------


def test_preview_is_audited_with_counts_not_text(owner) -> None:
    """Preview reads the *draft*, so it is an access event; the question is not kept."""
    _faq(owner, question="How long do refunds take?",
         answer="Refunds take five business days.")
    question = "How long do refunds take?"
    response = owner["client"].post(
        "/api/v1/knowledge/preview", json={"question": question}
    )
    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert hits

    rows = _rows(owner["db"], owner["tenant_id"], "knowledge.previewed")
    assert len(rows) == 1
    _, actor_user_id, detail = rows[0]
    assert str(actor_user_id) == owner["user_id"]
    assert detail["hits"] == len(hits)
    assert detail["declined"] is False
    # The owner's question and the document text are content in an append-only table.
    assert question not in json.dumps(detail)
    assert "five business days" not in json.dumps(detail)


def test_a_preview_with_nothing_to_search_is_still_audited(owner) -> None:
    """An access trail that keeps only the successes has a hole where someone looked."""
    response = owner["client"].post(
        "/api/v1/knowledge/preview", json={"question": "anything at all?"}
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "no_versions"

    rows = _rows(owner["db"], owner["tenant_id"], "knowledge.previewed")
    assert len(rows) == 1, "the empty-draft path returns before the record without this"
    assert rows[0][2]["versions"] == 0 and rows[0][2]["declined"] is True


# --- conversation: start ---------------------------------------------------


def test_starting_a_conversation_is_audited_without_the_visitor_identifiers(owner) -> None:
    """SEC-10 erases `visitor_session_id` and `page_url`; a receipt must not keep them."""
    session_id = f"visitor-{uuid.uuid4().hex[:8]}"
    page_url = "https://shop.example.test/checkout"
    response = owner["client"].post(
        "/api/v1/conversations",
        json={
            "bot_id": owner["bot_id"],
            "visitor_session_id": session_id,
            "page_url": page_url,
        },
    )
    assert response.status_code == 201, response.text
    conversation_id = response.json()["conversation_id"]

    rows = _rows(owner["db"], owner["tenant_id"], "conversation.started")
    assert len(rows) == 1, "a started conversation with no audit row is not auditable"
    entity_id, actor_user_id, detail = rows[0]
    assert str(entity_id) == conversation_id
    assert str(actor_user_id) == owner["user_id"]
    assert detail["bot_id"] == owner["bot_id"]
    assert detail["has_page_url"] is True
    # The values, not just the keys: a detail that carried them under another name
    # would still be the erasure undone by its own receipt.
    serialised = json.dumps(detail)
    assert session_id not in serialised, "the receipt retained the visitor session id"
    assert page_url not in serialised, "the receipt retained the page URL"


def test_a_conversation_for_an_unknown_bot_writes_no_row(owner) -> None:
    response = owner["client"].post(
        "/api/v1/conversations",
        json={"bot_id": str(uuid.uuid4()), "visitor_session_id": "v-1"},
    )
    assert response.status_code == 404
    assert _rows(owner["db"], owner["tenant_id"], "conversation.started") == []


# --- the vocabulary is closed ----------------------------------------------


def test_every_emitted_action_is_a_member_of_the_closed_vocabulary(owner) -> None:
    """A string literal at a call site makes the closed vocabulary a suggestion.

    Read from the tenant's own trail after driving one of each operation, so the claim
    is about what the code *wrote* rather than about what the module imports.

    ``Action`` is a plain class of string constants rather than an ``Enum``
    (``audit/service.py``), so the vocabulary is collected from its class body —
    uppercase, string-valued attributes — and the collection is asserted non-empty,
    because "no members" would satisfy the subset check by making it vacuous.
    """
    version = _faq(owner)
    owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": version["version_id"]},
    )
    owner["client"].post(
        "/api/v1/knowledge/publish",
        json={"bot_id": owner["bot_id"], "version_id": version["version_id"]},
    )
    owner["client"].post("/api/v1/knowledge/rollback", json={"bot_id": owner["bot_id"]})
    owner["client"].post("/api/v1/knowledge/preview", json={"question": "Returns?"})
    owner["client"].post(
        "/api/v1/conversations",
        json={"bot_id": owner["bot_id"], "visitor_session_id": "v-1"},
    )

    with psycopg.connect(_admin_dsn(owner["db"])) as conn:
        written = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT action FROM audit_logs WHERE tenant_id = %s",
                (owner["tenant_id"],),
            )
        }
    known = _vocabulary()
    assert len(known) > 20, "the vocabulary was read wrongly, so the subset proves nothing"
    assert written, "the walk wrote nothing, so this assertion would be vacuous"
    assert written <= known, f"not in the closed vocabulary: {sorted(written - known)}"


def test_the_four_ops15_names_exist_in_the_vocabulary():
    """Named individually: `written <= known` also passes if the routes record nothing."""
    known = _vocabulary()
    for name in (
        "knowledge.published",
        "knowledge.rolled_back",
        "knowledge.previewed",
        "conversation.started",
    ):
        assert name in known, name


def _vocabulary() -> set[str]:
    from app.audit.service import Action

    return {
        value
        for key, value in vars(Action).items()
        if key.isupper() and isinstance(value, str)
    }
