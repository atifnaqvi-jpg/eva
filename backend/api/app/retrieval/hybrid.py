"""Hybrid retrieval: dense + sparse candidates, RRF fusion, rerank, decline.

The fusion order is normative in ``docs/RETRIEVAL.md``; this module implements it
and nothing beyond it. Two rules are structural rather than conventional:

* Every candidate query carries ``tenant_id`` **and** ``version_ids``. There is no
  method here that can retrieve without both, so "unfiltered, just for eval" is not
  expressible rather than merely discouraged.
* Source kind never enters a score, order or tie-break. R18 priority is blocked on
  HALT-03 (PLAN 8.4.1), so the pipeline is deliberately blind to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# RRF paper default. Config, not a tuned constant: changing it changes rankings.
RRF_K = 60

DEFAULT_CANDIDATES = 20
MAX_CANDIDATES = 100
DEFAULT_RERANK_IN = 20
DEFAULT_RERANK_OUT = 5
DEFAULT_CONFIDENCE_FLOOR = 0.55


@dataclass(frozen=True)
class Candidate:
    """One chunk as seen by a single retriever."""

    chunk_id: UUID
    document_version_id: UUID
    text: str
    score: float
    metadata: dict[str, object]

    @property
    def ordinal(self) -> int:
        value = self.metadata.get("ordinal")
        return value if isinstance(value, int) else 0


@dataclass
class RankedChunk:
    chunk_id: UUID
    document_version_id: UUID
    text: str
    score: float
    metadata: dict[str, object]
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float = 0.0

    @property
    def ordinal(self) -> int:
        value = self.metadata.get("ordinal")
        return value if isinstance(value, int) else 0


@dataclass
class RetrievalResult:
    """Everything the caller needs, including why nothing was returned."""

    chunks: list[RankedChunk] = field(default_factory=list)
    declined: bool = False
    reason: str | None = None
    best_score: float = 0.0
    threshold: float = DEFAULT_CONFIDENCE_FLOOR
    reranked: bool = False

    @property
    def answered(self) -> bool:
        return not self.declined


def _clamp(value: int, default: int) -> int:
    return max(1, min(value, MAX_CANDIDATES)) if value else default


def dense_candidates(
    session: Session,
    tenant_id: UUID,
    query_vector: list[float],
    *,
    version_ids: list[UUID],
    limit: int = DEFAULT_CANDIDATES,
) -> list[Candidate]:
    """pgvector HNSW cosine. Same table, same filters as the sparse path."""
    if not version_ids:
        # An empty version list means "this bot has nothing published". Returning
        # rows here would silently search every version in the tenant.
        return []
    rows = session.execute(
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
            "limit": _clamp(limit, DEFAULT_CANDIDATES),
        },
    ).mappings()
    return [
        Candidate(
            chunk_id=row["id"],
            document_version_id=row["document_version_id"],
            text=row["text"],
            score=float(row["score"]),
            metadata=dict(row["metadata"] or {}),
        )
        for row in rows
    ]


def sparse_candidates(
    session: Session,
    tenant_id: UUID,
    question: str,
    *,
    version_ids: list[UUID],
    limit: int = DEFAULT_CANDIDATES,
) -> list[Candidate]:
    """Postgres FTS over ``chunks.fts``, ranked by ``ts_rank_cd``.

    ``websearch_to_tsquery`` is used because it tolerates real user input
    (quotes, ``or``, stray punctuation) instead of raising on it.
    """
    if not version_ids or not question.strip():
        return []
    rows = session.execute(
        text(
            """
            SELECT id,
                   document_version_id,
                   text,
                   metadata,
                   ts_rank_cd(fts, websearch_to_tsquery('english', :question)) AS score
              FROM chunks
             WHERE tenant_id = :tenant_id
               AND document_version_id = ANY(:versions)
               AND fts @@ websearch_to_tsquery('english', :question)
             ORDER BY score DESC
             LIMIT :limit
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "versions": [str(v) for v in version_ids],
            "question": question,
            "limit": _clamp(limit, DEFAULT_CANDIDATES),
        },
    ).mappings()
    return [
        Candidate(
            chunk_id=row["id"],
            document_version_id=row["document_version_id"],
            text=row["text"],
            score=float(row["score"]),
            metadata=dict(row["metadata"] or {}),
        )
        for row in rows
    ]


def reciprocal_rank_fusion(
    dense: list[Candidate],
    sparse: list[Candidate],
    *,
    k: int = RRF_K,
) -> list[RankedChunk]:
    """Merge two ranked lists by RRF, with a total order for determinism.

    Tie-break (RETRIEVAL.md 5): equal RRF score -> better dense rank -> lower
    ordinal -> lower chunk id. Without it, identical inputs can produce different
    orders and a flaky eval gets blamed on the model.
    """
    merged: dict[UUID, RankedChunk] = {}
    dense_rank: dict[UUID, int] = {}
    sparse_rank: dict[UUID, int] = {}

    for rank, candidate in enumerate(dense, start=1):
        dense_rank[candidate.chunk_id] = rank
        merged[candidate.chunk_id] = RankedChunk(
            chunk_id=candidate.chunk_id,
            document_version_id=candidate.document_version_id,
            text=candidate.text,
            score=candidate.score,
            metadata=candidate.metadata,
            dense_score=candidate.score,
            rrf_score=1.0 / (k + rank),
        )

    for rank, candidate in enumerate(sparse, start=1):
        sparse_rank[candidate.chunk_id] = rank
        contribution = 1.0 / (k + rank)
        existing = merged.get(candidate.chunk_id)
        if existing is None:
            merged[candidate.chunk_id] = RankedChunk(
                chunk_id=candidate.chunk_id,
                document_version_id=candidate.document_version_id,
                text=candidate.text,
                score=candidate.score,
                metadata=candidate.metadata,
                sparse_score=candidate.score,
                rrf_score=contribution,
            )
        else:
            existing.sparse_score = candidate.score
            existing.rrf_score += contribution

    def order(chunk: RankedChunk) -> tuple[float, int, int, str]:
        return (
            -chunk.rrf_score,
            dense_rank.get(chunk.chunk_id, len(dense) + 1),
            chunk.ordinal,
            str(chunk.chunk_id),
        )

    return sorted(merged.values(), key=order)


def _to_pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
