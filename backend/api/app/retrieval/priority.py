"""Knowledge-source priority (R18, KNOW-10) as a weighted boost on the fused score.

HALT-03 was ruled OPTION B (PLAN §8.24 RULINGS, 2026-11-17): similarity still decides, and a per-source
multiplier nudges the FUSED score (RRF) so an upload outranks a crawl at equal
relevance without letting a stale upload outrank a direct answer.

Two limits are structural rather than conventional:

* The boost lands on ``rrf_score`` — the ordering score — and never on ``score``.
  The confidence floor is a similarity measure (RETRIEVAL.md 8), and a priority
  multiplier that could lift a weak chunk over the floor would convert a ranking
  preference into a confidence claim. Priority orders; it does not make the
  product more sure.
* Weights are configuration, never literals in the ranking path. The shipped
  defaults live in ``DEFAULT_SOURCE_WEIGHTS``; a tenant may override them under
  ``tenants.settings.retrieval.source_weights``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.hybrid import RankedChunk

logger = logging.getLogger(__name__)

# Ruling defaults (PLAN §8.24 RULINGS). Tuned against the WP-10 golden set, not chosen here.
DEFAULT_SOURCE_WEIGHTS: dict[str, float] = {
    "upload": 1.15,
    "faq": 1.10,
    "crawl": 1.00,
}

# A boost outside this band would stop being a tie-shifter and start overriding
# relevance, which the ruling forbids ("strong relevance must still beat tier
# preference"). Out-of-range tuning is clamped rather than obeyed.
MIN_WEIGHT = 0.5
MAX_WEIGHT = 2.0


def source_weights(tenant_settings: Any) -> dict[str, float]:
    """Normalise the priority slice of ``tenants.settings``.

    Tolerant like the other policy readers: this is operator-supplied JSON, and a
    malformed blob must degrade to the shipped defaults rather than break a
    visitor's question. Only known source kinds are accepted, so a typo cannot
    silently introduce a tier the pipeline does not understand.
    """
    weights = dict(DEFAULT_SOURCE_WEIGHTS)
    raw = tenant_settings if isinstance(tenant_settings, dict) else {}
    retrieval = raw.get("retrieval")
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    configured = retrieval.get("source_weights")
    if not isinstance(configured, dict):
        return weights

    for kind, value in configured.items():
        if kind not in DEFAULT_SOURCE_WEIGHTS:
            continue
        # `bool` is an `int` subclass, and `True` as a weight is a mistake worth
        # refusing rather than reading as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        clamped = max(MIN_WEIGHT, min(float(value), MAX_WEIGHT))
        if clamped != float(value):
            logger.warning(
                "source weight %s=%.3f outside [%.1f, %.1f]; using %.1f",
                kind, float(value), MIN_WEIGHT, MAX_WEIGHT, clamped,
            )
        weights[kind] = clamped
    return weights


def source_kinds_for_versions(
    session: Session, tenant_id: UUID, version_ids: list[UUID]
) -> dict[UUID, str]:
    """Map ``document_version_id`` -> ``knowledge_sources.type`` in one query.

    The tenant predicate is explicit as well as enforced by RLS: the join reaches
    three tables, and a query plan that has to be trusted is worse than one that
    states its filter.
    """
    if not version_ids:
        return {}
    rows = session.execute(
        text(
            """
            SELECT v.id AS version_id, s.type AS source_type
              FROM document_versions v
              JOIN documents d ON d.id = v.document_id
              JOIN knowledge_sources s ON s.id = d.source_id
             WHERE v.tenant_id = :tenant_id
               AND v.id = ANY(:versions)
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "versions": [str(v) for v in version_ids],
        },
    ).mappings()
    return {row["version_id"]: row["source_type"] for row in rows}


def apply_source_priority(
    chunks: list[RankedChunk],
    source_kinds: Mapping[UUID, str],
    weights: Mapping[str, float],
) -> list[RankedChunk]:
    """Scale each fused score by its source weight, then re-order.

    Pure, so the arithmetic and the tie-break are testable without a database.

    The tie-break is "equal boosted score -> the order RRF produced". The input
    list is already in RRF's documented total order, so the previous position is
    the correct tie-break and re-deriving dense ranks here would duplicate the
    one rule RETRIEVAL.md 5 already fixes.
    """
    if not chunks:
        return []

    boosted: list[tuple[int, RankedChunk]] = []
    for index, chunk in enumerate(chunks):
        kind = source_kinds.get(chunk.document_version_id)
        weight = float(weights.get(kind, 1.0)) if kind else 1.0
        boosted.append(
            (
                index,
                RankedChunk(
                    chunk_id=chunk.chunk_id,
                    document_version_id=chunk.document_version_id,
                    text=chunk.text,
                    score=chunk.score,
                    metadata=chunk.metadata,
                    dense_score=chunk.dense_score,
                    sparse_score=chunk.sparse_score,
                    rrf_score=chunk.rrf_score * weight,
                    source_kind=kind,
                    priority_weight=weight,
                ),
            )
        )

    boosted.sort(key=lambda pair: (-pair[1].rrf_score, pair[0]))
    return [chunk for _index, chunk in boosted]
