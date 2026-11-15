"""Schema-shape assertions for the §16.1 core schema.

These read the live catalog after the real migration, so they fail if the DDL
drifts from the architecture document.
"""

import os
import subprocess
import sys
import uuid

import psycopg
import pytest

CORE_TABLES = {
    "tenants", "users", "bots", "bot_domains", "knowledge_sources", "documents",
    "document_versions", "chunks", "conversations", "messages", "feedback",
    "handovers", "visitor_files", "api_credentials", "webhooks", "usage_records",
    "audit_logs", "sessions",
}


def test_all_core_tables_exist(migrated_db: str) -> None:
    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    assert CORE_TABLES <= rows, f"missing tables: {CORE_TABLES - rows}"


def test_embedding_dimension_is_parameterised(migrated_db: str) -> None:
    """chunks.embedding must be vector(N) with N from EMBEDDING_DIM, nullable."""
    from conftest import _admin_dsn

    dim = int(os.environ.get("EMBEDDING_DIM", "1024"))
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        udt, nullable = conn.execute(
            "SELECT udt_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'chunks' AND column_name = 'embedding'"
        ).fetchone()
        atttypmod = conn.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()[0]
    assert udt == "vector"
    assert nullable == "YES", "pre-embedding rows require a nullable embedding"
    assert atttypmod == dim, f"expected vector({dim}), got typmod {atttypmod}"


def test_chunks_fts_is_generated_tsvector(migrated_db: str) -> None:
    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        row = conn.execute(
            "SELECT is_generated, generation_expression FROM information_schema.columns "
            "WHERE table_name = 'chunks' AND column_name = 'fts'"
        ).fetchone()
        # Prove it actually populates from text.
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(uuid.uuid4()),))
    assert row is not None, "chunks.fts column missing"
    assert row[0] == "ALWAYS", "fts should be a generated column"
    assert "to_tsvector" in row[1]


def test_fts_populates_from_text(migrated_db: str) -> None:
    """End-to-end: insert a chunk with no embedding and read back its tsvector."""
    from conftest import _admin_dsn

    tid, doc_id, ver_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'fts-tenant')", (tid,)
        )
        source_id = conn.execute(
            "INSERT INTO knowledge_sources (tenant_id, type) VALUES (%s, 'upload') "
            "RETURNING id", (tid,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO documents (id, tenant_id, source_id, title) "
            "VALUES (%s, %s, %s, 'Doc')", (doc_id, tid, source_id),
        )
        conn.execute(
            "INSERT INTO document_versions (id, tenant_id, document_id, version_no, "
            "content_hash) VALUES (%s, %s, %s, 1, 'abc')", (ver_id, tid, doc_id),
        )
        conn.execute(
            "INSERT INTO chunks (tenant_id, document_version_id, ordinal, text) "
            "VALUES (%s, %s, 0, 'Refunds are processed within five business days')",
            (tid, ver_id),
        )
        hits = conn.execute(
            "SELECT count(*) FROM chunks WHERE fts @@ plainto_tsquery('english', 'refund')"
        ).fetchone()[0]
        conn.rollback()
    assert hits == 1, "generated fts did not index the chunk text"


def test_required_indexes_exist(migrated_db: str) -> None:
    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        indexes = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        )}
        hnsw = conn.execute(
            "SELECT amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
            "WHERE c.relname = 'ix_chunks_embedding_hnsw'"
        ).fetchone()
        gin = conn.execute(
            "SELECT amname FROM pg_class c JOIN pg_am am ON am.oid = c.relam "
            "WHERE c.relname = 'ix_chunks_fts'"
        ).fetchone()
    assert hnsw and hnsw[0] == "hnsw"
    assert gin and gin[0] == "gin"
    assert "ix_chunks_tenant_version" in indexes
    # Composite indexes on tenant-owned tables (§16).
    for name in ("ix_bots_tenant_id", "ix_messages_tenant_conversation",
                 "ix_conversations_tenant_bot", "ix_audit_logs_tenant_created"):
        assert name in indexes, f"missing composite index {name}"


def test_no_secret_material_columns(migrated_db: str) -> None:
    """§16.2: only vault references may be stored, never the secret itself."""
    from conftest import _admin_dsn

    forbidden = {"secret", "password", "api_key", "token", "private_key",
                 "credential", "key_ciphertext"}
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        columns = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        ).fetchall()

    offenders = [
        (t, c) for t, c in columns
        if c in forbidden
        # password_hash is a one-way digest (AUTH-03), and *_ref/_reference are
        # vault pointers; _token is the domain-verification public value.
        and c not in {"password_hash", "secret_ref", "key_reference", "verification_token"}
    ]
    assert not offenders, f"secret-bearing columns present: {offenders}"


def test_users_are_sso_ready(migrated_db: str) -> None:
    """FUT-03 / R28: auth_provider defaults to password, external_identity nullable."""
    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        cols = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT column_name, column_default FROM information_schema.columns "
                "WHERE table_name = 'users'"
            )
        }
        nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'external_identity'"
        ).fetchone()[0]
        email_type = conn.execute(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'email'"
        ).fetchone()[0]

    assert cols["auth_provider"] and "password" in cols["auth_provider"]
    assert nullable == "YES"
    assert email_type == "citext", "email must be case-insensitive unique"


def test_rls_enabled_and_forced_on_all_tenant_tables(migrated_db: str) -> None:
    from conftest import _admin_dsn

    tenant_tables = CORE_TABLES - {"tenants"}
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        state = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(%s) AND relkind = 'r'", (list(tenant_tables),)
            )
        }
    for table in tenant_tables:
        enabled, forced = state[table]
        assert enabled, f"{table}: RLS not enabled"
        assert forced, f"{table}: RLS not forced (table owner would bypass)"


def test_migration_downgrades_and_reapplies_cleanly() -> None:
    """Up → down → up on a throwaway database must all succeed (§ WP-02 item 4)."""
    from conftest import API_DIR, PG_HOST, PG_PORT, PG_PASSWORD, PG_USER, _admin_dsn
    from urllib.parse import quote

    db = "eva_test_migrate"
    with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db}"')

    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+psycopg://{PG_USER}:{quote(PG_PASSWORD)}@{PG_HOST}:{PG_PORT}/{db}"
    )
    env["EVA_APP_DB_PASSWORD"] = "appsecret"

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=API_DIR, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"alembic {args} failed:\n{result.stderr}"

    try:
        alembic("upgrade", "head")
        alembic("downgrade", "base")
        with psycopg.connect(_admin_dsn(db)) as conn:
            left = {
                r[0] for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
        # alembic_version is Alembic's own bookkeeping, not part of our schema.
        assert left <= {"alembic_version"}, f"downgrade left tables behind: {left}"

        alembic("upgrade", "head")
        with psycopg.connect(_admin_dsn(db)) as conn:
            back = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            ).fetchone()[0]
        assert back >= len(CORE_TABLES), "re-upgrade did not recreate the schema"
    finally:
        with psycopg.connect(_admin_dsn("postgres"), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
