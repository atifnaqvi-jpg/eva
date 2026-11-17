"""Reranker interface (RAG-06).

A reranker scores ``(question, chunk)`` jointly and reorders the fused candidates.
Per RETRIEVAL.md 6, a rerank failure degrades to the RRF order and never fails the
request; the response says ``reranked: false`` so the degradation is visible to the
eval harness instead of becoming silent drift.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.retrieval.hybrid import RankedChunk

logger = logging.getLogger(__name__)


@runtime_checkable
class Reranker(Protocol):
    def score(self, question: str, texts: list[str]) -> list[float]:
        """Relevance in [0, 1], one per text, same order as the input."""
        ...


class NoopReranker:
    """Rerank disabled for this tenant: keep the fused order."""

    def score(self, question: str, texts: list[str]) -> list[float]:
        raise NotImplementedError("NoopReranker must not be called")


def apply_rerank(
    reranker: Reranker | None,
    question: str,
    chunks: list[RankedChunk],
    *,
    keep: int,
    on: bool = True,
) -> tuple[list[RankedChunk], bool]:
    """Rerank ``chunks`` and keep the best ``keep``. Returns (chunks, reranked).

    Never raises: an exception or a malformed score list downgrades to the fused
    order, which is a worse ranking but a working product.
    """
    if not on or reranker is None or not chunks:
        return chunks[:keep], False

    try:
        scores = reranker.score(question, [c.text for c in chunks])
    except Exception:  # noqa: BLE001 - any reranker failure degrades, never fails
        logger.warning("rerank failed; falling back to fused order", exc_info=True)
        return chunks[:keep], False

    if len(scores) != len(chunks):
        logger.warning(
            "rerank returned %d scores for %d chunks; ignoring it",
            len(scores), len(chunks),
        )
        return chunks[:keep], False

    rescored = [
        RankedChunk(
            chunk_id=c.chunk_id,
            document_version_id=c.document_version_id,
            text=c.text,
            score=float(score),
            metadata=c.metadata,
            dense_score=c.dense_score,
            sparse_score=c.sparse_score,
            rrf_score=c.rrf_score,
            source_kind=c.source_kind,
            priority_weight=c.priority_weight,
        )
        for c, score in zip(chunks, scores, strict=True)
    ]
    rescored.sort(key=lambda c: (-c.score, str(c.chunk_id)))
    return rescored[:keep], True
