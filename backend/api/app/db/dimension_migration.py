"""Physical embedding-dimension rewrite (ARCHITECTURE 16.1).

pgvector's HNSW operator class is bound to a dimension, so a model change is not
a setting: the ``chunks.embedding`` column must be recreated at the new width, the
HNSW index rebuilt, and every existing vector re-computed. That is exactly why
"a model change = new chunks, never in-place mutation" — the in-place path is
implemented here *only* to destroy the old vectors, never to reinterpret them.

The function is written to be safe to run mid-life:

1. Drop the dimension-bound HNSW index (it cannot survive a type change).
2. Null out embeddings at the old width — they are meaningless in the new space.
3. Change the column type (existing rows become NULL, which is the honest state).
4. Rebuild the index at the new width.

Re-embedding is *not* done here; the caller re-ingests, which is what the
model-switch test exercises end to end.

**Requires the owner role *and* a quiet table.** ``ALTER COLUMN ... TYPE`` takes an
ACCESS EXCLUSIVE lock, so the rewrite blocks while any other transaction holds a
lock on ``chunks`` — including a reader sitting in an open transaction. This is a
maintenance-window operation; the caller must commit or close other sessions
first, or it will wait indefinitely.

**Requires the owner role.** This is DDL, and the application role deliberately
lacks it — ``eva_app`` cannot drop an index or alter a table, so a model change is
an operator action, not something a request handler can trigger. The caller must
pass a session opened with the migration/owner connection.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

HNSW_INDEX = "ix_chunks_embedding_hnsw"


def current_dimension(session: Session) -> int | None:
    row = session.execute(
        text(
            """
            SELECT atttypmod
              FROM pg_attribute
             WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'
            """
        )
    ).scalar()
    # pgvector stores the dimension directly in atttypmod (no +4 header).
    return int(row) if row and int(row) > 0 else None


def migrate_embedding_dimension(session: Session, new_dimension: int) -> dict[str, object]:
    """Rewrite ``chunks.embedding`` to ``vector(new_dimension)``.

    Returns a description of what happened, including how many vectors were
    discarded — losing vectors must be visible, not silent.
    """
    old_dimension = current_dimension(session)
    if old_dimension == new_dimension:
        return {
            "changed": False,
            "old_dimension": old_dimension,
            "new_dimension": new_dimension,
            "vectors_discarded": 0,
        }

    discarded = session.execute(
        text("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    ).scalar() or 0

    # Fail fast instead of queueing behind readers for the rest of the day.
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text(f"DROP INDEX IF EXISTS {HNSW_INDEX}"))
    session.execute(text("UPDATE chunks SET embedding = NULL, embedding_model = NULL,"
                         " embedding_dim = NULL"))
    session.execute(
        text(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({int(new_dimension)})")
    )
    session.execute(
        text(
            f"CREATE INDEX {HNSW_INDEX} ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
    )
    return {
        "changed": True,
        "old_dimension": old_dimension,
        "new_dimension": new_dimension,
        "vectors_discarded": int(discarded),
    }
