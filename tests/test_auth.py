"""WP-03 auth tests: endpoints, password hashing, RBAC matrix.

Everything runs against the real migrated database and the real FastAPI app —
no mocks. Argon2 cost is lowered via env purely so the suite stays fast; the
production defaults live in app/config.py.
"""

import os

# Must be set before app modules read config at import time.
os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(migrated_db: str) -> TestClient:
    """FastAPI client bound to the migrated test database as the app role."""
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    os.environ["APP_DATABASE_URL"] = (
        f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{migrated_db}"
    )
    os.environ["COOKIE_NAME"] = "eva_session"

    import importlib

    from app import config, db, main

    importlib.reload(config)
    importlib.reload(db.session)

    from app.auth import service
    importlib.reload(service)
    from app.security import passwords
    importlib.reload(passwords)
    from app.auth import dependencies, router
    importlib.reload(dependencies)
    importlib.reload(router)
    from app import main as main_module
    importlib.reload(main_module)

    # https base URL on purpose: the session cookie is Secure, and a client that
    # speaks http would simply never send it back (which is the correct browser
    # behaviour and exactly what we want to keep testing).
    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def fresh_email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.test"


def _session_cookie(response) -> str:
    """Extract the session token from a Set-Cookie header (avoids CookieConflict)."""
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no session cookie in response")


def _register(client: TestClient, email: str, password: str = "correct-horse-battery") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "tenant_name": "Acme"},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- registration / login / logout -----------------------------------------


def test_register_creates_tenant_and_owner(client: TestClient, fresh_email: str) -> None:
    body = _register(client, fresh_email)
    assert body["role"] == "owner"
    assert body["email"] == fresh_email
    uuid.UUID(body["tenant_id"])

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user_id"] == body["user_id"]


def test_register_sets_secure_httponly_cookie(client: TestClient, fresh_email: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": fresh_email, "password": "correct-horse-battery"},
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie, "session cookie must be HttpOnly (19.1)"
    assert "samesite=lax" in cookie, "session cookie must be SameSite=Lax (19.1)"


def test_duplicate_email_is_rejected(client: TestClient, fresh_email: str) -> None:
    _register(client, fresh_email)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": fresh_email.upper(), "password": "correct-horse-battery"},
    )
    assert response.status_code == 409, "email uniqueness must be case-insensitive"


def test_login_succeeds_and_rotates_session(client: TestClient, fresh_email: str) -> None:
    _register(client, fresh_email)
    first = _session_cookie(
        client.post(
            "/api/v1/auth/register",
            json={"email": f"rot-{uuid.uuid4().hex[:8]}@example.test",
                  "password": "correct-horse-battery"},
        )
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": fresh_email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 200
    second = _session_cookie(response)

    assert first and second
    assert first != second, "session id must rotate on login (19.1)"


def test_login_wrong_password_is_rejected(client: TestClient, fresh_email: str) -> None:
    _register(client, fresh_email)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": fresh_email, "password": "not-the-password"},
    )
    assert response.status_code == 401


def test_login_unknown_email_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.test", "password": "whatever-long"},
    )
    assert response.status_code == 401


def test_weak_password_rejected(client: TestClient, fresh_email: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": fresh_email, "password": "short"},
    )
    assert response.status_code == 422


def test_invalid_email_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "correct-horse-battery"},
    )
    assert response.status_code == 422


def test_logout_revokes_session(client: TestClient, fresh_email: str) -> None:
    _register(client, fresh_email)
    token = client.cookies.get("eva_session")
    assert client.get("/api/v1/auth/me").status_code == 200

    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.clear()
    client.cookies.set("eva_session", token)
    assert client.get("/api/v1/auth/me").status_code == 401, "revoked session must fail"


