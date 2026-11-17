"""WP-08: knowledge HTTP surface — upload, FAQ, list, publish, preview.

Real FastAPI app, real database, real HTTP. Two things are substituted at their
seams and nothing else:

* the embedder, because the real one is a 3.2 GiB model reached over the network
  (covered by ``test_bge_m3_provider.py`` and the embedding-service tests);
* the arq enqueue call, because the job itself is already tested end to end in
  ``test_ingest_job.py`` — running it inline here would re-test that file.

The claims that matter are the boundaries: unsupported formats rejected, tenant
isolation enforced, a draft previewable before publish, and an unready version
refused by publish.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import importlib

import psycopg
import pytest
from fastapi.testclient import TestClient


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
    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client

    # Registration commits, so these tenants outlive the client. audit_logs is
    # append-only and blocks even the owner (R29), so the guard trigger has to be
    # disabled for this test-only teardown; production never deletes audit rows.
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute("ALTER TABLE audit_logs DISABLE TRIGGER USER")
        conn.execute(
            "DELETE FROM audit_logs WHERE tenant_id IN"
            " (SELECT tenant_id FROM users WHERE email LIKE ANY(%s))",
            (["know-%@example.test", "other-%@example.test"],),
        )
        conn.execute("ALTER TABLE audit_logs ENABLE TRIGGER USER")
        conn.execute(
            "DELETE FROM tenants WHERE id IN"
            " (SELECT tenant_id FROM users WHERE email LIKE ANY(%s))",
            (["know-%@example.test", "other-%@example.test"],),
        )


@pytest.fixture
def session(client: TestClient) -> dict:
    """Register a fresh tenant and return its cookie + ids."""
    email = f"know-{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "K"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    token = None
    for header in login.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            token = header.split("eva_session=", 1)[1].split(";", 1)[0]
    assert token
    client.cookies.set("eva_session", token)
    return body


@pytest.fixture(autouse=True)
def stub_embedding_and_queue(monkeypatch):
    """Swap the model and the queue at their seams; leave everything else real."""
    from app.knowledge import queue as queue_module
    from app.retrieval.embedding import HashingEmbedder

    embedder = HashingEmbedder(dimension=1024)
    monkeypatch.setattr("app.knowledge.router._embedder", lambda: embedder, raising=False)
    monkeypatch.setattr(queue_module, "enqueue_ingestion", lambda *a, **k: None)
    monkeypatch.setattr(queue_module, "enqueue_crawl", lambda *a, **k: None)


def _bot_id(migrated_db: str, tenant_id: str) -> str:
    """The bot registration creates for the tenant."""
    with psycopg.connect(_admin(migrated_db)) as conn:
        return str(
            conn.execute(
                "SELECT id FROM bots WHERE tenant_id = %s", (tenant_id,)
            ).fetchone()[0]
        )


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def test_upload_creates_document_and_version(client, session, migrated_db) -> None:
    response = client.post(
        "/api/v1/knowledge/uploads?filename=policy.md",
        content=b"# Refunds\n\nRefunds take five business days.",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "processing"
    assert body["chunks"] >= 1

    docs = client.get("/api/v1/knowledge/documents").json()
    assert len(docs) == 1
    # The document's own heading is a better admin label than the filename, and
    # the filename is still kept so the owner can tell uploads apart.
    assert docs[0]["title"] == "Refunds"
    assert docs[0]["uri"] == "policy.md"


def test_unsupported_upload_is_rejected_with_supported_list(client, session) -> None:
    response = client.post(
        "/api/v1/knowledge/uploads?filename=deck.pptx", content=b"not really a pptx"
    )
    assert response.status_code == 415
    detail = response.json()["detail"]["error"]
    assert detail["code"] == "unsupported_format"
    assert ".pdf" in detail["message"], "the error must tell the owner what is accepted"


def test_binary_named_as_text_is_rejected(client, session) -> None:
    """SEC-07: the suffix is attacker-controlled, so the bytes are checked too.

    Measured before the fix: an ELF binary named ``report.txt`` returned **201** and
    became a document with one chunk, so the bytes were ingested as text. The suffix
    alone decides which parser runs, which is why the check precedes the parser.
    """
    response = client.post(
        "/api/v1/knowledge/uploads?filename=report.txt",
        content=b"\x7fELF\x02\x01\x01\x00FAKE",
    )
    assert response.status_code == 415, response.text
    detail = response.json()["detail"]["error"]
    assert detail["code"] == "content_mismatch"
    assert "binary" in detail["message"]


def test_html_named_as_pdf_is_rejected(client, session) -> None:
    """A PDF suffix must not be enough to dispatch the PDF parser."""
    response = client.post(
        "/api/v1/knowledge/uploads?filename=policy.pdf",
        content=b"<html><body>not a pdf</body></html>",
    )
    assert response.status_code == 415, response.text
    assert response.json()["detail"]["error"]["code"] == "content_mismatch"


def test_corrupt_pdf_is_unreadable_not_a_500(client, session) -> None:
    """A truncated PDF is the client's error, so it must not surface as a 500.

    Measured before the fix: pypdf raised ``PdfStreamError``, which is not a
    ``ValueError``, so the router's ``except ValueError`` missed it and the owner got
    "500 Internal Server Error" with no indication the file was the problem.
    """
    response = client.post(
        "/api/v1/knowledge/uploads?filename=broken.pdf",
        content=b"%PDF-1.7\n\x00\x01 truncated",
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]["error"]
    assert detail["code"] == "unreadable_document"
    assert "PDF" in detail["message"]


def test_a_real_text_file_is_still_accepted(client, session, migrated_db) -> None:
    """The guard must not reject ordinary uploads: a plain sentence has no NUL byte."""
    response = client.post(
        "/api/v1/knowledge/uploads?filename=shipping.md",
        content=b"Shipping is free over 50 EUR.",
    )
    assert response.status_code == 201, response.text


def test_empty_upload_is_rejected(client, session) -> None:
    response = client.post("/api/v1/knowledge/uploads?filename=a.md", content=b"")
    assert response.status_code == 400


def test_upload_requires_a_filename(client, session) -> None:
    response = client.post("/api/v1/knowledge/uploads", content=b"hello")
    assert response.status_code == 422


def test_faq_is_created_ready(client, session) -> None:
    response = client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Do you ship abroad?", "answer": "To twelve countries."},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["chunks"] == 1


def test_publish_requires_ready_version(client, session, migrated_db) -> None:
    upload = client.post(
        "/api/v1/knowledge/uploads?filename=policy.md",
        content=b"# Shipping\n\nExpress is next day.",
    ).json()
    bot_id = _bot_id(migrated_db, session["tenant_id"])

    response = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": bot_id, "version_id": upload["version_id"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "version_not_ready"


def test_publish_succeeds_for_a_ready_version(client, session, migrated_db) -> None:
    faq = client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Returns?", "answer": "Thirty days."},
    ).json()
    bot_id = _bot_id(migrated_db, session["tenant_id"])

    response = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": bot_id, "version_id": faq["version_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_knowledge_version_id"] == faq["version_id"]


def test_preview_reads_unpublished_draft(client, session) -> None:
    """The whole point of preview: see a change before visitors can."""
    client.post(
        "/api/v1/knowledge/faqs",
        json={
            "question": "How long do refunds take?",
            "answer": "Refunds are processed within five business days.",
        },
    )
    response = client.post(
        "/api/v1/knowledge/preview",
        json={"question": "How long do refunds take?"},
    )
    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert hits, "preview must find the draft chunk"
    assert "five business days" in hits[0]["text"]


def test_preview_never_crosses_tenants(client, session) -> None:
    """A second tenant's preview must not see the first tenant's chunks."""
    client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Secret policy?", "answer": "Only for tenant one."},
    )

    email = f"other-{uuid.uuid4().hex[:10]}@example.test"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "B"},
    )
    client.cookies.clear()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    token = [
        h.split("eva_session=", 1)[1].split(";", 1)[0]
        for h in login.headers.get_list("set-cookie")
        if h.startswith("eva_session=")
    ][0]
    client.cookies.set("eva_session", token)

    response = client.post(
        "/api/v1/knowledge/preview", json={"question": "Secret policy?"}
    )
    assert response.json()["hits"] == [], "tenant two must see nothing of tenant one"


def test_publish_rejects_unknown_version(client, session, migrated_db) -> None:
    bot_id = _bot_id(migrated_db, session["tenant_id"])
    response = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": bot_id, "version_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


def test_publish_rejects_malformed_uuid(client, session) -> None:
    response = client.post(
        "/api/v1/knowledge/publish", json={"bot_id": "nope", "version_id": "nope"}
    )
    assert response.status_code == 422


def test_knowledge_routes_require_authentication(client) -> None:
    client.cookies.clear()
    for method, path in (
        ("get", "/api/v1/knowledge/documents"),
        ("post", "/api/v1/knowledge/faqs"),
        ("post", "/api/v1/knowledge/preview"),
        ("post", "/api/v1/knowledge/publish"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{path} should require auth"


def test_crawl_schedule_is_validated(client, session) -> None:
    response = client.post(
        "/api/v1/knowledge/crawls",
        json={"url": "http://example.test", "schedule": "hourly"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "invalid_schedule"


def test_registration_creates_exactly_one_bot(client, session, migrated_db) -> None:
    """Publish needs a bot id; nothing else in the surface can create one."""
    with psycopg.connect(_admin(migrated_db)) as conn:
        count = conn.execute(
            "SELECT count(*) FROM bots WHERE tenant_id = %s", (session["tenant_id"],)
        ).fetchone()[0]
    assert count == 1


def test_publish_then_rollback_moves_the_pointer(client, session, migrated_db) -> None:
    """The live bot must serve v1 again after a rollback, not v2."""
    first = client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Returns?", "answer": "Thirty days."},
    ).json()
    second = client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Shipping?", "answer": "Five days."},
    ).json()
    bot_id = _bot_id(migrated_db, session["tenant_id"])

    for version in (first, second):
        assert client.post(
            "/api/v1/knowledge/publish",
            json={"bot_id": bot_id, "version_id": version["version_id"]},
        ).status_code == 200

    response = client.post(
        "/api/v1/knowledge/rollback",
        json={"bot_id": bot_id, "version_id": first["version_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_knowledge_version_id"] == first["version_id"]


def test_publish_cannot_target_another_tenants_bot(client, session, migrated_db) -> None:
    """RLS hides the bot, but a silent no-op UPDATE would look like success."""
    faq = client.post(
        "/api/v1/knowledge/faqs",
        json={"question": "Refunds?", "answer": "Five days."},
    ).json()

    email = f"other-{uuid.uuid4().hex[:10]}@example.test"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "B"},
    )
    with psycopg.connect(_admin(migrated_db)) as conn:
        foreign_bot = conn.execute(
            "SELECT id FROM bots WHERE tenant_id ="
            " (SELECT id FROM tenants WHERE name = 'B' ORDER BY created_at DESC LIMIT 1)"
        ).fetchone()
    assert foreign_bot, "registration should have created a bot for tenant B"

    response = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": str(foreign_bot[0]), "version_id": faq["version_id"]},
    )
    assert response.status_code == 404
