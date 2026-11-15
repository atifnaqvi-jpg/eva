"""Enforce append-only audit log (R29)

Revision ID: 0003_audit_append_only
Revises: 0002_auth_sessions

0001 stated ``audit_logs`` is append-only but then granted UPDATE/DELETE on every
table, including this one - the comment was aspirational, and two foreign keys
contradicted it outright. R29 and the retention table (ARCHITECTURE 19.3: audit is
*never* purged) make append-only a real requirement.

1. The app role loses UPDATE/DELETE/TRUNCATE: a buggy or compromised ``eva_app``
   can only INSERT and SELECT. TRUNCATE is revoked too because it ignores RLS.
2. ``actor_user_id`` loses its foreign key. ``ON DELETE SET NULL`` is an UPDATE on
   the audit row, so deleting a user raised "audit_logs is append-only" and the
   user could never be removed. The UUID is kept as a historical reference; audit
   rows must outlive the entities they describe.
3. ``tenant_id`` FK becomes ``ON DELETE RESTRICT``. It was ``ON DELETE CASCADE``,
   which would let a hard tenant delete silently destroy that tenant's whole trail.
   Tenants are soft-deleted via ``status`` (ARCHITECTURE 16.1), so this only
   affects real hard deletes, which must fail loudly instead of erasing history.
"""

from alembic import op

revision = "0003_audit_append_only"
down_revision = "0002_auth_sessions"
branch_labels = None
depends_on = None

APP_ROLE = "eva_app"


def upgrade() -> None:
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM {APP_ROLE}")
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_actor_user_id_fkey")
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_tenant_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_tenant_id_fkey"
        " FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT audit_logs_tenant_id_fkey")
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_tenant_id_fkey"
        " FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT audit_logs_actor_user_id_fkey"
        " FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL"
    )
    op.execute(f"GRANT UPDATE, DELETE, TRUNCATE ON audit_logs TO {APP_ROLE}")
