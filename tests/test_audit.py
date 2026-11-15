"""WP-04: audit trail (R29).

The important claim is not "we insert a row" but "history cannot be rewritten".
So the append-only guarantees are tested against the real database as the real
application role, not asserted from a docstring.
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
    from app.audit import service as audit_service

    importlib.reload(audit_service)
    from app.auth import service, dependencies, router

    importlib.reload(service)
    importlib.reload(dependencies)
    importlib.reload(router)
    from app.admin import router as admin_router

    importlib.reload(admin_router)
    from app.audit import router as audit_router

    importlib.reload(audit_router)
    from app import main as main_module

    importlib.reload(main_module)

    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client


def _register(client: TestClient) -> dict:
    email = f"audit-{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "A"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["password"] = "correct-horse-battery"
    body["email"] = email
    return body


def _token(response) -> str:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no cookie")


def _login(client: TestClient, body: dict) -> str:
    return _token(
        client.post(
            "/api/v1/auth/login",
            json={"email": body["email"], "password": body["password"]},
        )
    )


def _as(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set("eva_session", token)


# --- events are recorded ----------------------------------------------------


def test_registration_is_audited(client: TestClient) -> None:
    body = _register(client)
    _as(client, _login(client, body))
    logs = client.get("/api/v1/audit-logs").json()
    assert any(e["action"] == "tenant.registered" for e in logs)


def test_admin_mutation_is_audited(client: TestClient) -> None:
    owner = _register(client)
    _as(client, _login(client, owner))

    created = client.post(
        "/api/v1/admin/users",
        json={"email": f"new-{uuid.uuid4().hex[:8]}@example.test",
              "password": "correct-horse-battery", "role": "viewer"},
    )
    assert created.status_code == 201

    logs = client.get("/api/v1/audit-logs").json()
    entries = [e for e in logs if e["action"] == "user.created"]
    assert entries, "creating a user must leave an audit trail"
    assert entries[0]["entity_id"] == created.json()["id"]
    assert entries[0]["actor_user_id"] == owner["user_id"]


def test_failed_login_is_audited(client: TestClient) -> None:
    body = _register(client)
    client.post(
        "/api/v1/auth/login",
        json={"email": body["email"], "password": "definitely-wrong"},
    )
    _as(client, _login(client, body))
    logs = client.get("/api/v1/audit-logs").json()
    assert any(e["action"] == "auth.login_failed" for e in logs)


def test_login_success_is_audited(client: TestClient) -> None:
    body = _register(client)
    _as(client, _login(client, body))
    logs = client.get("/api/v1/audit-logs").json()
    assert any(e["action"] == "auth.login_succeeded" for e in logs)


# --- access control ---------------------------------------------------------


def test_viewer_cannot_read_audit_log(client: TestClient, migrated_db: str) -> None:
    from conftest import _admin_dsn

    body = _register(client)
    token = _login(client, body)
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("UPDATE users SET role = 'viewer' WHERE id = %s", (body["user_id"],))
        conn.commit()

    _as(client, token)
    assert client.get("/api/v1/audit-logs").status_code == 403


def test_audit_log_is_tenant_scoped(client: TestClient) -> None:
    a = _register(client)
    b = _register(client)
    _as(client, _login(client, a))
    logs_a = client.get("/api/v1/audit-logs").json()

    entity_ids = {e["entity_id"] for e in logs_a if e["entity_id"]}
    # Registration audits the tenant, so each tenant's own id must appear for its
    # own reader and must not appear for anyone else.
    assert a["tenant_id"] in entity_ids, "own tenant's events must be visible"
    assert b["tenant_id"] not in entity_ids, "must not see another tenant's events"

    _as(client, _login(client, b))
    logs_b = client.get("/api/v1/audit-logs").json()
    entity_ids_b = {e["entity_id"] for e in logs_b if e["entity_id"]}
    assert b["tenant_id"] in entity_ids_b
    assert a["tenant_id"] not in entity_ids_b


# --- append-only is a database guarantee ------------------------------------


def _app_conn(migrated_db: str):
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    return psycopg.connect(
        host=PG_HOST, port=PG_PORT, dbname=migrated_db,
        user=APP_USER, password=APP_PASSWORD,
    )


def test_app_role_cannot_update_audit_rows(client: TestClient, migrated_db: str) -> None:
    body = _register(client)
    _as(client, _login(client, body))
    client.get("/api/v1/audit-logs")

    with _app_conn(migrated_db) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (body["tenant_id"],))
        log_id = conn.execute("SELECT id FROM audit_logs LIMIT 1").fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_logs SET action = 'tampered' WHERE id = %s", (log_id,))


def test_app_role_cannot_delete_audit_rows(client: TestClient, migrated_db: str) -> None:
    body = _register(client)
    _as(client, _login(client, body))
    client.get("/api/v1/audit-logs")

    with _app_conn(migrated_db) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (body["tenant_id"],))
        log_id = conn.execute("SELECT id FROM audit_logs LIMIT 1").fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_logs WHERE id = %s", (log_id,))


def test_app_role_cannot_truncate_audit_log(client: TestClient, migrated_db: str) -> None:
    with _app_conn(migrated_db) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE audit_logs")


def test_user_deletion_does_not_break_the_trail(
    client: TestClient, migrated_db: str
) -> None:
    """actor_user_id has no FK, so removing a user must not touch history."""
    from conftest import _admin_dsn

    body = _register(client)
    _as(client, _login(client, body))
    before = len(client.get("/api/v1/audit-logs").json())

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (body["user_id"],))
        conn.commit()
        after = conn.execute("SELECT count(*) FROM audit_logs").fetchone()[0]

    assert after >= before, "audit history must survive the actor"
