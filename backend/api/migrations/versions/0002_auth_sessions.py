"""Sessions + globally-unique email for login

Revision ID: 0002_auth_sessions
Revises: 0001_core_schema

Two changes, both required by ARCHITECTURE 17.1 / 19.1:

1. ``users.email`` is ``citext unique`` in 16.1, i.e. unique across the whole
   table. 0001 made it unique per tenant, but login takes email+password with no
   tenant input, so a per-tenant constraint makes login ambiguous. Corrected here
   to match the blueprint.

2. Sessions live in Postgres (not process memory) because uvicorn runs multiple
   workers (20.1) and a cookie minted by one worker must be valid on another.

Both lookups that happen *before* a tenant is known (resolve user by email,
resolve session by token) go through narrow SECURITY DEFINER functions. That is
deliberate: ``users`` and ``sessions`` are RLS-protected, and authentication is
precisely the moment when no tenant context exists yet. The functions expose only
the columns the login path needs and only accept a single lookup key.
"""

from alembic import op

revision = "0002_auth_sessions"
down_revision = "0001_core_schema"
branch_labels = None
depends_on = None

APP_ROLE = "eva_app"


def upgrade() -> None:
    # --- 1. global email uniqueness (16.1) ---------------------------------
    op.drop_constraint("uq_users_tenant_email", "users", type_="unique")
    op.create_unique_constraint("uq_users_email", "users", ["email"])

    # --- 2. sessions --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE sessions (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            -- Only the SHA-256 of the cookie value is stored: a database leak
            -- must not hand over usable session cookies.
            token_hash  text NOT NULL UNIQUE,
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            revoked_at  timestamptz,
            ip          inet,
            user_agent  text
        )
        """
    )
    op.execute("CREATE INDEX ix_sessions_user_id ON sessions (user_id)")
    op.execute(
        "CREATE INDEX ix_sessions_tenant_user ON sessions (tenant_id, user_id)"
    )
    # Supports the expiry sweep without scanning the table.
    op.execute("CREATE INDEX ix_sessions_expires_at ON sessions (expires_at)")

    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions FORCE ROW LEVEL SECURITY")
    # Same fail-closed predicate as every other tenant table: an unset context
    # (NULL) or a leftover empty-string placeholder both yield zero rows.
    predicate = (
        "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    )
    op.execute(
        f"""
        CREATE POLICY sessions_tenant_isolation ON sessions
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON sessions TO {APP_ROLE}")

    # --- 3. pre-tenant lookups ---------------------------------------------
    # SECURITY DEFINER so they run with the owner's rights (they must see past
    # RLS — no tenant context exists yet). Hardened with an explicit empty
    # search_path so a caller cannot hijack name resolution.
    op.execute(
        """
        CREATE FUNCTION auth_user_by_email(p_email text)
        RETURNS TABLE (id uuid, tenant_id uuid, password_hash text,
                       role text, auth_provider text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT u.id, u.tenant_id, u.password_hash, u.role, u.auth_provider
            FROM public.users u
            JOIN public.tenants t ON t.id = u.tenant_id
            WHERE u.email = p_email AND t.status = 'active'
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION auth_session_by_token(p_token_hash text)
        RETURNS TABLE (session_id uuid, tenant_id uuid, user_id uuid, role text,
                       email text, expires_at timestamptz)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT s.id, s.tenant_id, s.user_id, u.role, u.email, s.expires_at
            FROM public.sessions s
            JOIN public.users u ON u.id = s.user_id
            JOIN public.tenants t ON t.id = s.tenant_id
            WHERE s.token_hash = p_token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND t.status = 'active'
        $$
        """
    )
    for fn in ("auth_user_by_email(text)", "auth_session_by_token(text)"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_session_by_token(text)")
    op.execute("DROP FUNCTION IF EXISTS auth_user_by_email(text)")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.create_unique_constraint(
        "uq_users_tenant_email", "users", ["tenant_id", "email"]
    )
