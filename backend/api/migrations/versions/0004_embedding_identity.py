"""Record which embedding model produced each vector

Revision ID: 0004_embedding_identity
Revises: 0003_audit_append_only

ARCHITECTURE 16.1: "Dimension N set by the configured embedding model; a model
change = new chunks (re-ingestion), never in-place mutation." Dimension alone does
not identify a model — two providers can both emit 1024 dims and their vectors are
not comparable — so the provider key is stored alongside the vector and search
refuses to mix keys.

``embedding_dim`` is recorded too, so a row that somehow survived a dimension
rewrite can be detected instead of silently comparing across vector spaces.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_embedding_identity"
down_revision = "0003_audit_append_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("embedding_dim", sa.Integer(), nullable=True))
    op.create_index(
        "ix_chunks_tenant_version_model",
        "chunks",
        ["tenant_id", "document_version_id", "embedding_model"],
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_tenant_version_model", table_name="chunks")
    op.drop_column("chunks", "embedding_dim")
    op.drop_column("chunks", "embedding_model")
