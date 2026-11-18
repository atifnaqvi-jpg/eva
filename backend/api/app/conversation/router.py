"""Conversation HTTP surface: start a conversation, stream a turn (CONV-04, CONV-09).

**Scope note.** ARCHITECTURE §17.4 puts these operations under
``/api/v1/widget/...`` and secures them with a widget session token plus an
``Origin``-to-``bot_domains`` check. That token and the domain verification are
CONV's WP-16 work. Exposing an unauthenticated SSE endpoint before it exists would
be a public hole with a support bot on the other end, so the same turn loop is
served here behind the owner session cookie instead. WP-16 adds the widget route
as a thin adapter over ``service.turn_events``; the event contract below is the
one it will emit, unchanged.

The event sequence is the §17.4 contract:

    event: token
    data: {"text": "Your order usually ships..."}

    event: final
    data: {"message_id": "...", "citations": [...], "confidence": 0.82, "usage": {}}

``token`` events are additive and a client must tolerate any number of them
including zero; ``final`` always comes last on a successful turn. A turn that
cannot run at all (unknown conversation, wrong tenant, conversation no longer
active) emits one ``error`` event instead — never a non-SSE body, because the
client is already parsing this response as an event stream.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth.dependencies import RequireAdmin, ScopedSession
from app.conversation import controls, service
from app.conversation import suggestions as suggestion_engine
from app.conversation.llm import default_provider
from app.observability import metrics as obs_metrics
from app.retrieval.embedding import default_embedder
from app.security.client_ip import client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


class StartConversationRequest(BaseModel):
    bot_id: str
    visitor_session_id: str = Field(min_length=1, max_length=200)
    page_url: str | None = Field(default=None, max_length=2000)


class StartConversationResponse(BaseModel):
    conversation_id: str
    status: str


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class BotConfigResponse(BaseModel):
    """The session config from §17.4, minus the widget-only fields."""

    name: str
    welcome: str | None = None
    suggestions: list[str]
    powered_by_visible: bool
    confidence_threshold: float


class FeedbackRequest(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    id: str
    message_id: str
    rating: str


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "invalid_uuid", "message": f"bad {field}"}},
        ) from None


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@router.get("/bots/{bot_id}/config", response_model=BotConfigResponse)
def bot_config(
    bot_id: str, user: RequireAdmin, session: ScopedSession
) -> BotConfigResponse:
    """Proactive suggestion chips for the panel (CONV-06).

    Served from the bot row, not generated: these are the owner's curated chips and
    the visitor sees them before typing anything.
    """
    row = session.execute(
        text(
            "SELECT name, welcome_message, suggestion_settings, powered_by_visible,"
            " confidence_threshold FROM bots"
            " WHERE id = :bot_id AND tenant_id = :tenant_id"
        ),
        {"bot_id": str(_uuid(bot_id, "bot_id")), "tenant_id": str(user.tenant_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "bot_not_found", "message": "unknown bot"}},
        )
    return BotConfigResponse(
        name=row["name"],
        welcome=row["welcome_message"],
        suggestions=suggestion_engine.settings(row["suggestion_settings"])["proactive"],
        powered_by_visible=row["powered_by_visible"],
        confidence_threshold=row["confidence_threshold"],
    )


@router.post("", response_model=StartConversationResponse, status_code=201)
def start_conversation(
    user: RequireAdmin, session: ScopedSession, body: StartConversationRequest
) -> StartConversationResponse:
    """Open a conversation for a bot (CONV-09: `active`, with `last_activity_at`)."""
    bot_id = _uuid(body.bot_id, "bot_id")
    row = session.execute(
        text("SELECT id FROM bots WHERE id = :bot_id AND tenant_id = :tenant_id"),
        {"bot_id": str(bot_id), "tenant_id": str(user.tenant_id)},
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "bot_not_found", "message": "unknown bot"}},
        )
    conversation_id = session.execute(
        text(
            """
            INSERT INTO conversations (tenant_id, bot_id, visitor_session_id, page_url)
            VALUES (:tenant_id, :bot_id, :visitor_session_id, :page_url)
            RETURNING id, status
            """
        ),
        {
            "tenant_id": str(user.tenant_id),
            "bot_id": str(bot_id),
            "visitor_session_id": body.visitor_session_id,
            "page_url": body.page_url,
        },
    ).one()
    session.commit()
    return StartConversationResponse(
        conversation_id=str(conversation_id.id), status=conversation_id.status
    )


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    user: RequireAdmin,
    session: ScopedSession,
    body: MessageRequest,
    request: Request,
) -> StreamingResponse:
    """Run one turn and stream it as SSE.

    The DB session is used synchronously inside an async generator, which blocks
    the event loop for the duration of each query. That is acceptable here —
    retrieval is a couple of indexed queries — and the alternative (a second
    connection per stream) would mean a second RLS context for one turn. WP-13
    revisits it when provider latency starts to dominate.
    """
    cid = _uuid(conversation_id, "conversation_id")

    # R36. The visitor key is the conversation's own visitor session, not the IP:
    # the widget is embedded on a tenant's site, so an IP is frequently shared by
    # many legitimate visitors behind one NAT.
    visitor_session_id = session.execute(
        text(
            "SELECT visitor_session_id FROM conversations"
            " WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": str(cid), "tenant_id": str(user.tenant_id)},
    ).scalar()
    if visitor_session_id is not None:
        try:
            controls.limiter().check(
                tenant_id=user.tenant_id,
                visitor_session_id=visitor_session_id,
                ip=client_ip(request),
            )
        except controls.RateLimited as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": {"code": exc.code, "message": "slow down"}},
                headers={"Retry-After": str(exc.retry_after)},
            ) from None

    embedder = default_embedder()
    provider = default_provider()

    async def events() -> AsyncIterator[str]:
        try:
            # OPS-03. Counted here as well as in the widget route because both are
            # live SSE surfaces; a metric that covered only one would report half the
            # streams as missing. `error` deliberately does not set `completed`: the
            # visitor got a 200 and then a failure, which is the abort case §22's
            # stream-health signal exists to distinguish.
            with obs_metrics.sse_stream() as stream_state:
                async for event, payload in service.turn_events(
                    session,
                    tenant_id=user.tenant_id,
                    conversation_id=cid,
                    question=body.text,
                    embedder=embedder,
                    provider=provider,
                ):
                    if event == service.EVENT_FINAL:
                        stream_state["completed"] = True
                    yield _sse(event, payload)
        except Exception:  # noqa: BLE001 - the stream is already open
            # Once the 200 and the headers are out, an exception would truncate the
            # body and leave the client hanging on a half-parsed event. Convert it
            # into a final event so the widget can show its offline state.
            logger.exception("turn failed mid-stream (conversation=%s)", cid)
            yield _sse("error", {"code": "turn_failed"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/messages/{message_id}/feedback",
    response_model=FeedbackResponse,
    status_code=201,
)
def submit_feedback(
    message_id: str,
    user: RequireAdmin,
    session: ScopedSession,
    body: FeedbackRequest,
) -> FeedbackResponse:
    """CSAT thumbs up/down for one assistant answer (CONV-08, R40).

    The message must belong to this tenant *and* be an assistant message. Rating a
    visitor's own turn is meaningless, and rating another tenant's answer must be
    impossible even if the id is known — the tenant check is explicit rather than
    left to RLS alone, because RLS would happily allow the insert if the id leaked
    and the tenant matched.

    Re-rating replaces the previous vote: a visitor who clicks thumbs-down by
    accident should be able to correct it, and two rows per message would make
    every CSAT average wrong.
    """
    mid = _uuid(message_id, "message_id")
    row = session.execute(
        text(
            "SELECT id, role FROM messages"
            " WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": str(mid), "tenant_id": str(user.tenant_id)},
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "message_not_found", "message": "unknown message"}},
        )
    if row["role"] != "assistant":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "not_an_answer",
                    "message": "only assistant messages can be rated",
                }
            },
        )

    session.execute(
        text("DELETE FROM feedback WHERE message_id = :id AND tenant_id = :tenant_id"),
        {"id": str(mid), "tenant_id": str(user.tenant_id)},
    )
    feedback_id = session.execute(
        text(
            """
            INSERT INTO feedback (tenant_id, message_id, rating, comment)
            VALUES (:tenant_id, :message_id, :rating, :comment)
            RETURNING id
            """
        ),
        {
            "tenant_id": str(user.tenant_id),
            "message_id": str(mid),
            "rating": body.rating,
            "comment": body.comment,
        },
    ).scalar_one()
    session.commit()
    return FeedbackResponse(
        id=str(feedback_id), message_id=str(mid), rating=body.rating
    )
