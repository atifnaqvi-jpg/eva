"""The retrieval pipeline: question in, either ranked chunks or a refusal out.

Implements docs/RETRIEVAL.md. Two properties are worth keeping in mind when
editing this file:

* ``version_ids`` is always resolved from a bot's active pointer, or passed in
  explicitly by the draft-preview path. The pipeline has no "search everything"
  mode, so a draft cannot be reached through the published path and an unfiltered
  eval query is not expressible.
* The confidence floor is a decision, not a score. Below it the pipeline declines
  and says why, because a soft answer from a weak match is the failure mode the
  floor exists to prevent (ARCHITECTURE 10.3.5, R19).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.hybrid import (
    DEFAULT_CANDIDATES,
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_RERANK_IN,
    DEFAULT_RERANK_OUT,
    RankedChunk,
    RetrievalResult,
    dense_candidates,
    reciprocal_rank_fusion,
    sparse_candidates,
)
from app.retrieval.rerank import Reranker, apply_rerank

logger = logging.getLogger(__name__)

DECLINE_NO_CANDIDATES = "no_candidates"
DECLINE_BELOW_FLOOR = "below_confidence_floor"


@dataclass(frozen=True)
class BotRetrievalConfig:
    rerank_enabled: bool = True
    confidence_threshold: float = DEFAULT_CONFIDENCE_FLOOR
    candidates: int = DEFAULT_CANDIDATES


def active_version_ids(session: Session, tenant_id: UUID, bot_id: UUID) -> list[UUID]:
    """The versions the bot currently serves.

    Returns ``[]`` when nothing is published — which the pipeline treats as "no
    candidates", never as "search the whole tenant".
    """
    pointer = session.execute(
        text(
            "SELECT active_knowledge_version_id FROM bots"
            " WHERE id = :bot_id AND tenant_id = :tenant_id"
        ),
        {"bot_id": str(bot_id), "tenant_id": str(tenant_id)},
    ).scalar()
    if pointer is None:
        return []
    valid = session.execute(
        text(
            "SELECT 1 FROM document_versions"
            " WHERE tenant_id = :tenant_id AND id = :pointer"
            "   AND status IN ('ready', 'published')"
        ),
        {"tenant_id": str(tenant_id), "pointer": str(pointer)},
    ).scalar()
    return [pointer] if valid else []


def bot_config(session: Session, tenant_id: UUID, bot_id: UUID) -> BotRetrievalConfig:
    """Floor from the bot, rerank toggle from the tenant's settings jsonb.

    ``tenants.settings`` is part of the frozen schema (16.1), so per-tenant
    rerank config (RAG-06) needs no migration: ``settings->'retrieval'`` may carry
    ``{"rerank": false}``.
    """
    row = session.execute(
        text(
            "SELECT b.confidence_threshold, t.settings"
            "  FROM bots b JOIN tenants t ON t.id = b.tenant_id"
            " WHERE b.id = :bot_id AND b.tenant_id = :tenant_id"
        ),
        {"bot_id": str(bot_id), "tenant_id": str(tenant_id)},
    ).mappings().first()
    if row is None:
        return BotRetrievalConfig()
    retrieval = (row["settings"] or {}).get("retrieval") or {}
    return BotRetrievalConfig(
        rerank_enabled=bool(retrieval.get("rerank", True)),
        confidence_threshold=float(row["confidence_threshold"]),
    )


def retrieve(
    session: Session,
    tenant_id: UUID,
    query_vector: list[float],
    question: str,
    *,
    version_ids: list[UUID],
    config: BotRetrievalConfig | None = None,
    reranker: Reranker | None = None,
    rerank_in: int = DEFAULT_RERANK_IN,
    rerank_out: int = DEFAULT_RERANK_OUT,
) -> RetrievalResult:
    """Dense + sparse -> RRF -> rerank -> floor. Never returns an unranked list."""
    config = config or BotRetrievalConfig()

    dense = dense_candidates(
        session, tenant_id, query_vector, version_ids=version_ids,
        limit=config.candidates,
    )
    sparse = sparse_candidates(
        session, tenant_id, question, version_ids=version_ids,
        limit=config.candidates,
    )
    fused = reciprocal_rank_fusion(dense, sparse)

    if not fused:
        logger.info(
            "retrieval declined: no candidates (tenant=%s versions=%d)",
            tenant_id, len(version_ids),
        )
        return RetrievalResult(
            declined=True,
            reason=DECLINE_NO_CANDIDATES,
            threshold=config.confidence_threshold,
        )

    ranked, reranked = apply_rerank(
        reranker, question, fused[:rerank_in],
        keep=rerank_out, on=config.rerank_enabled,
    )
    best = max((c.score for c in ranked), default=0.0)

    if best < config.confidence_threshold:
        logger.info(
            "retrieval declined: best %.3f below floor %.3f (tenant=%s)",
            best, config.confidence_threshold, tenant_id,
        )
        return RetrievalResult(
            ranked, declined=True, reason=DECLINE_BELOW_FLOOR,
            best_score=best, threshold=config.confidence_threshold, reranked=reranked,
        )

    return RetrievalResult(
        ranked, declined=False, best_score=best,
        threshold=config.confidence_threshold, reranked=reranked,
    )


def citations(session: Session, tenant_id: UUID, chunks: list[RankedChunk]) -> list[dict]:
    """Attach document name + section to each chunk (RAG-08).

    One query for all versions rather than one per chunk: the join is the cheapest
    part of the pipeline and N+1 here would be gratuitous.
    """
    if not chunks:
        return []
    version_ids = sorted({c.document_version_id for c in chunks})
    rows = session.execute(
        text(
            """
            SELECT v.id AS version_id, d.title, d.uri
              FROM document_versions v
              JOIN documents d ON d.id = v.document_id
             WHERE v.tenant_id = :tenant_id
               AND v.id = ANY(:versions)
            """
        ),
        {"tenant_id": str(tenant_id), "versions": [str(v) for v in version_ids]},
    ).mappings()
    by_version = {
        row["version_id"]: {"document": row["title"], "uri": row["uri"]} for row in rows
    }

    out = []
    for chunk in chunks:
        source = by_version.get(chunk.document_version_id, {})
        out.append(
            {
                "chunk_id": str(chunk.chunk_id),
                "document": source.get("document"),
                "uri": source.get("uri"),
                "section": chunk.metadata.get("section"),
                "page": chunk.metadata.get("page"),
                "score": round(chunk.score, 4),
            }
        )
    return out
