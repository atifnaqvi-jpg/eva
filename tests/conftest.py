"""Shared fixtures: a real migrated database per test session.

These tests run against the compose Postgres (published on 127.0.0.1:5432) and
exercise the actual Alembic migration — no mocks, no in-memory substitutes.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import psycopg
import pytest

API_DIR = Path(__file__).resolve().parent.parent / "backend" / "api"
sys.path.insert(0, str(API_DIR))

TEST_DB = "eva_test"
PG_HOST = os.environ.get("TEST_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("TEST_PG_PORT", "5432"))
# Owner role (superuser) — DDL only.
PG_USER = os.environ.get("TEST_PG_USER", "eva")
PG_PASSWORD = os.environ.get("TEST_PG_PASSWORD", "changeme")
# Non-superuser runtime role created by the migration.
APP_USER = "eva_app"
APP_PASSWORD = os.environ.get("TEST_APP_DB_PASSWORD", "appsecret")


def _admin_dsn(dbname: str) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
        f"password={PG_PASSWORD} dbname={dbname}"
    )


def app_dsn(dbname: str = TEST_DB) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} user={APP_USER} "
        f"password={APP_PASSWORD} dbname={dbname}"
    )


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[str]:
    """Create a fresh database, migrate it to head, and drop it afterwards."""
    with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+psycopg://{PG_USER}:{quote(PG_PASSWORD)}@{PG_HOST}:{PG_PORT}/{TEST_DB}"
    )
    env["EVA_APP_DB_PASSWORD"] = APP_PASSWORD
    env["EMBEDDING_DIM"] = env.get("EMBEDDING_DIM", "1024")

    # Alembic needs app importable; run from the api directory like the container does.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")

    try:
        yield TEST_DB
    finally:
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')


@pytest.fixture
def tenant_ids(migrated_db: str) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Seed two synthetic tenants and one bot each; clean up after the test."""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        for tid, name in ((tenant_a, "tenant-a"), (tenant_b, "tenant-b")):
            conn.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, %s)", (tid, name)
            )
            conn.execute(
                "INSERT INTO bots (tenant_id, public_id, name) VALUES (%s, %s, %s)",
                (tid, f"bot-{name}", f"Bot {name}"),
            )
    try:
        yield tenant_a, tenant_b
    finally:
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)", ([tenant_a, tenant_b],)
            )
