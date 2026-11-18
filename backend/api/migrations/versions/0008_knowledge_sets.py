"""A bot serves a set of document versions, not one version

Revision ID: 0008_knowledge_sets
Revises: 0007_provider_outcomes

RAG-11 implements HALT-07's OPTION 2 (PLAN §8.24 RULINGS, 2026-11-17): *"Expectation is a
multi document knowledge base"* / *"Halt 07: option 2 snapshot table"*.

The defect this closes was measured, not theorised (§8.22): a bot carried one
``active_knowledge_version_id``, so publishing a second document made the first
unreachable — a live question about shipping declined at 0.4367 against a 0.55 floor
while the content sat indexed in the same tenant. "Publish" was therefore not additive
and the documents table promised what the product could not keep.

The ruling's shape is implemented literally:

* ``knowledge_sets`` is one publishable snapshot of a bot's knowledge;
* ``knowledge_set_members`` makes membership explicit, so a publish is a set and never a
  diff of per-version statuses (a status flag cannot express "these five, not those
  five", and a diff has no atomic swap);
* ``bots.active_knowledge_set_id`` is the served pointer, so publishing is still one
  UPDATE — atomic by construction, and rollback is the same swap pointing backwards
  (§10.5);
* the existing single-version pointer is **migrated, not stranded**: every bot that
  pointed at a version gets a one-member set holding that version and is re-pointed at
  it. The old column is then cleared, because two pointers that can disagree is exactly
  the defect this replaces — one served truth, or the next reader re-introduces it.

Draft/live separation needs no extra column: a set is a draft while it is not the
pointer, which keeps ADMN-05's two-column preview meaning (draft = everything indexed,
live = the pointed set) and keeps rollback a re-point.

``tenant_id`` on ``knowledge_set_members`` is not in the ruling's column list. It is
added because every tenant-owned table in §16.1 carries one and is fail-closed under
RLS (§16.3); a member table scoped only by ``set_id`` would be the single tenant table
in this schema with no policy of its own, and a lost predicate there is a query that
succeeds and returns another tenant's knowledge.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_knowledge_sets"
down_revision = "0007_provider_outcomes"
branch_labels = None
depends_on = None

APP_ROLE = "eva_app"

# Same fail-closed predicate as every other tenant table: a missing tenant context
# collapses to NULL and matches nothing rather than everything.
PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "knowledge_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    # Every read is "this bot's sets, newest first" (rollback) or "by id, in tenant".
    op.create_index(
        "ix_knowledge_sets_tenant_bot", "knowledge_sets", ["tenant_id", "bot_id"]
    )

    op.create_table(
        "knowledge_set_members",
        sa.Column("set_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_sets.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
    )
    op.create_index(
        "ix_knowledge_set_members_tenant_set", "knowledge_set_members",
        ["tenant_id", "set_id"],
    )

    # SET NULL rather than CASCADE: deleting a set must not delete the bot. It clears
    # the pointer, and retrieval then returns no candidates and the bot declines —
    # fail-closed, which is the same behaviour as a bot that never published.
    op.add_column(
        "bots",
        sa.Column("active_knowledge_set_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("knowledge_sets.id", ondelete="SET NULL"),
                  nullable=True),
    )

    # --- migrate the single pointer into a one-member set -------------------
    op.execute(
        """
        INSERT INTO knowledge_sets (tenant_id, bot_id, label)
        SELECT b.tenant_id, b.id, 'Migrated from a single published version'
          FROM bots b
         WHERE b.active_knowledge_version_id IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO knowledge_set_members (tenant_id, set_id, document_version_id)
        SELECT s.tenant_id, s.id, b.active_knowledge_version_id
          FROM bots b
          JOIN knowledge_sets s ON s.bot_id = b.id AND s.tenant_id = b.tenant_id
         WHERE b.active_knowledge_version_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE bots
           SET active_knowledge_set_id = s.id
          FROM knowledge_sets s
         WHERE s.bot_id = bots.id
           AND s.tenant_id = bots.tenant_id
           AND bots.active_knowledge_version_id IS NOT NULL
        """
    )
    # Clear it: the version it named now lives in the set. The column survives because
    # §16.1 lists it and the ruling migrates the pointer rather than removing the
    # column, but no code reads or writes it any more — one served truth, or the next
    # reader re-introduces the disagreement this migration exists to remove.
    # ``tests/test_knowledge_sets.py`` pins that a value written here has no effect.
    op.execute(
        "UPDATE bots SET active_knowledge_version_id = NULL"
        " WHERE active_knowledge_set_id IS NOT NULL"
    )

    for table in ("knowledge_sets", "knowledge_set_members"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
            USING ({PREDICATE})
            WITH CHECK ({PREDICATE})
            """
        )


def downgrade() -> None:
    # Restore the single pointer wherever the live set holds exactly one version, which
    # is every set the upgrade itself created and every publish of one document. A
    # multi-document set has no expression in the old schema, so it is left NULL rather
    # than collapsed to an arbitrary member: silently serving one of five documents is
    # the defect RAG-11 removed, and coming back from a downgrade is not where to
    # reintroduce it.
    op.execute(
        """
        UPDATE bots
           SET active_knowledge_version_id = m.document_version_id
          FROM knowledge_set_members m
         WHERE m.set_id = bots.active_knowledge_set_id
           AND m.tenant_id = bots.tenant_id
           AND (SELECT count(*)
                  FROM knowledge_set_members c
                 WHERE c.set_id = bots.active_knowledge_set_id) = 1
        """
    )

    op.drop_column("bots", "active_knowledge_set_id")

    for table in ("knowledge_set_members", "knowledge_sets"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_knowledge_set_members_tenant_set",
                  table_name="knowledge_set_members")
    op.drop_table("knowledge_set_members")
    op.drop_index("ix_knowledge_sets_tenant_bot", table_name="knowledge_sets")
    op.drop_table("knowledge_sets")
