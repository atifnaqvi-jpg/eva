"""Knowledge-gap detection: the questions the bot could not answer (OPS-05).

**Why this is not a frequency list.** ADMN-08 shipped a ``top_unanswered_questions``
that ranked visitor messages by how often they were asked. A frequently asked
question is usually the *best* answered one, so the field was named after something
it did not measure. A gap is a turn the pipeline *declined*, and this module reads
that signal and then collapses paraphrases so ten ways of asking about one missing
topic read as one gap.

**The decline signal is ``confidence < threshold``, and that invariant is the whole
detector.** ``conversation/service.py`` writes ``result.best_score`` as the assistant
message's ``confidence`` on every retrieval path, and the pipeline declines exactly
when that score is below the bot's floor (``retrieval/pipeline.py``: ``best <
config.confidence_threshold``). So a non-NULL confidence below the bot's *current*
threshold is a decline, and the paths that write ``confidence IS NULL`` — injection
refusal, handover offer, embedder failure — are excluded rather than counted as gaps
they are not. A declined turn is joined to the visitor message it was answering, so
the gap is phrased the way the visitor asked it.

**The floor is the bot's current one, not a constant and not a stored snapshot.** A
floor can be tuned (ADMN-03), and comparing every turn against one tenant-wide constant
would be wrong for any tenant that moved it. It is still read *now* rather than as it
was at the turn: a turn declined at 0.55 reads as answered against a floor later
lowered to 0.40, and vice versa. That drift is stated in the response's own definition
rather than hidden, because a past-tense decision cannot be recovered from a column
that only stores the score.

**Clustering uses the existing BGE-M3 pipeline**, because the row asks for semantic
clustering and a hand-written string-similarity heuristic would cluster "how long is
shipping" with "how long is the warranty". The embedder is the same provider the turn
loop uses (``retrieval.embedding.default_embedder``), so no new dependency and no
second vector space. When it is unavailable — the API image has no torch, and the
remote service can be down — the report degrades to an **unclustered, still truthful**
list with ``clustered: false``, because "could not cluster" and "no gaps" must not look
the same (the §8.38/§8.41 lesson, applied to an analytics field).

**Ranking is frequency decayed by recency**: each occurrence contributes
``0.5 ** (age_days / HALF_LIFE_DAYS)``, so a topic asked twice this week outranks one
asked five times last month. The half-life is in the payload rather than implied,
because a ranking whose rule is invisible is a ranking the owner cannot disagree with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.retrieval.embedding import (
    EmbeddingProvider,
    cosine_similarity,
    default_embedder,
)

logger = logging.getLogger(__name__)

# Bounded work in a request path: the query is capped so a tenant with a bad week
# cannot make the analytics screen slow, and the embedding call is capped separately
# because vectors are the expensive half (one HTTP round trip per batch).
MAX_GAP_TURNS = 500
MAX_CLUSTER_QUESTIONS = 120
# Cosine similarity at which two questions are treated as one topic. Measured against
# the live BGE-M3 service, because a guessed value is what shipped the first draft's bug.
#
#   same topic, must merge     0.6942  0.7300  0.7406  0.7658  0.7710  0.8028
#   different topic, must not  0.4315  0.5302  0.5303  0.5456  0.5463  0.5893
#                              0.6244  0.6304  0.6736
#
# 0.68 sits between the highest different-topic pair (0.6736, "how do I return an item"
# vs "what is your refund policy") and the lowest same-topic pair (0.6942, "what is your
# refund policy" vs "can I get my money back").
#
# **The margin is only ~0.02 wide and that is a real limitation, not a safe number.**
# BGE-M3 places short support questions in a narrow band: "what is your refund policy"
# and "do you offer a warranty" are 0.6304 apart-similar, which is *closer* than
# "do you ship to canada" and "can you deliver to canadian addresses" (0.7658)
# same-similar. At 0.62 — also measured, also defensible-looking — a live tenant's
# refund and warranty questions merged into one "gap". Expect some adjacent-topic merges
# and some same-topic splits on real traffic, and treat the label as illustrative: the
# counts and the phrasings list are what the owner acts on.
CLUSTER_SIMILARITY = 0.68
HALF_LIFE_DAYS = 7.0
# Phrasings are illustrative, not exhaustive; the count travels beside them.
MAX_PHRASINGS = 5


@dataclass(frozen=True)
class GapOccurrence:
    """One declined turn, phrased as the visitor asked it."""

    question: str
    asked_at: datetime


@dataclass
class KnowledgeGap:
    question: str
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    score: float
    phrasings: list[dict]
    distinct_phrasings: int = 0

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "occurrences": self.occurrences,
            "distinct_phrasings": self.distinct_phrasings,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "score": round(self.score, 4),
            "phrasings": self.phrasings,
        }


@dataclass
class GapReport:
    """The clustered gaps plus what the numbers mean and how sure they are."""

    items: list[KnowledgeGap] = field(default_factory=list)
    declined_turns: int = 0
    distinct_questions: int = 0
    clustered: bool = False
    truncated: bool = False
    note: str | None = None


def declined_turns(
    session: Session, tenant_id: UUID, *, since: datetime, limit: int = MAX_GAP_TURNS
) -> list[GapOccurrence]:
    """Declined assistant turns in the window, each with the question it answered.

    ``LATERAL`` rather than a self-join on an offset: the preceding visitor message is
    the nearest one at or before the reply, which is what the turn loop answered, and
    an offset join silently pairs a turn with a different question once a conversation
    has an interleaved system message.

    The tenant predicate is repeated even though RLS enforces it — defence in depth,
    and it is not the guard (``tests/test_tenant_isolation``); a lost predicate here
    would return another tenant's questions.
    """
    rows = session.execute(
        text(
            """
            SELECT v.content AS question, v.created_at AS asked_at
              FROM messages m
              JOIN conversations c
                ON c.id = m.conversation_id AND c.tenant_id = m.tenant_id
              JOIN bots b
                ON b.id = c.bot_id AND b.tenant_id = m.tenant_id
              JOIN LATERAL (
                    SELECT content, created_at
                      FROM messages v
                     WHERE v.conversation_id = m.conversation_id
                       AND v.tenant_id = m.tenant_id
                       AND v.role = 'visitor'
                       AND v.created_at <= m.created_at
                     ORDER BY v.created_at DESC
                     LIMIT 1
                   ) v ON true
             WHERE m.tenant_id = :tenant_id
               AND m.role = 'assistant'
               AND m.created_at >= :since
               AND m.confidence IS NOT NULL
               AND m.confidence < b.confidence_threshold
             ORDER BY m.created_at DESC
             LIMIT :limit
            """
        ),
        {"tenant_id": str(tenant_id), "since": since, "limit": limit},
    ).mappings().all()
    return [GapOccurrence(question=r["question"], asked_at=r["asked_at"]) for r in rows]


def published_version_count(session: Session, tenant_id: UUID) -> int:
    """How many document versions the tenant's bots actually serve.

    Only used to word one sentence. With nothing published every decline is a missing
    publication rather than a missing topic, and a list of forty "gaps" would send an
    owner to write content for questions their documents already answer.
    """
    return int(
        session.execute(
            text(
                """
                SELECT count(*)
                  FROM knowledge_set_members m
                  JOIN bots b
                    ON b.active_knowledge_set_id = m.set_id
                   AND b.tenant_id = m.tenant_id
                 WHERE m.tenant_id = :tenant_id
                """
            ),
            {"tenant_id": str(tenant_id)},
        ).scalar_one()
    )


def recency_weight(age_days: float, *, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """One occurrence's contribution to a gap's rank. Halves every half-life."""
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def _label(occurrences: list[GapOccurrence]) -> str:
    """The most frequent exact wording; the *earliest* breaks a tie.

    Not the newest: a topic's label should be how visitors usually ask it, and one
    unusual phrasing from yesterday should not rename a gap asked about all month. The
    tie-break is earliest-first because the input is ordered oldest-first and the
    leader that established the cluster is the wording a reader recognises.
    """
    counts: dict[str, list] = {}
    for occ in occurrences:
        entry = counts.setdefault(occ.question, [0, occ.asked_at])
        entry[0] += 1
    return min(counts, key=lambda q: (-counts[q][0], counts[q][1], q))


def _gaps_from_groups(
    groups: list[list[GapOccurrence]], *, now: datetime
) -> list[KnowledgeGap]:
    """Turn groups of occurrences into ranked gaps. Shared by both paths."""
    gaps: list[KnowledgeGap] = []
    for cluster in groups:
        per_wording: dict[str, list[GapOccurrence]] = {}
        for occ in cluster:
            per_wording.setdefault(occ.question, []).append(occ)
        phrasings = sorted(
            (
                {
                    "question": wording,
                    "occurrences": len(items),
                    "last_seen": max(i.asked_at for i in items).isoformat(),
                }
                for wording, items in per_wording.items()
            ),
            key=lambda p: (-p["occurrences"], p["last_seen"], p["question"]),
        )
        gaps.append(
            KnowledgeGap(
                question=_label(cluster),
                occurrences=len(cluster),
                distinct_phrasings=len(per_wording),
                first_seen=min(o.asked_at for o in cluster),
                last_seen=max(o.asked_at for o in cluster),
                score=sum(
                    recency_weight((now - o.asked_at).total_seconds() / 86400.0)
                    for o in cluster
                ),
                phrasings=phrasings[:MAX_PHRASINGS],
            )
        )

    gaps.sort(key=lambda g: (-g.score, -g.occurrences, g.question))
    return gaps


def cluster_occurrences(
    occurrences: list[GapOccurrence],
    vectors: list[list[float]] | None,
    *,
    now: datetime,
    threshold: float = CLUSTER_SIMILARITY,
) -> list[KnowledgeGap]:
    """Greedy leader clustering in vector space, ranked by decayed frequency.

    Leader clustering rather than k-means on purpose: the number of gaps is the thing
    the owner is trying to learn, so picking *k* first would answer the question with
    its own assumption. Single-pass and deterministic — the input order decides ties,
    and the caller sorts — so two identical windows cannot render two different lists.

    ``vectors=None`` is the "could not embed" path: every occurrence is its own cluster,
    so identical wordings are still grouped by ``_gaps_from_groups`` (one gap per exact
    phrasing) but no two different wordings merge. It is expressed as an absence rather
    than as a raised threshold because cosine similarity cannot exceed 1, so
    "threshold=2.0" would be a comparison that can never be true — a reader could not
    tell whether that was the intent or a bug.
    """
    clusters: list[list[int]] = []
    leaders: list[list[float]] = []
    if vectors is None:
        # No vectors means no cross-wording merge, but identical wordings are still one
        # gap: "same text" is not a similarity judgement, it is equality.
        by_wording: dict[str, int] = {}
        for index, occ in enumerate(occurrences):
            if occ.question in by_wording:
                clusters[by_wording[occ.question]].append(index)
            else:
                by_wording[occ.question] = len(clusters)
                clusters.append([index])
        return _gaps_from_groups(
            [[occurrences[i] for i in members] for members in clusters], now=now
        )

    for index in range(len(occurrences)):
        target = None
        for position, leader in enumerate(leaders):
            if cosine_similarity(vectors[index], leader) >= threshold:
                target = position
                break
        if target is None:
            clusters.append([index])
            leaders.append(vectors[index])
        else:
            clusters[target].append(index)

    return _gaps_from_groups(
        [[occurrences[i] for i in members] for members in clusters], now=now
    )


def detect_knowledge_gaps(
    session: Session,
    tenant_id: UUID,
    *,
    since: datetime,
    now: datetime | None = None,
    embedder: EmbeddingProvider | None = None,
) -> GapReport:
    """Declined turns -> clustered topics -> a ranked, actionable list."""
    now = now or datetime.now(UTC)
    occurrences = declined_turns(session, tenant_id, since=since)
    report = GapReport(declined_turns=len(occurrences))

    if not occurrences:
        # No embedder call at all on the empty path: the API image has no torch, so a
        # tenant that never had a decline must not 500 on a missing model.
        return report

    if published_version_count(session, tenant_id) == 0:
        report.note = (
            "Nothing is published for this bot, so every declined turn is a missing"
            " publication rather than a missing topic. Publish a document, then read"
            " this list as content gaps."
        )

    per_wording: dict[str, list[GapOccurrence]] = {}
    for occ in occurrences:
        per_wording.setdefault(occ.question, []).append(occ)
    for items in per_wording.values():
        items.sort(key=lambda o: o.asked_at)
    report.distinct_questions = len(per_wording)

    wordings = list(per_wording)
    if len(wordings) > MAX_CLUSTER_QUESTIONS:
        # Newest last-seen wins the budget: a gap list is a to-do list, and last
        # quarter's questions are the ones an owner is least likely to act on now.
        wordings.sort(key=lambda q: per_wording[q][-1].asked_at, reverse=True)
        wordings = wordings[:MAX_CLUSTER_QUESTIONS]
        report.truncated = True
    # Oldest first so the greedy pass meets the establishing wording before variants.
    wordings.sort(key=lambda q: per_wording[q][0].asked_at)

    # Every occurrence, labelled by the wording it belongs to, so a cluster can be
    # scored over real occurrences while each wording costs exactly one vector.
    flat = [occ for q in wordings for occ in per_wording[q]]

    try:
        provider = embedder or default_embedder()
        vectors = dict(zip(wordings, provider.embed(wordings), strict=True))
    except Exception:  # noqa: BLE001 - see the module docstring: degrade, never 500
        logger.warning("gap clustering unavailable (tenant=%s)", tenant_id, exc_info=True)
        report.clustered = False
        report.note = (
            "Semantic clustering is unavailable, so these are exact phrasings rather"
            " than topics. Every declined turn is listed, unmerged."
        )
        report.items = cluster_occurrences(flat, None, now=now)
        return report

    report.clustered = True
    report.items = cluster_occurrences(
        flat,
        [vectors[occ.question] for occ in flat],
        now=now,
    )
    return report
