"""Owner-facing conversation archive: list, transcript, export (ADMN-06/07).

**Why this is not in ``router.py``.** That module serves the *visitor* turn loop and
is documented as such; this one serves the owner reading the same conversations
back. They share a URL prefix and nothing else, and mixing them would put a
``StreamingResponse`` turn endpoint next to a GDPR export in one file.

**Tenant scope.** Every query here is filtered on ``tenant_id`` taken from the
authenticated session (``ScopedSession``), never from a query parameter or header.
Row-level security would also stop a cross-tenant read, but the explicit predicate
is what makes the intent legible — and it is the thing the isolation tests assert.

**One conversation, one transcript.** The transcript returned by ``GET /{id}`` and
the body of ``GET /{id}/export?format=txt`` are built by the *same* function
(``handover.transcript.compile_transcript``) that builds the handover email. That is
deliberate: §13.2 promises the owner sees what the email contained, and ADMN-07
cites R32, where the export is also the data-subject access mechanism. An export
that disagreed with the view would be a compliance defect, not a cosmetic one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.auth.dependencies import RequireViewer, ScopedSession
from app.handover.transcript import compile_transcript
from app.observability import logs as obs_logs

router = APIRouter(prefix="/conversations", tags=["conversations"])

PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MAX = 200


class HandoverBadge(BaseModel):
    """Handover state as the list may show it.

    ``ambiguous`` exists because the database cannot tell two situations apart.
    ``status='pending'`` is written when a trigger fires, and cleared to
    ``recorded``/``notified``/``failed`` when delivery is resolved. If delivery
    *raises*, the row stays ``pending`` — indistinguishable from a visitor who was
    shown the contact form and has not submitted it. HAND-01's keyword trigger and
    HAND-03's low-confidence trigger both leave a conversation here on purpose.

    So the API refuses to let a client render "waiting for the visitor" as fact: when
    ``ambiguous`` is true, the row is ``pending`` for either reason and the UI must
    say so. This is the observability gap recorded in PLAN §8.20; closing it needs a
    distinct state, which is a schema change and therefore not taken here.
    """

    reason: str
    status: str
    notified_at: str | None = None
    ambiguous: bool = False


class ConversationSummary(BaseModel):
    id: str
    status: str
    bot_id: str
    bot_name: str
    channel: str
    locale: str
    page_url: str | None = None
    visitor_name: str | None = None
    visitor_email: str | None = None
    started_at: str
    last_activity_at: str
    ended_at: str | None = None
    message_count: int
    handover: HandoverBadge | None = None
    csat_up: int = 0
    csat_down: int = 0


class ConversationList(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class Turn(BaseModel):
    id: str
    role: str
    content: str
    citations: list[Any] = []
    confidence: float | None = None
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: str
    feedback: str | None = None
    feedback_comment: str | None = None


class ConversationDetail(BaseModel):
    """Both readings of the same conversation: the human transcript and the rows.

    ``transcript`` is byte-identical to the body of the handover email's transcript
    section, so the dashboard cannot drift from what was mailed.
    """

    summary: ConversationSummary
    transcript: str
    turns: list[Turn]


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "invalid_uuid", "message": f"bad {field}"}},
        ) from None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_bound(value: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 instant, rejecting a naive one.

    A naive timestamp has two readings (whose timezone?), and silently assuming UTC
    would shift every window by hours for an owner who thought they meant local
    time. Returning 422 is the honest option.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "invalid_time",
                    "message": f"{field} must be ISO-8601",
                }
            },
        ) from None
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "naive_time",
                    "message": f"{field} must include a UTC offset, e.g. 2026-11-01T00:00:00Z",
                }
            },
        )
    return parsed.astimezone(UTC)


@router.get("", response_model=ConversationList)
def list_conversations(
    user: RequireViewer,
    session: ScopedSession,
    status_filter: str | None = Query(default=None, alias="status"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    limit: int = Query(default=PAGE_LIMIT_DEFAULT, ge=1, le=PAGE_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
) -> ConversationList:
    """Search conversations by status and start-time window (ADMN-06).

    ``from`` filters on ``started_at``, not ``last_activity_at``: an owner looking at
    "last week" means conversations that *started* then, and using activity would
    drag a months-old thread back into the window every time a visitor replied.
    """
    start = _parse_bound(from_, "from")
    end = _parse_bound(to, "to")
    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "inverted_window",
                    "message": "from must not be later than to",
                }
            },
        )

    where = ["c.tenant_id = :tenant_id"]
    params: dict[str, Any] = {"tenant_id": str(user.tenant_id)}
    if status_filter is not None:
        where.append("c.status = :status")
        params["status"] = status_filter
    if start is not None:
        where.append("c.started_at >= :start")
        params["start"] = start
    if end is not None:
        where.append("c.started_at < :end")
        params["end"] = end
    clause = " AND ".join(where)

    total = session.execute(
        text(f"SELECT count(*) FROM conversations c WHERE {clause}"), params
    ).scalar_one()

    params.update({"limit": limit, "offset": offset})
    rows = session.execute(
        text(
            f"""
            SELECT c.id, c.status, c.bot_id, b.name AS bot_name, c.channel, c.locale,
                   c.page_url, c.visitor_name, c.visitor_email, c.started_at,
                   c.last_activity_at, c.ended_at,
                   COALESCE(m.n, 0) AS message_count,
                   h.reason AS handover_reason, h.status AS handover_status,
                   h.notified_at,
                   COALESCE(f.up, 0) AS csat_up, COALESCE(f.down, 0) AS csat_down
              FROM conversations c
              JOIN bots b ON b.id = c.bot_id AND b.tenant_id = c.tenant_id
              LEFT JOIN LATERAL (
                   SELECT count(*) AS n FROM messages
                    WHERE conversation_id = c.id AND tenant_id = c.tenant_id
              ) m ON true
              LEFT JOIN LATERAL (
                   SELECT reason, status, notified_at FROM handovers
                    WHERE conversation_id = c.id AND tenant_id = c.tenant_id
                    ORDER BY id LIMIT 1
              ) h ON true
              LEFT JOIN LATERAL (
                   SELECT count(*) FILTER (WHERE f.rating = 'up')   AS up,
                          count(*) FILTER (WHERE f.rating = 'down') AS down
                     FROM feedback f
                     JOIN messages mm ON mm.id = f.message_id
                                      AND mm.tenant_id = f.tenant_id
                    WHERE mm.conversation_id = c.id AND f.tenant_id = c.tenant_id
              ) f ON true
             WHERE {clause}
             ORDER BY c.started_at DESC, c.id DESC
             LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    return ConversationList(
        items=[_summary(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def _summary(row: Any) -> ConversationSummary:
    handover = None
    if row["handover_reason"] is not None:
        handover = HandoverBadge(
            reason=row["handover_reason"],
            status=row["handover_status"],
            notified_at=_iso(row["notified_at"]),
            # See HandoverBadge: pending is two states wearing one name.
            ambiguous=row["handover_status"] == "pending",
        )
    return ConversationSummary(
        id=str(row["id"]),
        status=row["status"],
        bot_id=str(row["bot_id"]),
        bot_name=row["bot_name"],
        channel=row["channel"],
        locale=row["locale"],
        page_url=row["page_url"],
        visitor_name=row["visitor_name"],
        visitor_email=row["visitor_email"],
        started_at=_iso(row["started_at"]),
        last_activity_at=_iso(row["last_activity_at"]),
        ended_at=_iso(row["ended_at"]),
        message_count=row["message_count"],
        handover=handover,
        csat_up=row["csat_up"],
        csat_down=row["csat_down"],
    )


def _load(session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> dict:
    """Load everything held about one conversation, or 404.

    404 rather than 403 for another tenant's id: a 403 would confirm the id exists,
    which is the enumeration oracle the isolation tests are written against.
    """
    # OPS-01. One place, so the three read routes (detail, export, and anything added
    # later) all carry the conversation in their log lines.
    obs_logs.bind(conversation_id=conversation_id)
    row = session.execute(
        text(
            """
            SELECT c.id, c.status, c.bot_id, b.name AS bot_name, c.channel, c.locale,
                   c.page_url, c.visitor_session_id, c.visitor_name, c.visitor_email,
                   c.started_at, c.last_activity_at, c.ended_at,
                   h.reason AS handover_reason, h.status AS handover_status,
                   h.notified_at
              FROM conversations c
              JOIN bots b ON b.id = c.bot_id AND b.tenant_id = c.tenant_id
              LEFT JOIN LATERAL (
                   SELECT reason, status, notified_at FROM handovers
                    WHERE conversation_id = c.id AND tenant_id = c.tenant_id
                    ORDER BY id LIMIT 1
              ) h ON true
             WHERE c.id = :id AND c.tenant_id = :tenant_id
            """
        ),
        {"id": str(conversation_id), "tenant_id": str(tenant_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "conversation_not_found",
                    "message": "unknown conversation",
                }
            },
        )

    messages = session.execute(
        text(
            """
            SELECT m.id, m.role, m.content, m.citations, m.confidence, m.model,
                   m.latency_ms, m.prompt_tokens, m.completion_tokens, m.created_at,
                   f.rating AS feedback, f.comment AS feedback_comment
              FROM messages m
              LEFT JOIN feedback f ON f.message_id = m.id AND f.tenant_id = m.tenant_id
             WHERE m.conversation_id = :id AND m.tenant_id = :tenant_id
             ORDER BY m.created_at, m.id
            """
        ),
        {"id": str(conversation_id), "tenant_id": str(tenant_id)},
    ).mappings().all()

    # Visitor uploads hold no bytes (see app/widget/uploads.py), so what is held about
    # them is this metadata, and the SAR export has to carry it.
    files = session.execute(
        text(
            """
            SELECT id, mime, size_bytes, scan_status, created_at
              FROM visitor_files
             WHERE conversation_id = :id AND tenant_id = :tenant_id
             ORDER BY created_at, id
            """
        ),
        {"id": str(conversation_id), "tenant_id": str(tenant_id)},
    ).mappings().all()

    return {"row": row, "messages": messages, "files": files}


def _render(row: Any, messages: Any) -> str:
    """The one transcript renderer, shared with the handover email."""
    return compile_transcript(
        messages=messages,
        bot_name=row["bot_name"],
        started_at=row["started_at"],
        page_url=row["page_url"],
        reason=row["handover_reason"],
        contact={"name": row["visitor_name"], "email": row["visitor_email"]},
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str, user: RequireViewer, session: ScopedSession
) -> ConversationDetail:
    """Transcript view plus the underlying turns, with CSAT per answer (ADMN-06)."""
    data = _load(session, user.tenant_id, _uuid(conversation_id, "conversation_id"))
    row, messages = data["row"], data["messages"]

    handover = None
    if row["handover_reason"] is not None:
        handover = HandoverBadge(
            reason=row["handover_reason"],
            status=row["handover_status"],
            notified_at=_iso(row["notified_at"]),
            ambiguous=row["handover_status"] == "pending",
        )
    summary = ConversationSummary(
        id=str(row["id"]),
        status=row["status"],
        bot_id=str(row["bot_id"]),
        bot_name=row["bot_name"],
        channel=row["channel"],
        locale=row["locale"],
        page_url=row["page_url"],
        visitor_name=row["visitor_name"],
        visitor_email=row["visitor_email"],
        started_at=_iso(row["started_at"]),
        last_activity_at=_iso(row["last_activity_at"]),
        ended_at=_iso(row["ended_at"]),
        message_count=len(messages),
        handover=handover,
        csat_up=sum(1 for m in messages if m["feedback"] == "up"),
        csat_down=sum(1 for m in messages if m["feedback"] == "down"),
    )
    return ConversationDetail(
        summary=summary,
        transcript=_render(row, messages),
        turns=[
            Turn(
                id=str(m["id"]),
                role=m["role"],
                content=m["content"],
                citations=m["citations"] or [],
                confidence=m["confidence"],
                model=m["model"],
                latency_ms=m["latency_ms"],
                prompt_tokens=m["prompt_tokens"],
                completion_tokens=m["completion_tokens"],
                created_at=_iso(m["created_at"]),
                feedback=m["feedback"],
                feedback_comment=m["feedback_comment"],
            )
            for m in messages
        ],
    )


@router.get("/{conversation_id}/export")
def export_conversation(
    conversation_id: str,
    user: RequireViewer,
    session: ScopedSession,
    format: Literal["txt", "json"] = Query(default="txt"),
) -> StreamingResponse:
    """Stream one conversation as a file (ADMN-07, R32).

    This is also the data-subject access export, so it carries everything held about
    the conversation — transcript, every message, every CSAT vote, the handover
    record and the visitor identifiers — not just the pretty transcript. The ``txt``
    form is the same transcript the view shows, followed by the sections the
    transcript renderer omits by design (CSAT, handover provenance).

    Streamed rather than buffered because R32 has no size ceiling and a long
    conversation would otherwise be assembled whole in memory.
    """
    cid = _uuid(conversation_id, "conversation_id")
    data = _load(session, user.tenant_id, cid)
    row, messages, files = data["row"], data["messages"], data["files"]
    filename = f"conversation-{cid}.{format}"

    if format == "json":
        payload = {
            "exported_at": datetime.now(UTC).isoformat(),
            "format_version": 2,
            "conversation": {
                "id": str(row["id"]),
                "status": row["status"],
                "bot_id": str(row["bot_id"]),
                "bot_name": row["bot_name"],
                "channel": row["channel"],
                "locale": row["locale"],
                "page_url": row["page_url"],
                "visitor_session_id": row["visitor_session_id"],
                "visitor_name": row["visitor_name"],
                "visitor_email": row["visitor_email"],
                "started_at": _iso(row["started_at"]),
                "last_activity_at": _iso(row["last_activity_at"]),
                "ended_at": _iso(row["ended_at"]),
            },
            "handover": (
                {
                    "reason": row["handover_reason"],
                    "status": row["handover_status"],
                    "notified_at": _iso(row["notified_at"]),
                }
                if row["handover_reason"] is not None
                else None
            ),
            "messages": [
                {
                    "id": str(m["id"]),
                    "role": m["role"],
                    "content": m["content"],
                    "citations": m["citations"] or [],
                    "confidence": m["confidence"],
                    "model": m["model"],
                    "latency_ms": m["latency_ms"],
                    "prompt_tokens": m["prompt_tokens"],
                    "completion_tokens": m["completion_tokens"],
                    "created_at": _iso(m["created_at"]),
                    "feedback": m["feedback"],
                    "feedback_comment": m["feedback_comment"],
                }
                for m in messages
            ],
            # Empty today for every tenant, because nothing parses a visitor upload and
            # no bytes are stored. Carried anyway: this file is the subject-access
            # answer, and "we hold no files" must be a statement the export can make
            # rather than one a reader has to infer from an absent key.
            "visitor_files": [
                {
                    "id": str(f["id"]),
                    "mime": f["mime"],
                    "size_bytes": f["size_bytes"],
                    "scan_status": f["scan_status"],
                    "created_at": _iso(f["created_at"]),
                }
                for f in files
            ],
            "transcript": _render(row, messages),
        }
        body = json.dumps(payload, indent=2, ensure_ascii=False)
        stream = _chunks([body])
        media = "application/json"
    else:
        stream = _chunks(
            [_render(row, messages), _txt_extras(row, messages, files)]
        )
        media = "text/plain; charset=utf-8"

    return StreamingResponse(
        stream,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _txt_extras(row: Any, messages: Any, files: Any) -> str:
    """The parts of the record the transcript renderer leaves out.

    Kept separate from ``compile_transcript`` so the email body stays a transcript,
    while the export stays complete for R32.
    """
    lines = ["", "=" * 60, "Export record", "=" * 60, ""]
    lines.append(f"Conversation ID: {row['id']}")
    lines.append(f"Status: {row['status']}")
    lines.append(f"Bot: {row['bot_name']} ({row['bot_id']})")
    lines.append(f"Started: {_iso(row['started_at'])}")
    lines.append(f"Last activity: {_iso(row['last_activity_at'])}")
    lines.append(f"Ended: {_iso(row['ended_at']) or '-'}")
    lines.append(f"Visitor session: {row['visitor_session_id']}")
    lines.append(f"Visitor name: {row['visitor_name'] or '-'}")
    lines.append(f"Visitor email: {row['visitor_email'] or '-'}")
    lines.append(f"Page: {row['page_url'] or '-'}")
    lines.append("")
    if row["handover_reason"] is not None:
        lines.append(f"Handover reason: {row['handover_reason']}")
        lines.append(f"Handover status: {row['handover_status']}")
        lines.append(f"Notified at: {_iso(row['notified_at']) or '-'}")
        if row["handover_status"] == "pending":
            lines.append(
                "Note: pending means either the visitor has not submitted the contact"
                " form or delivery failed; the record cannot distinguish them."
            )
    else:
        lines.append("Handover: none")
    lines.append("")
    votes = [m for m in messages if m["feedback"] is not None]
    lines.append(f"CSAT votes: {len(votes)}")
    for m in votes:
        comment = f" — {m['feedback_comment']}" if m["feedback_comment"] else ""
        lines.append(f"  [{_iso(m['created_at'])}] {m['feedback']}{comment}")
    lines.append("")
    lines.append(f"Visitor files: {len(files)}")
    for f in files:
        lines.append(
            f"  [{_iso(f['created_at'])}] {f['mime']} {f['size_bytes']} bytes"
            f" scan={f['scan_status']} id={f['id']}"
        )
    return "\n".join(lines) + "\n"


def _chunks(parts: list[str], size: int = 16 * 1024) -> Iterator[bytes]:
    for part in parts:
        for start in range(0, len(part), size):
            yield part[start : start + size].encode("utf-8")
