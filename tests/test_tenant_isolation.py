"""Adversarial cross-tenant isolation tests (§16.3, §19.1, §21.1, §27).

Three independent attack paths must all fail:

1. Scoped path: a session that declares tenant A must return zero of tenant B's rows.
2. Raw path: a connection with NO tenant context at all must return zero rows
   (an empty GUC means "no tenant", never "all tenants").
3. Direct SQL path: even a hand-written INSERT of a foreign tenant_id must be
   rejected by the WITH CHECK clause.

These queries are deliberately hostile: no WHERE clause, ``SELECT *``, aggregate
counts. If RLS were absent any of them would return another tenant's data.
"""

import uuid

import psycopg
import pytest

TENANT_TABLES = (
    "users",
    "bots",
    "knowledge_sources",
    "documents",
    "document_versions",
    "chunks",
    "knowledge_sets",
    "knowledge_set_members",
    "conversations",
    "messages",
    "handovers",
    "visitor_files",
    "api_credentials",
    "webhooks",
    "usage_records",
    "audit_logs",
)


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_scoped_session_cannot_see_other_tenant(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID], table: str
) -> None:
    """Tenant A's session must see zero rows of tenant B via an unfiltered scan."""
    from conftest import _admin_dsn, app_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(_admin_dsn(migrated_db)) as admin:
        own_rows = admin.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant_a,)
        ).fetchone()[0]

    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        foreign = conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (tenant_b,)
        ).fetchone()[0]
        visible = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    assert foreign == 0, f"{table}: tenant A could read tenant B rows"
    # An unfiltered scan must return exactly the tenant's own rows and nothing else,
    # which catches both leakage and a policy that over-blocks.
    assert visible == own_rows, (
        f"{table}: unfiltered scan saw {visible} rows, tenant A owns {own_rows}"
    )


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_no_tenant_context_returns_no_rows(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID], table: str
) -> None:
    """A connection without app.tenant_id set must see nothing, not everything."""
    from conftest import app_dsn

    with psycopg.connect(app_dsn(migrated_db)) as conn:
        total = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    assert total == 0, f"{table}: rows visible without tenant context"


def test_bots_seeded_rows_not_visible_cross_tenant(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Positive control: each tenant does see its own bot, so RLS is not simply
    blocking everything (a policy that denies all rows would be useless-but-green).
    """
    from conftest import app_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        own = conn.execute("SELECT count(*) FROM bots").fetchone()[0]
        names = {row[0] for row in conn.execute("SELECT name FROM bots")}

    assert own == 1, "tenant A must see exactly its own bot"
    assert names == {"Bot tenant-a"}, f"unexpected visibility: {names}"


def test_insert_for_foreign_tenant_is_rejected(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """WITH CHECK must block writing a row owned by another tenant."""
    from conftest import app_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO bots (tenant_id, public_id, name) VALUES (%s, %s, %s)",
                (tenant_b, f"smuggled-{uuid.uuid4()}", "Smuggled"),
            )
        conn.rollback()


def test_update_cannot_move_row_to_another_tenant(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A tenant cannot rewrite tenant_id to hand its row to someone else."""
    from conftest import app_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE bots SET tenant_id = %s WHERE tenant_id = %s",
                         (tenant_b, tenant_a))
        conn.rollback()


def test_superuser_is_bypassed_but_app_role_is_not(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Documents why the app role exists: the owner connection sees both tenants.

    This is an explicit statement of the trust boundary, not a desired behaviour
    for the application.
    """
    from conftest import _admin_dsn

    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        total = conn.execute("SELECT count(*) FROM bots").fetchone()[0]
    assert total >= 2, "owner role should see all rows (it bypasses RLS)"


def test_audit_logs_are_append_only(
    migrated_db: str, tenant_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """R29: the app role has no UPDATE/DELETE on audit_logs."""
    from conftest import app_dsn

    tenant_a, _ = tenant_ids
    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        conn.execute(
            "INSERT INTO audit_logs (tenant_id, action, entity_type) "
            "VALUES (%s, 'test.action', 'bot')",
            (tenant_a,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_logs SET action = 'tampered'")
        conn.rollback()
