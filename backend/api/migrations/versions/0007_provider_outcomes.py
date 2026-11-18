"""Persist per-turn provider outcomes

Revision ID: 0007_provider_outcomes
Revises: 0006_widget_public_lookup

OPS-12 (PLAN §8.25 RULINGS, HALT-09 Reading B). §22 asks for "LLM latency/error by
provider" and OPS-04 watches a provider error-rate spike, but no provider outcome was
persisted anywhere: a failure was logged to stdout and then forgotten, so an alert had
neither a numerator nor a provider dimension.

The shape is one row per turn, deliberately narrow:

* ``outcome`` is ``success`` or ``failure`` — the two facts an error rate is a ratio of;
* ``provider``/``model`` are the ones that actually served the turn (or, on a total
  failure, the chain, because no attempt succeeded);
* ``error_class`` is the exception's *type*, never its message. A provider message can
  echo up to 300 characters of the response body, which can contain the prompt — the
  same rule OPS-02 applies to spans, and this table is read by an alerting poller rather
  than by a human who can judge it.

No visitor content and no conversation text: this is telemetry about a call, which is why
the row cascades away with its conversation instead of surviving erasure the way
``usage_records`` (billing, ON DELETE SET NULL) deliberately does.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_provider_outcomes"
down_revision = "0006_widget_public_lookup"
branch_labels = None
depends_on = None

APP_ROLE = "eva_app"


def upgrade() -> None:
    op.create_table(
        "provider_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure')", name="ck_provider_outcomes_outcome"
        ),
    )
    # OPS-04 reads "the last N minutes" per tenant, which is the only query shape this
    # table ever serves.
    op.create_index(
        "ix_provider_outcomes_tenant_created",
        "provider_outcomes",
        ["tenant_id", "created_at"],
    )

    op.execute(f"GRANT SELECT, INSERT ON provider_outcomes TO {APP_ROLE}")
    # Same fail-closed predicate as every other tenant table (§16.3).
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute("ALTER TABLE provider_outcomes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_outcomes FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY provider_outcomes_tenant_isolation ON provider_outcomes
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS provider_outcomes_tenant_isolation ON provider_outcomes"
    )
    op.drop_index("ix_provider_outcomes_tenant_created", table_name="provider_outcomes")
    op.drop_table("provider_outcomes")
