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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

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
        "top_unanswered_questions": [
            {"question": row["content"], "occurrences": row["occurrences"]}
            for row in questions
        ],
        "definitions": {
            "top_unanswered_questions": (
                "PROVISIONAL — visitor messages ranked by frequency in the window."
                " Placeholder until OPS-05 clusters low-confidence and unanswered"
                " turns; a repeated question is not necessarily an unanswered one."
            )
        },
    }
    return JSONResponse(status_code=200, content=payload)