def test_unauthenticated_me_is_401(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_forged_session_token_rejected(client: TestClient) -> None:
    client.cookies.clear()
    client.cookies.set("eva_session", "forged-token-value")
    assert client.get("/api/v1/auth/me").status_code == 401


# --- password hashing -------------------------------------------------------


def test_password_hash_is_argon2id_and_verifies() -> None:
    from app.security.passwords import hash_password, verify_password

    encoded = hash_password("correct-horse-battery")
    assert encoded.startswith("$argon2id$v=19$")
    assert verify_password("correct-horse-battery", encoded)
    assert not verify_password("wrong", encoded)


def test_hash_is_salted() -> None:
    from app.security.passwords import hash_password

    assert hash_password("same") != hash_password("same")


def test_verify_rejects_garbage() -> None:
    from app.security.passwords import verify_password

    assert not verify_password("x", "not-a-phc-string")
    assert not verify_password("x", "")


def test_token_hash_is_not_reversible() -> None:
    from app.security.passwords import hash_token

    token = "abc123"
    digest = hash_token(token)
    assert token not in digest
    assert len(digest) == 64


# --- sessions are in Postgres, not process memory --------------------------


def test_session_row_exists_in_postgres(client: TestClient, fresh_email: str, migrated_db: str) -> None:
    """A cookie issued by one worker must be usable by another (20.1: 2 workers)."""
    from conftest import _admin_dsn
    from app.security.passwords import hash_token

    response = client.post(
        "/api/v1/auth/register",
        json={"email": fresh_email, "password": "correct-horse-battery"},
    )
    token = _session_cookie(response)

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        count = conn.execute(
            "SELECT count(*) FROM sessions WHERE token_hash = %s", (hash_token(token),)
        ).fetchone()[0]
    assert count == 1, "session must be persisted in Postgres"


def test_cookie_value_is_not_stored_verbatim(
    client: TestClient, fresh_email: str, migrated_db: str
) -> None:
    """Only the digest is stored, so a DB leak does not yield usable cookies."""
    from conftest import _admin_dsn

    response = client.post(
        "/api/v1/auth/register",
        json={"email": fresh_email, "password": "correct-horse-battery"},
    )
    token = _session_cookie(response)

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        stored = conn.execute("SELECT token_hash FROM sessions").fetchall()
    assert all(token != row[0] for row in stored)


# --- RBAC matrix ------------------------------------------------------------

# (role, endpoint, method, expected status)
RBAC_MATRIX = [
    ("owner", "/api/v1/auth/me", "GET", 200),
    ("admin", "/api/v1/auth/me", "GET", 200),
    ("viewer", "/api/v1/auth/me", "GET", 200),
    ("owner", "/api/v1/admin/users", "GET", 200),
    ("admin", "/api/v1/admin/users", "GET", 200),
    ("viewer", "/api/v1/admin/users", "GET", 403),
    ("owner", "/api/v1/admin/users", "POST", 201),
    ("admin", "/api/v1/admin/users", "POST", 201),
    ("viewer", "/api/v1/admin/users", "POST", 403),
]


@pytest.mark.parametrize("role,endpoint,method,expected", RBAC_MATRIX)
def test_rbac_matrix(
    client: TestClient, role: str, endpoint: str, method: str, expected: int
) -> None:
    from conftest import _admin_dsn
    from app.auth.service import _issue_session

    email = f"{role}-{uuid.uuid4().hex[:8]}@example.test"
    body = _register(client, email)
    tenant_id = uuid.UUID(body["tenant_id"])
    user_id = uuid.UUID(body["user_id"])

    # Set the role directly in the database; there is no public role-change API yet.
    with psycopg.connect(_admin_dsn(_db_name())) as conn:
        conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
        conn.commit()

    token = _issue_session(tenant_id, user_id, None, None)
    client.cookies.set("eva_session", token)

    if method == "GET":
        response = client.get(endpoint)
    else:
        response = client.post(
            endpoint,
            json={"email": f"new-{uuid.uuid4().hex[:8]}@example.test",
                  "password": "correct-horse-battery", "role": "viewer"},
        )
    assert response.status_code == expected, f"{role} {method} {endpoint}: {response.text}"


def _db_name() -> str:
    import conftest

    return conftest.TEST_DB
