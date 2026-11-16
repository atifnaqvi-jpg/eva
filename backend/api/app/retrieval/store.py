"""VectorStore interface (RAG-01, ARCHITECTURE 8.3 / 9.4).

The point of this interface is that swapping Postgres+pgvector for Qdrant must
not touch a call site. Everything the app needs is expressed here — upsert chunks
for a version, search within one tenant and one knowledge version, and drop a
version — and nothing in it mentions SQL.

Two invariants are enforced by implementations rather than trusted from callers:

* **Tenant isolation** — a search can only ever see rows for the tenant it was
  asked about. The pgvector implementation gets this from row-level security on
  top of an explicit filter.
* **Version scoping** — search never mixes chunks from two document versions,
  because a vector from an old model would silently poison ranking.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: UUID
    document_version_id: UUID
    text: str
    embedding: list[float]


@dataclass(frozen=True)
class SearchHit:
    chunk_id: UUID
    document_version_id: UUID
    text: str
    score: float
    metadata: dict[str, object]


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, tenant_id: UUID, records: list[VectorRecord]) -> int:
        """Store vectors; returns the number written."""
        ...

    def delete_version(self, tenant_id: UUID, document_version_id: UUID) -> int:
        """Remove every vector belonging to a document version."""
        ...

    def search(
        self,
        tenant_id: UUID,
        query_vector: list[float],
        *,
        version_ids: list[UUID],
        limit: int = 10,
    ) -> list[SearchHit]:
        """Nearest chunks within ``version_ids`` for one tenant, best first."""
        ...
