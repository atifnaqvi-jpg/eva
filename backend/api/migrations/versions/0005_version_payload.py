"""Store the source bytes a version was built from

Revision ID: 0005_version_payload
Revises: 0004_embedding_identity

The ingestion job is retried by arq, and KNOW-09/R20.4 require retries to be safe.
A retry cannot re-read the upload body — the request is long gone — so the bytes
are persisted with the version. That also makes "re-ingest this version" an
operator action, which the dimension-switch path needs.

``payload`` is nullable: crawl and FAQ versions have no file bytes, and existing
rows predate the column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_version_payload"
down_revision = "0004_embedding_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("filename", sa.Text(), nullable=True))
    op.add_column("document_versions", sa.Column("payload", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_versions", "payload")
    op.drop_column("document_versions", "filename")
