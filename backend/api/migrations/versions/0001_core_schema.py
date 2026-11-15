"""Full §16.1 core schema, tenant RLS, composite indexes, HNSW + GIN.

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-11-15
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

# Non-superuser role the application connects as. RLS does not constrain a
# superuser or a BYPASSRLS role, so the scoped session must run as this role for
# the policies to be meaningful (§16.3).
APP_ROLE = "eva_app"

TENANT_TABLES = (
    "users",
    "bots",
    "bot_domains",
    "knowledge_sources",
    "documents",
    "document_versions",
    "chunks",
    "conversations",
    "messages",
    "feedback",
    "handovers",
    "visitor_files",
    "api_credentials",
    "webhooks",
    "usage_records",
    "audit_logs",
)


def _tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- tenants -----------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("plan", sa.Text(), nullable=False, server_default="free"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "settings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'deleted')", name="ck_tenants_status"
        ),
    )

    # ---- users (SSO-ready, R28) -------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
        sa.Column("auth_provider", sa.Text(), nullable=False, server_default="password"),
        sa.Column("external_identity", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'viewer')", name="ck_users_role"
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ---- bots --------------------------------------------------------------
    op.create_table(
        "bots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("public_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("welcome_message", sa.Text(), nullable=True),
        sa.Column("appearance", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("suggestion_settings", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence_threshold", sa.Float(), nullable=False,
                  server_default=sa.text("0.55")),
        sa.Column("active_knowledge_version_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column("powered_by_visible", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_bots_tenant_id", "bots", ["tenant_id"])

    # ---- bot_domains -------------------------------------------------------
    op.create_table(
        "bot_domains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("verification_token", sa.Text(), nullable=True),
        sa.UniqueConstraint("bot_id", "domain", name="uq_bot_domains_bot_domain"),
    )
    op.create_index("ix_bot_domains_tenant_bot", "bot_domains", ["tenant_id", "bot_id"])

    # ---- knowledge_sources -------------------------------------------------
    op.create_table(
        "knowledge_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "type IN ('upload', 'crawl', 'faq', 'cloud', 'api')",
            name="ck_knowledge_sources_type",
        ),
    )
    op.create_index("ix_knowledge_sources_tenant", "knowledge_sources", ["tenant_id"])

    # ---- documents ---------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_documents_tenant_source", "documents", ["tenant_id", "source_id"])

    # ---- document_versions -------------------------------------------------
    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'published')",
            name="ck_document_versions_status",
        ),
        sa.UniqueConstraint("document_id", "version_no", name="uq_versions_doc_no"),
    )
    op.create_index(
        "ix_document_versions_tenant_doc", "document_versions",
        ["tenant_id", "document_id"],
    )

    # ---- chunks ------------------------------------------------------------
    # embedding is nullable so rows may exist before the embedding job runs.
    op.execute(
        f"""
        CREATE TABLE chunks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            document_version_id uuid NOT NULL
                REFERENCES document_versions(id) ON DELETE CASCADE,
            ordinal integer NOT NULL,
            text text NOT NULL,
            token_count integer NOT NULL DEFAULT 0,
            metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            embedding vector({EMBEDDING_DIM}) NULL,
            fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chunks_tenant_version ON chunks (tenant_id, document_version_id)"
    )
    op.execute("CREATE INDEX ix_chunks_fts ON chunks USING gin (fts)")
    # HNSW cosine; the tenant filter is applied by the query layer and RLS.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # ---- conversations -----------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("visitor_session_id", sa.Text(), nullable=False),
        sa.Column("visitor_name", sa.Text(), nullable=True),
        sa.Column("visitor_email", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("channel", sa.Text(), nullable=False, server_default="text"),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=False, server_default="en"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'handed_over', 'closed')", name="ck_conversations_status"
        ),
        sa.CheckConstraint("channel IN ('text', 'voice')", name="ck_conversations_channel"),
    )
    op.create_index(
        "ix_conversations_tenant_bot", "conversations", ["tenant_id", "bot_id"]
    )
    op.create_index(
        "ix_conversations_tenant_started", "conversations", ["tenant_id", "started_at"]
    )

    # ---- messages ----------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column("suggestions", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "role IN ('visitor', 'assistant', 'system')", name="ck_messages_role"
        ),
    )
    op.create_index(
        "ix_messages_tenant_conversation", "messages", ["tenant_id", "conversation_id"]
    )

    # ---- feedback (CSAT, R40) ---------------------------------------------
    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("rating IN ('up', 'down')", name="ck_feedback_rating"),
    )
    op.create_index("ix_feedback_tenant_message", "feedback", ["tenant_id", "message_id"])

    # ---- handovers ---------------------------------------------------------
    op.create_table(
        "handovers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("transcript_uri", sa.Text(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reason IN ('explicit', 'low_confidence', 'negative_feedback', 'rule')",
            name="ck_handovers_reason",
        ),
        sa.CheckConstraint("channel IN ('email', 'webhook')", name="ck_handovers_channel"),
    )
    op.create_index(
        "ix_handovers_tenant_conversation", "handovers", ["tenant_id", "conversation_id"]
    )

    # ---- visitor_files -----------------------------------------------------
    op.create_table(
        "visitor_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("scan_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_visitor_files_tenant_conversation", "visitor_files",
        ["tenant_id", "conversation_id"],
    )

    # ---- api_credentials (§16.2: references only, never secret material) ---
    op.create_table(
        "api_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("key_reference", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_api_credentials_tenant", "api_credentials", ["tenant_id"])

    # ---- webhooks ----------------------------------------------------------
    op.create_table(
        "webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_webhooks_tenant", "webhooks", ["tenant_id"])

    # ---- usage_records -----------------------------------------------------
    op.create_table(
        "usage_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("units", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ('chat', 'embedding', 'rerank', 'stt', 'tts')",
            name="ck_usage_records_kind",
        ),
    )
    op.create_index(
        "ix_usage_records_tenant_created", "usage_records", ["tenant_id", "created_at"]
    )

    # ---- audit_logs (append-only, R29) ------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        _tenant_column(),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"]
    )

    # ---- append-only enforcement for audit_logs --------------------------
    op.execute(
        """
        CREATE FUNCTION audit_logs_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only (R29)';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_update BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_reject_mutation()"
    )

    # ---- application role + RLS (§16.3) ----------------------------------
    app_password = os.environ.get("EVA_APP_DB_PASSWORD")
    if not app_password:
        raise RuntimeError(
            "EVA_APP_DB_PASSWORD must be set: the app role needs a non-superuser "
            "login so RLS is enforceable"
        )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN;
            END IF;
        END
        $$
        """
    )
    # NOSUPERUSER/NOBYPASSRLS are the whole point: a superuser ignores policies.
    op.execute(
        f"ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE "
        f"PASSWORD '{app_password}'"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # audit_logs is append-only for the app too: no UPDATE/DELETE.
    op.execute(f"REVOKE UPDATE, DELETE ON audit_logs FROM {APP_ROLE}")

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # NULLIF handles both "never set" (NULL) and the empty-string placeholder
        # PostgreSQL leaves behind after a transaction-local set on a pooled
        # connection. Both collapse to NULL, so the comparison is never true:
        # a missing tenant context yields zero rows instead of an error.
        predicate = (
            "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
        )
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
            """
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_reject_mutation()")

    # The role is cluster-wide while a migration is per-database: dropping it here
    # would break a sibling database still using it. Revoke this database's
    # privileges only; CREATE ROLE is idempotent so re-upgrade needs nothing.
    op.execute(f"DROP OWNED BY {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")

    op.execute("DROP TABLE IF EXISTS chunks")
    for table in (
        "audit_logs",
        "usage_records",
        "webhooks",
        "api_credentials",
        "visitor_files",
        "handovers",
        "feedback",
        "messages",
        "conversations",
        "document_versions",
        "documents",
        "knowledge_sources",
        "bot_domains",
        "bots",
        "users",
        "tenants",
    ):
        op.drop_table(table)

    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS citext")
