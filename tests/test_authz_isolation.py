"""AUTH-05: isolation must hold through a real authenticated request path.

WP-02 proved RLS blocks a hand-set GUC. This suite proves the *application* layer
feeds that GUC from the authenticated session only — so an attacker changing
headers, query strings or bodies cannot change which tenant they are.

No mocks: real FastAPI app, real Postgres, real cookies.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(migrated_db: str) -> TestClient:
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    os.environ["APP_DATABASE_URL"] = (
        f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{migrated_db}"
    )
    import importlib

    from app import config

    importlib.reload(config)
    from app.db import session as db_session

    importlib.reload(db_session)
    from app.security import passwords

    importlib.reload(passwords)
    from app.auth import service, dependencies, router

    importlib.reload(service)
    importlib.reload(dependencies)
    importlib.reload(router)
    from app.admin import router as admin_router

    importlib.reload(admin_router)
    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client


def _register(client: TestClient) -> dict:
    email = f"iso-{uuid.uuid4().hex[:12]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "T"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["password"] = "correct-horse-battery"
    return body


def _session_token(response) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no cookie")


def test_users_list_is_scoped_to_authenticated_tenant(client: TestClient) -> None:
    """Two tenants each register; neither can see the other's users."""
    tenant_a = _register(client)
    token_a = _session_token(
        client.post("/api/v1/auth/login", json={
            "email": tenant_a["email"], "password": tenant_a["password"]})
    )
    tenant_b = _register(client)

    # As tenant A, list users: must be exactly the one user A created.
    client.cookies.clear()
    client.cookies.set("eva_session", token_a)
    response = client.get("/api/v1/admin/users")
    assert response.status_code == 200, response.text
    emails = [u["email"] for u in response.json()]

    assert emails == [tenant_a["email"]]
    assert tenant_b["email"] not in emails, "cross-tenant user leak through the API"


def test_client_cannot_choose_tenant_via_headers(client: TestClient) -> None:
    """Spoofing tenant headers/params must not change the scope."""
    tenant_a = _register(client)
    tenant_b = _register(client)
    token_a = _session_token(
        client.post("/api/v1/auth/login", json={
            "email": tenant_a["email"], "password": tenant_a["password"]})
    )

    client.cookies.clear()
    client.cookies.set("eva_session", token_a)
    response = client.get(
        "/api/v1/admin/users",
        headers={
            "X-Tenant-Id": tenant_b["tenant_id"],
            "X-Tenant": tenant_b["tenant_id"],
        },
        params={"tenant_id": tenant_b["tenant_id"]},
    )
    assert response.status_code == 200
    emails = [u["email"] for u in response.json()]
    assert tenant_b["email"] not in emails, "tenant id must never come from request input"


def test_no_cookie_means_no_data(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/admin/users").status_code == 401


def test_forged_cookie_cannot_read_data(client: TestClient) -> None:
    client.cookies.clear()
    client.cookies.set("eva_session", f"forged-{uuid.uuid4().hex}")
    assert client.get("/api/v1/admin/users").status_code == 401


def test_session_of_deleted_user_is_rejected(
    client: TestClient, migrated_db: str
) -> None:
    """Deleting the user cascades the session; the cookie stops working."""
    from conftest import _admin_dsn

    tenant = _register(client)
    token = _session_token(
        client.post("/api/v1/auth/login", json={
            "email": tenant["email"], "password": tenant["password"]})
    )
    client.cookies.clear()
    client.cookies.set("eva_session", token)
    assert client.get("/api/v1/auth/me").status_code == 200

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (tenant["user_id"],))
        conn.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_is_rejected(client: TestClient, migrated_db: str) -> None:
    from conftest import _admin_dsn
    from app.security.passwords import hash_token

    tenant = _register(client)
    token = _session_token(
        client.post("/api/v1/auth/login", json={
            "email": tenant["email"], "password": tenant["password"]})
    )

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 hour'"
            " WHERE token_hash = %s", (hash_token(token),),
        )
        conn.commit()

    client.cookies.clear()
    client.cookies.set("eva_session", token)
    assert client.get("/api/v1/auth/me").status_code == 401
