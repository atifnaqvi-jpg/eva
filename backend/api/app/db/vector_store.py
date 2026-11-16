"""pgvector implementation of VectorStore (RAG-01).

pgvector's operator classes are dimension-specific: an ``hnsw`` index over
``vector(1024)`` cannot serve a ``vector(384)`` query. The dimension is therefore
not a runtime detail — a model change physically rewrites the column, which is
exactly why ARCHITECTURE 16.1 calls a model change a re-ingestion rather than an
in-place mutation. ``migrate_embedding_dimension`` performs that rewrite and says
so in its return value.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.store import SearchHit, VectorRecord


class PgVectorStore:
    """VectorStore backed by ``chunks.embedding`` (pgvector, cosine distance)."""

    def __init__(self, session: Session, dimension: int) -> None:
        self._session = session
        self._dimension = dimension

    def upsert(self, tenant_id: UUID, records: list[VectorRecord]) -> int:
        if not records:
            return 0
        for record in records:
            if len(record.embedding) != self._dimension:
                raise ValueError(
                    f"embedding has {len(record.embedding)} dims, store expects "
                    f"{self._dimension}: vectors from different models cannot share "
                    "a column (ARCHITECTURE 16.1)"
                )
        self._session.execute(
            text(
                """
                UPDATE chunks
                   SET embedding = CAST(:embedding AS vector)
                 WHERE id = :chunk_id AND tenant_id = :tenant_id
                """
            ),
            [
                {
                    "chunk_id": str(r.chunk_id),
                    "tenant_id": str(tenant_id),
                    "embedding": _to_pgvector(r.embedding),
                }
                for r in records
            ],
        )
        return len(records)

    def delete_version(self, tenant_id: UUID, document_version_id: UUID) -> int:
        result = self._session.execute(
            text(
                "DELETE FROM chunks WHERE tenant_id = :tenant_id"
                " AND document_version_id = :version_id"
            ),
            {"tenant_id": str(tenant_id), "version_id": str(document_version_id)},
        )
        return result.rowcount or 0

    def search(
        self,
        tenant_id: UUID,
        query_vector: list[float],
        *,
        version_ids: list[UUID],
        limit: int = 10,
    ) -> list[SearchHit]:
        if not version_ids:
            return []
        if len(query_vector) != self._dimension:
            raise ValueError(
                f"query vector has {len(query_vector)} dims, store expects "
                f"{self._dimension}"
            )
        rows = self._session.execute(
            text(
                """
                SELECT id,
                       document_version_id,
                       text,
                       metadata,
                       1 - (embedding <=> CAST(:query AS vector)) AS score
                  FROM chunks
                 WHERE tenant_id = :tenant_id
                   AND document_version_id = ANY(:versions)
                   AND embedding IS NOT NULL
                 ORDER BY embedding <=> CAST(:query AS vector)
                 LIMIT :limit
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "versions": [str(v) for v in version_ids],
                "query": _to_pgvector(query_vector),
                "limit": limit,
            },
        ).mappings()
        return [
            SearchHit(
                chunk_id=row["id"],
                document_version_id=row["document_version_id"],
                text=row["text"],
                score=float(row["score"]),
                metadata=dict(row["metadata"] or {}),
            )
            for row in rows
        ]


def _to_pgvector(vector: list[float]) -> str:
    # pgvector's text input format; passed as a bind parameter, never spliced.
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
