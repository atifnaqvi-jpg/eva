"""Rollback restores the previous published set

Revision ID: 0009_knowledge_set_history
Revises: 0008_knowledge_sets

ADMN-17 implements HALT-10's ruling (PLAN §8.47, 2026-11-19): *"rollback restores the
previous set"*. Before this, ``POST /knowledge/rollback`` took a ``version_id`` and built
a **one-member** set from it, so an owner serving five documents who rolled one back ended
up serving that document alone — four silently stopped being answerable, with a 200. The
label and the effect disagreed, which is the defect class SEC-15 closed one layer up.

The ruling's first consequence is why this migration exists at all: *"record that edge
explicitly rather than inferring it from ``created_at``, which two publishes in the same
second would tie."*

* ``knowledge_sets.previous_set_id`` is that edge: the set this bot served before the one
  this row created. A publish records it; a rollback follows it.
* ``ON DELETE SET NULL`` rather than ``CASCADE``: deleting an old set must not delete the
  sets that followed it. A cleared edge makes rollback refuse, which is the fail-closed
  direction — no restore is better than a guessed one.
* **No backfill, deliberately.** Sets created before this revision carry ``NULL``, so a
  rollback on a bot whose current set predates the migration refuses with "there is no
  earlier published set recorded". Guessing the edge here from ``created_at`` is exactly
  what the ruling forbids, and a guess that ties would restore an arbitrary set.

No RLS or grant change: the column lives on ``knowledge_sets``, which ``0008`` already
writes policies and table-level grants for (a table-level UPDATE grant covers a new column).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_knowledge_set_history"
down_revision = "0008_knowledge_sets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_sets",
        sa.Column(
            "previous_set_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # The edge is the only thing this revision adds; the sets themselves are untouched,
    # so a downgrade returns the schema to 0008's shape without losing published
    # knowledge.
    op.drop_column("knowledge_sets", "previous_set_id")
