"""The tenant scoping layer used by application code (§16.3).

Uses the real ``tenant_session`` helper against the real database, so the GUC
plumbing is exercised rather than assumed.
"""

import uuid

import psycopg
import pytest
from sqlalchemy import text


def _point_app_engine_at(monkeypatch: pytest.MonkeyPatch, dbname: str) -> None:
    """Rebuild the module engine against the migrated test database."""
    from sqlalchemy import create_engine

    from app.db import session as session_module
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    url = f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{dbname}"
    engine = create_engine(url)
    monkeypatch.setattr(session_module, "engine", engine)
    # Via the real factory so the tenant-GUC after_begin hook is attached.
    monkeypatch.setattr(
        session_module, "SessionLocal", session_module.make_session_factory(engine)
    )


def test_tenant_session_only_sees_own_rows(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _point_app_engine_at(monkeypatch, migrated_db)
    from app.db.session import tenant_session

    tenant_a, tenant_b = tenant_ids
    with tenant_session(tenant_a) as session:
        own = session.execute(text("SELECT count(*) FROM bots")).scalar_one()
        leaked = session.execute(
            text("SELECT count(*) FROM bots WHERE tenant_id = :b"), {"b": str(tenant_b)}
        ).scalar_one()

    assert own == 1
    assert leaked == 0


def test_tenant_session_does_not_bleed_between_transactions(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GUC is transaction-local: a later unscoped statement sees nothing."""
    _point_app_engine_at(monkeypatch, migrated_db)
    from app.db.session import SessionLocal, tenant_session

    tenant_a, _ = tenant_ids
    with tenant_session(tenant_a) as session:
        assert session.execute(text("SELECT count(*) FROM bots")).scalar_one() == 1

    # Fresh transaction, no tenant context set at all.
    with SessionLocal() as session:
        assert session.execute(text("SELECT count(*) FROM bots")).scalar_one() == 0


def test_psql_level_denial_is_real(migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Direct psql-style session as eva_app is denied cross-tenant rows."""
    from conftest import app_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        # Deliberately qualified with the other tenant's id.
        rows = conn.execute(
            "SELECT name FROM bots WHERE tenant_id = %s", (tenant_b,)
        ).fetchall()
    assert rows == [], f"psql-level RLS denial failed, saw {rows}"
