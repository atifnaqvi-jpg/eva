"""Analytics summary for the owner dashboard (ADMN-08, R31).

**Every number here is one SQL statement**, and each response field carries the
definition that produced it. That is not decoration: "resolution rate" and
"unanswered" have no canonical meaning, and a dashboard that shows 84% without
saying what it measured is a dashboard an owner will make decisions on and later
distrust. The definitions travel with the numbers so a reader can disagree with them
explicitly rather than silently.

**Window and timezone are in the response.** Buckets are UTC because the database
stores ``timestamptz`` and the API has no per-tenant timezone setting yet (ADMN-10 is
where that would arrive). Reporting the window used is the minimum honesty required
while that gap exists.

**What "resolution proxy" means here.** There is no ticket-close signal in this
product, so there is no true resolution rate. Two proxies are exposed, each with its
own definition:

* ``answered_turn_rate`` — assistant turns that produced an answer, i.e. had a
  confidence score. A declined (out-of-scope) turn records no answer.
* ``handover_rate`` — conversations the bot handed to a human, so it did not resolve
  them.

Neither is named "resolution rate" in the response, because naming a proxy after the
thing it approximates is how metrics become lies.

**``top_questions`` is a frequency list and is named one (OPS-05).** It used to be
called ``top_unanswered_questions``, which was false: it ranked visitor messages by
how often they were asked, and a frequently asked question is usually the *best*
answered one. The unanswered questions are ``GET /analytics/knowledge-gaps``, which
detects declined turns and clusters them (``app/analytics/gaps.py``) — a different
computation, so a different field and a different route, rather than one name covering
both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.analytics import gaps
from app.auth.dependencies import RequireViewer, ScopedSession

router = APIRouter(prefix="/analytics", tags=["analytics"])

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366


@router.get("/summary")
def analytics_summary(
    user: RequireViewer,
    session: ScopedSession,
    days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
) -> JSONResponse:
    """Counts, proxies, latency percentiles, tokens, CSAT and top questions (ADMN-08).

    ``days`` is a rolling window ending now, not a calendar month: it needs no
    timezone decision to be unambiguous, which matters while tenants have no
    configured timezone.
    """
    tenant_id = str(user.tenant_id)
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    params = {"tenant_id": tenant_id, "since": since}

    counts = session.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM conversations
                WHERE tenant_id = :tenant_id AND started_at >= :since) AS conversations,
              (SELECT count(*) FROM conversations
                WHERE tenant_id = :tenant_id AND started_at >= :since
                  AND status = 'handed_over') AS handed_over,
              (SELECT count(*) FROM messages
                WHERE tenant_id = :tenant_id AND created_at >= :since
                  AND role = 'visitor') AS visitor_messages,
              (SELECT count(*) FROM messages
                WHERE tenant_id = :tenant_id AND created_at >= :since
                  AND role = 'assistant') AS assistant_messages,
              (SELECT count(*) FROM messages
                WHERE tenant_id = :tenant_id AND created_at >= :since
                  AND role = 'assistant' AND confidence IS NOT NULL) AS answered
            """
        ),
        params,
    ).mappings().one()

    latency = session.execute(
        text(
            """
            SELECT
              percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
              percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
              count(*) AS samples
              FROM messages
             WHERE tenant_id = :tenant_id AND created_at >= :since
               AND role = 'assistant' AND latency_ms IS NOT NULL
            """
        ),
        params,
    ).mappings().one()

    tokens = session.execute(
        text(
            """
            SELECT COALESCE(sum(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(sum(completion_tokens), 0) AS completion_tokens
              FROM messages
             WHERE tenant_id = :tenant_id AND created_at >= :since
            """
        ),
        params,
    ).mappings().one()

    csat = session.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE rating = 'up')   AS up,
                   count(*) FILTER (WHERE rating = 'down') AS down
              FROM feedback
             WHERE tenant_id = :tenant_id AND created_at >= :since
            """
        ),
        params,
    ).mappings().one()

    questions = session.execute(
        text(
            """
            SELECT content, count(*) AS occurrences
              FROM messages
             WHERE tenant_id = :tenant_id AND created_at >= :since
               AND role = 'visitor'
             GROUP BY content
             ORDER BY occurrences DESC, content
             LIMIT 10
            """
        ),
        params,
    ).mappings().all()

    up, down = csat["up"], csat["down"]
    votes = up + down
    conversations = counts["conversations"]
    answered = counts["answered"]
    assistant_messages = counts["assistant_messages"]
    prompt_tokens = int(tokens["prompt_tokens"])
    completion_tokens = int(tokens["completion_tokens"])

    payload = {
        "window": {
            "from": since.isoformat(),
            "to": now.isoformat(),
            "days": days,
            "timezone": "UTC",
            "basis": "rolling window on conversations.started_at and messages.created_at",
        },
        "counts": {
            "conversations": conversations,
            "handed_over_conversations": counts["handed_over"],
            "visitor_messages": counts["visitor_messages"],
            "assistant_messages": assistant_messages,
            "answered_turns": answered,
        },
        "resolution_proxies": {
            "answered_turn_rate": (answered / assistant_messages)
            if assistant_messages
            else None,
            "handover_rate": (counts["handed_over"] / conversations)
            if conversations
            else None,
            "definitions": {
                "answered_turn_rate": (
                    "assistant turns with a confidence score divided by all assistant"
                    " turns; a declined (out-of-scope) turn records no score"
                ),
                "handover_rate": (
                    "conversations whose status is handed_over divided by all"
                    " conversations started in the window"
                ),
                "note": (
                    "Neither is a true resolution rate: this product has no"
                    " ticket-close signal. They are proxies and are labelled as such."
                ),
            },
        },
        "latency_ms": {
            "p50": float(latency["p50"]) if latency["p50"] is not None else None,
            "p95": float(latency["p95"]) if latency["p95"] is not None else None,
            "samples": latency["samples"],
            "definition": "percentile_cont over messages.latency_ms for assistant turns",
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "csat": {
            "up": up,
            "down": down,
            "votes": votes,
            "positive_pct": round(100.0 * up / votes, 1) if votes else None,
        },
        "top_questions": [
            {"question": row["content"], "occurrences": row["occurrences"]}
            for row in questions
        ],
        "definitions": {
            "top_questions": (
                "Visitor messages ranked by exact-text frequency in the window. This"
                " is a popularity list, not a list of unanswered questions: a"
                " frequently asked question is usually a well answered one. Use"
                " GET /analytics/knowledge-gaps for the questions the bot declined."
            )
        },
    }
    return JSONResponse(status_code=200, content=payload)


@router.get("/knowledge-gaps")
def analytics_knowledge_gaps(
    user: RequireViewer,
    session: ScopedSession,
    days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS),
) -> JSONResponse:
    """Declined turns, clustered into ranked topics (OPS-05, §22/§18.2).

    A separate route rather than another field on ``/summary`` because the computation
    is different in kind: ``/summary`` is five constant-cost SQL statements, while this
    embeds up to ``MAX_CLUSTER_QUESTIONS`` texts. Folding it in would make every
    analytics page load wait on the embedding service for a list most visits do not
    read — and would make a summary 500 whenever the embedder was down.

    A viewer may read it: the content is the tenant's own visitor questions, which a
    viewer can already read in the conversation list, and nothing here is telemetry.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    report = gaps.detect_knowledge_gaps(
        session, user.tenant_id, since=since, now=now
    )

    payload = {
        "window": {
            "from": since.isoformat(),
            "to": now.isoformat(),
            "days": days,
            "timezone": "UTC",
        },
        "declined_turns": report.declined_turns,
        "distinct_questions": report.distinct_questions,
        "gap_count": len(report.items),
        "clustered": report.clustered,
        "truncated": report.truncated,
        "gaps": [gap.as_dict() for gap in report.items],
        "note": report.note,
        "definitions": {
            "declined_turn": (
                "an assistant turn whose recorded confidence is below the bot's"
                " confidence threshold at the time of this read; the turn loop writes"
                " the retrieval score as confidence on every retrieval path, and the"
                " pipeline declines exactly when that score is below the floor."
                " Turns with no confidence score (injection refusal, handover offer,"
                " provider outage) are not declined retrieval and are excluded."
            ),
            "clustering": (
                "declined questions are embedded with the same BGE-M3 vectors"
                " retrieval uses and merged when their cosine similarity is at least"
                f" {gaps.CLUSTER_SIMILARITY:g}. clustered=false means the embedding"
                " service was unavailable and the list is exact phrasings, unmerged."
            ),
            "ranking": (
                "each occurrence contributes 0.5 ** (age_days /"
                f" {gaps.HALF_LIFE_DAYS:g}) and a gap's score is the sum, so a topic"
                " asked twice this week outranks one asked five times a month ago."
            ),
        },
    }
    return JSONResponse(status_code=200, content=payload)
