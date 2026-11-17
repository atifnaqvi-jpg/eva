"""SEC-07: upload fuzzing over a fixed corpus (blueprint §27, §19.2).

The property under test is not "the right status code" — it is that **no hostile file
produces a 500**. A 4xx is the product working: the owner is told what was wrong and
what is accepted. A 5xx is the product failing, even when the file was garbage, because
it means an exception escaped the parser and the owner learned nothing.

Measured before this file existed: an ELF named ``report.txt`` was accepted (201), and
a truncated PDF returned 500 because ``pypdf`` raises ``PdfStreamError``, which is not a
``ValueError``. Those two are now cases fz-001 and fz-006.

The corpus is synthetic. Payloads are bytes, not documents.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest
from fastapi.testclient import TestClient

CORPUS = json.loads((Path(__file__).resolve().parent / "corpus" / "upload_fuzz.json").read_text())
CASES = CORPUS["cases"]


@pytest.fixture(scope="module")
def client(migrated_db: str):
    import importlib

    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    os.environ["APP_DATABASE_URL"] = (
        f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{migrated_db}"
    )
    from app import config

    importlib.reload(config)
    from app.db import session as db_session

    importlib.reload(db_session)
    from app.main import app

    return TestClient(app)


@pytest.fixture
def owner(client) -> dict:
    """A logged-in tenant, and a stubbed embedder/enqueue so the fuzz hits the parser."""
    email = f"fuzz-{uuid.uuid4().hex[:10]}@example.test"
    body = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "Fuzz Co"},
    )
    assert body.status_code == 201, body.text
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
    return body.json()


def _payload(case: dict) -> bytes:
    raw = case["bytes"]
    if case["encoding"] == "hex":
        return bytes.fromhex(raw)
    return raw.encode("utf-8")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_no_fuzz_case_ever_produces_a_5xx(client, owner, case: dict) -> None:
    """The core property: hostile input is refused or accepted, never a server error."""
    response = client.post(
        f"/api/v1/knowledge/uploads?filename={case['name']}",
        content=_payload(case),
    )
    assert response.status_code < 500, (
        f"{case['id']} ({case['why']}) produced {response.status_code}: {response.text[:400]}"
    )

    if case["kind"] == "reject":
        assert 400 <= response.status_code < 500, (
            f"{case['id']} ({case['why']}) was accepted with {response.status_code}; "
            "the corpus says this must be refused"
        )
        # A refusal without a machine-readable reason is not actionable.
        detail = response.json().get("detail")
        assert isinstance(detail, dict) and "error" in detail, (
            f"{case['id']} refusal has no error body: {response.text[:300]}"
        )
        assert detail["error"]["code"], f"{case['id']} refusal has no code"
    else:
        assert 200 <= response.status_code < 300, (
            f"{case['id']} ({case['why']}) was refused with {response.status_code}: "
            f"{response.text[:300]}"
        )


def test_traversal_filename_is_kept_as_a_label_and_never_becomes_a_path(
    client, owner, migrated_db
) -> None:
    """SEC-07's tenant-scoped paths must survive a traversal attempt in the filename.

    Measured finding while writing this: the API returns ``uri = "../../etc/escape.md"``
    verbatim. That is **not** a vulnerability today, and the test says so precisely rather
    than asserting a sanitisation that does not exist:

    * no route writes a file named from the upload — owner payloads go to
      ``document_versions.payload`` in Postgres and visitor files to a key built from
      server-chosen UUIDs (``widget.uploads.storage_key``), so there is no filesystem
      path for ``..`` to walk;
    * ``uri`` is a display label, rendered as text, never joined to a path.

    What is pinned is therefore the *actual* invariant: the stored bytes live under the
    tenant's own version row, so a hostile name cannot make one tenant's document land
    in another's scope.
    """
    response = client.post(
        "/api/v1/knowledge/uploads?filename=../../etc/escape.md",
        content=b"Traversal attempt body.",
    )
    assert response.status_code < 500
    if response.status_code >= 300:
        return

    import psycopg

    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        # Scoped to this tenant on purpose: an earlier fuzz case is also named
        # ``../escape.md`` under a different tenant, and a bare LIKE would match it and
        # make this assert the wrong row (measured: passed alone, failed in the file).
        row = conn.execute(
            """
            SELECT dv.filename, d.tenant_id
            FROM document_versions dv
            JOIN documents d ON d.id = dv.document_id
            WHERE dv.filename LIKE %s AND d.tenant_id = %s
            """,
            ("%escape.md", owner["tenant_id"]),
        ).fetchone()
    assert row is not None, "the upload must have stored a version row"
    # The row is scoped by tenant_id, not by the name: the hostile name cannot move the
    # document into another tenant's scope, which is the property that matters.
    assert str(row[1]) == owner["tenant_id"]


def test_oversize_body_is_refused_by_the_size_cap_not_the_parser(client, owner) -> None:
    """The cap must fire before parsing: 26,214,400 bytes is the measured limit."""
    limit = 26_214_400
    response = client.post(
        "/api/v1/knowledge/uploads?filename=huge.md",
        content=b"a" * (limit + 1),
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["error"]["code"] == "file_too_large"
