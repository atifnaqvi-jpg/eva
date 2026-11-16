"""Public widget routes (WIDG-05, §12.3, §17.4).

These are the only unauthenticated endpoints that touch a tenant's knowledge base,
so three things are true of every one of them:

* **The tenant comes from the token, never from the request body.** §12.3 is
  explicit; a `tenant_id` field on a public endpoint is a cross-tenant read waiting
  to happen, and it would also defeat RLS because the app would be setting the GUC
  from client input.
* **The bot in the token must still be the bot named in the path.** A token minted
  for bot A must not open a conversation with bot B in the same tenant, or one
  tenant's low-sensitivity bot becomes a proxy for its high-sensitivity one.
* **Origin is re-checked on use, not just on mint.** A token is a bearer credential;
  binding it to an origin is only useful if the binding is verified where the token
  is spent.

Consent (§12.2 / R41) is enforced *client*-side by the loader — nothing is stored or
loaded before the visitor clicks — and that is where the browser-storage audit looks
for it (WIDG-04, SEC-08). Server-side, the relevant guarantee is that a conversation
row is only created when the visitor acts: `POST /session` is the act of opening the
chat, so it creates nothing, and `POST /conversations` is the first message.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.conversation import controls, service
from app.conversation import suggestions as suggestion_engine
from app.conversation.llm import default_provider
from app.conversation.router import _sse
from app.db import session as db_session
from app.retrieval.embedding import default_embedder
from app.security.client_ip import client_ip
from app import config
from app.config import VISITOR_MAX_UPLOAD_BYTES
from app.handover import channels
from app.handover import policy as handover_policy
from app.handover import webhooks
from app.widget import appearance as appearance_settings
from app.widget import tokens, uploads

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/widget", tags=["widget"])


class SessionRequest(BaseModel):
    bot_public_id: str = Field(min_length=1, max_length=200)


class SessionResponse(BaseModel):
    token: str
    expires_in: int
    bot: dict
    powered_by_visible: bool
    # R43. The widget needs this *before* the visitor types: outside business hours
    # it leads with the offline banner and the contact form instead of inviting a
    # question nobody will answer (§18.3).
    online: bool
    next_open: str | None = None


class StartRequest(BaseModel):
    # No tenant_id and no bot_id: both come from the token. See the module docstring.
    visitor_session_id: str = Field(min_length=1, max_length=200)
    page_url: str | None = Field(default=None, max_length=2000)


class StartResponse(BaseModel):
    conversation_id: str
    status: str


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class HandoverRequest(BaseModel):
    """Contact form (§13.2, HAND-02).

    Email is required because the whole point of the form is to give the owner a way
    to reply; a name is optional because demanding one loses handovers for no
    benefit. Length caps are enforced here rather than in the handler so a huge body
    is rejected before it reaches the transcript compiler.
    """

    email: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class HandoverResponse(BaseModel):
    handover_id: str
    status: str
    reason: str


def _next_open_label(tenant_settings) -> str | None:
    """A short "when we're back" string for the offline banner.

    Empty when there is no schedule, because "opens at None" is worse than silence:
    the widget shows the offline message without a time in that case.
    """
    moment = handover_policy.next_open(tenant_settings)
    if moment is None:
        return None
    return moment.strftime("%a %d %b %H:%M %Z").strip()


def _deny(code: str, message: str, status_code: int = status.HTTP_403_FORBIDDEN) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"error": {"code": code, "message": message}}
    )


def _bot_for_public_id(bot_public_id: str) -> dict:
    """Resolve a public bot id across tenants.

    This is the one lookup that legitimately runs without a tenant context: the
    public id *is* the widget's entry point, and §12.3 treats it as non-secret. It
    goes through a `SECURITY DEFINER` function rather than an owner connection so
    the privileged surface is one fixed statement (migration 0006), and it returns
    only presentation data for one bot.
    """
    # Accessed through the module, not bound at import: the test harness reloads
    # `app.db.session` to repoint the engine at the migrated test database, and a
    # from-import would keep the stale engine and its old credentials.
    session = db_session.SessionLocal()
    try:
        row = session.execute(
            text(
                "SELECT id, tenant_id, name, welcome_message, appearance,"
                " suggestion_settings, powered_by_visible"
                " FROM widget_bot_by_public_id(:public_id)"
            ),
            {"public_id": bot_public_id},
        ).mappings().first()
    finally:
        session.close()
    if row is None:
        # A wrong public id and a disabled bot are the same answer: telling the
        # difference would confirm which ids exist.
        raise _deny("bot_not_found", "unknown widget", status.HTTP_404_NOT_FOUND)
    return dict(row)


def _allowed_domains(session, *, tenant_id: uuid.UUID, bot_id: uuid.UUID) -> list[str]:
    """Only **verified** domains count, per §16.1 ("widget only serves on verified
    domains") and the §27 launch checklist ("widget refuses unverified origins").

    An unverified row is an owner's claim, not a proven control: without this filter
    anyone could register a customer's domain and serve the widget there, which is
    exactly the confusion the verification token exists to prevent.
    """
    return list(
        session.execute(
            text(
                "SELECT domain FROM bot_domains"
                " WHERE tenant_id = :tenant_id AND bot_id = :bot_id"
                " AND verified IS TRUE"
            ),
            {"tenant_id": str(tenant_id), "bot_id": str(bot_id)},
        ).scalars()
    )


def _rate_limit(token: tokens.WidgetToken, request: Request, visitor_session_id: str) -> None:
    """Apply the widget rate limits, or raise the ``RateLimited`` HTTP error.

    One helper for every widget route: a limit that each handler re-derives is a
    limit that the next handler gets wrong — which is exactly how the upload route
    shipped unlimited the first time.
    """
    try:
        controls.limiter().check(
            tenant_id=token.tenant_id,
            visitor_session_id=visitor_session_id,
            ip=client_ip(request),
        )
    except controls.RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"code": exc.code, "message": "slow down"}},
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def _verify_token(authorization: str | None) -> tokens.WidgetToken:
    """Validate the bearer token's signature and expiry. No database, no origin."""
    if not tokens.configured():
        # Fail closed. An unset secret must not mean "no checking".
        raise _deny(
            "widget_unavailable",
            "widget is not configured",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _deny("missing_token", "session token required", status.HTTP_401_UNAUTHORIZED)
    try:
        token = tokens.verify(authorization.split(" ", 1)[1].strip())
    except tokens.TokenError:
        # One message for every failure: expired, forged and malformed are the same
        # answer to the client, and distinguishing them helps only a prober.
        raise _deny("invalid_token", "session expired, please reload", status.HTTP_401_UNAUTHORIZED)

    return token


def _check_origin(token: tokens.WidgetToken, request: Request) -> None:
    """Re-check the request origin against the token's bot domains, on use.

    Separate from signature verification so the routes can resolve a conversation
    first and return a precise `token_bot_mismatch` rather than letting the origin
    check answer for a bot that has no domains configured.
    """
    origin = request.headers.get("origin")
    with db_session.tenant_session(token.tenant_id) as session:
        allowed = _allowed_domains(session, tenant_id=token.tenant_id, bot_id=token.bot_id)
    if not allowed:
        # No domains configured means the widget cannot be safely embedded anywhere.
        # Denying is the conservative reading: an owner who has not finished setup
        # should not have a live widget serving their knowledge.
        raise _deny(
            "no_allowed_domains",
            "this widget has no allowed domains configured",
            status.HTTP_403_FORBIDDEN,
        )
    if not origin or not tokens.origin_matches(origin, allowed):
        raise _deny("origin_not_allowed", "this domain is not allowed to use this widget")


def _conversation_for(
    token: tokens.WidgetToken, request: Request, conversation_id: str
) -> dict:
    """Resolve a conversation that belongs to this token's tenant *and* bot.

    Shared by every conversation-scoped route. The bot check is not redundant with
    the tenant check: a token minted for a public support bot must not reach a
    conversation held with the same tenant's internal HR bot.
    """
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise _deny("invalid_uuid", "bad conversation_id", status.HTTP_400_BAD_REQUEST) from None

    with db_session.tenant_session(token.tenant_id) as session:
        row = session.execute(
            text(
                "SELECT id, bot_id, visitor_session_id, status, visitor_email"
                " FROM conversations WHERE id = :id AND tenant_id = :tenant_id"
            ),
            {"id": str(cid), "tenant_id": str(token.tenant_id)},
        ).mappings().first()
    if row is None:
        raise _deny("conversation_not_found", "unknown conversation", status.HTTP_404_NOT_FOUND)
    if str(row["bot_id"]) != str(token.bot_id):
        raise _deny("token_bot_mismatch", "token does not match this conversation")
    _check_origin(token, request)
    return dict(row)


@router.post("/session", response_model=SessionResponse)
def create_session(
    request: Request, body: SessionRequest
) -> SessionResponse:
    """Mint a widget session token for a bot on an allowed origin (§12.3).

    Creates nothing: no conversation, no row. The loader calls this when it needs a
    token, and a pageview that never opens the chat leaves no trace in the database.
    """
    if not tokens.configured():
        raise _deny(
            "widget_unavailable",
            "widget is not configured",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    origin = request.headers.get("origin")
    bot = _bot_for_public_id(body.bot_public_id)

    with db_session.tenant_session(bot["tenant_id"]) as session:
        allowed = _allowed_domains(
            session, tenant_id=bot["tenant_id"], bot_id=bot["id"]
        )
    if not allowed:
        raise _deny(
            "no_allowed_domains",
            "this widget has no allowed domains configured",
        )
    if not origin or not tokens.origin_matches(origin, allowed):
        raise _deny("origin_not_allowed", "this domain is not allowed to use this widget")

    with db_session.tenant_session(bot["tenant_id"]) as session:
        tenant_settings = session.execute(
            text("SELECT settings FROM tenants WHERE id = :id"),
            {"id": str(bot["tenant_id"])},
        ).scalar()

    token = tokens.issue(
        tenant_id=bot["tenant_id"], bot_id=bot["id"], origin=origin
    )
    return SessionResponse(
        token=token,
        expires_in=tokens.WIDGET_TOKEN_TTL_SECONDS,
        bot={
            "name": bot["name"],
            "welcome": bot["welcome_message"],
            "suggestions": suggestion_engine.settings(bot["suggestion_settings"])[
                "proactive"
            ],
            # Validated, not passed through: this JSONB renders inside the host
            # page, so only known keys with checked values leave the server.
            "appearance": appearance_settings.normalise(bot["appearance"]),
            "public_id": body.bot_public_id,
        },
        powered_by_visible=bot["powered_by_visible"],
        online=handover_policy.is_online(tenant_settings),
        next_open=_next_open_label(tenant_settings),
    )


@router.post("/conversations", response_model=StartResponse, status_code=201)
def start_conversation(
    request: Request,
    body: StartRequest,
    authorization: str | None = Header(default=None),
) -> StartResponse:
    """Open a conversation. The visitor's first act, so this is where a row appears."""
    token = _verify_token(authorization)
    _check_origin(token, request)
    with db_session.tenant_session(token.tenant_id) as session:
        row = session.execute(
            text(
                """
                INSERT INTO conversations (tenant_id, bot_id, visitor_session_id, page_url)
                VALUES (:tenant_id, :bot_id, :visitor_session_id, :page_url)
                RETURNING id, status
                """
            ),
            {
                "tenant_id": str(token.tenant_id),
                "bot_id": str(token.bot_id),
                "visitor_session_id": body.visitor_session_id,
                "page_url": body.page_url,
            },
        ).one()
        session.commit()
    # After commit, so a receiver that reacts by fetching the conversation finds it.
    webhooks.emit_async(
        tenant_id=token.tenant_id,
        event="conversation.created",
        payload={
            "event": "conversation.created",
            "conversation_id": str(row.id),
            "status": row.status,
        },
        allow_private=config.WEBHOOK_ALLOW_PRIVATE_HOSTS,
    )
    return StartResponse(conversation_id=str(row.id), status=row.status)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    request: Request,
    body: MessageRequest,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """Stream one turn over the widget path (§17.4).

    A thin adapter over ``service.turn_events``: the same turn loop, the same event
    contract as the owner route, so there is one implementation of the answer path
    and no chance of the two drifting.
    """
    token = _verify_token(authorization)
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise _deny("invalid_uuid", "bad conversation_id", status.HTTP_400_BAD_REQUEST) from None

    with db_session.tenant_session(token.tenant_id) as session:
        row = session.execute(
            text(
                "SELECT bot_id, visitor_session_id FROM conversations"
                " WHERE id = :id AND tenant_id = :tenant_id"
            ),
            {"id": str(cid), "tenant_id": str(token.tenant_id)},
        ).mappings().first()
    if row is None:
        raise _deny("conversation_not_found", "unknown conversation", status.HTTP_404_NOT_FOUND)
    if str(row["bot_id"]) != str(token.bot_id):
        raise _deny("token_bot_mismatch", "token does not match this conversation")
    _check_origin(token, request)

    try:
        controls.limiter().check(
            tenant_id=token.tenant_id,
            visitor_session_id=row["visitor_session_id"],
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

    async def event_stream() -> AsyncIterator[str]:
        with db_session.tenant_session(token.tenant_id) as session:
            async for event, payload in service.turn_events(
                session,
                tenant_id=token.tenant_id,
                conversation_id=cid,
                question=body.text,
                embedder=embedder,
                provider=provider,
            ):
                yield _sse(event, payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/messages/after-handover", status_code=201)
def append_after_handover(
    conversation_id: str,
    request: Request,
    body: MessageRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """Store a visitor message once the AI has stepped aside (HAND-05, §13.3).

    After handover the visitor can still type: they may add detail, or answer a
    question the human will ask. Those messages must reach the owner, so they are
    appended to the transcript. But the AI must not answer, so this route writes the
    message and returns — it does not emit tokens.

    A distinct path rather than a mode on ``/messages``: the widget has to know
    before it sends whether to expect a stream, and a route that sometimes streams
    and sometimes does not would make every caller handle both.
    """
    token = _verify_token(authorization)
    conversation = _conversation_for(token, request, conversation_id)
    _rate_limit(token, request, conversation["visitor_session_id"])

    if conversation["status"] == "closed":
        raise _deny("conversation_closed", "this conversation has ended")

    with db_session.tenant_session(token.tenant_id) as session:
        message_id = session.execute(
            text(
                "INSERT INTO messages (tenant_id, conversation_id, role, content)"
                " VALUES (:tenant_id, :conversation_id, 'visitor', :content)"
                " RETURNING id"
            ),
            {
                "tenant_id": str(token.tenant_id),
                "conversation_id": str(conversation["id"]),
                "content": body.text,
            },
        ).scalar_one()
        session.execute(
            text(
                "UPDATE conversations SET last_activity_at = now()"
                " WHERE tenant_id = :tenant_id AND id = :id"
            ),
            {"tenant_id": str(token.tenant_id), "id": str(conversation["id"])},
        )
        session.commit()

    return {
        "message_id": str(message_id),
        "stored": True,
        "ai_will_answer": False,
        "conversation_status": conversation["status"],
    }


@router.post(
    "/conversations/{conversation_id}/handover",
    response_model=HandoverResponse,
    status_code=201,
)
def request_handover(
    conversation_id: str,
    request: Request,
    body: HandoverRequest,
    authorization: str | None = Header(default=None),
) -> HandoverResponse:
    """Visitor asks for a human, with a way to reach them (HAND-02, §13.2, R19).

    Repeating the call is safe and returns the same handover: the widget's contact
    form can be submitted twice by an impatient visitor, and a second email about the
    same conversation helps nobody. `open_handover` is what makes that true.
    """
    from app.handover import service as handover_service

    token = _verify_token(authorization)
    conversation = _conversation_for(token, request, conversation_id)
    _rate_limit(token, request, conversation["visitor_session_id"])

    email = _valid_email(body.email)
    if email is None:
        raise _deny(
            "invalid_email",
            "a valid email address is required",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    with db_session.tenant_session(token.tenant_id) as session:
        handover_id = handover_service.open_handover(
            session,
            tenant_id=token.tenant_id,
            conversation_id=conversation["id"],
            reason="explicit",
            channel="email",
            visitor_name=(body.name or "").strip() or None,
            visitor_email=email,
        )
        if body.note:
            # Kept as a real message so the transcript the owner receives includes
            # what the visitor chose to add, rather than a field nobody reads.
            session.execute(
                text(
                    "INSERT INTO messages (tenant_id, conversation_id, role, content)"
                    " VALUES (:tenant_id, :conversation_id, 'visitor', :content)"
                ),
                {
                    "tenant_id": str(token.tenant_id),
                    "conversation_id": str(conversation["id"]),
                    "content": body.note.strip()[:2000],
                },
            )
            session.commit()

        _notify(
            session,
            tenant_id=token.tenant_id,
            conversation_id=conversation["id"],
            handover_id=handover_id,
            reason="explicit",
        )

    return HandoverResponse(
        handover_id=str(handover_id), status="handed_over", reason="explicit"
    )


def _notify(
    session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    handover_id: uuid.UUID,
    reason: str,
) -> None:
    """Compile and deliver, without letting delivery failure fail the request.

    The visitor has already been told a human is coming; if SMTP is down they should
    still get that answer, and the failure belongs on the handover row. A background
    thread is used rather than a queue because the delivery is one SMTP call and the
    session must not outlive the request that opened it.
    """
    from app.handover import service as handover_service

    def _work() -> None:
        # A fresh session: the request's session is closed once the response is
        # returned, and holding it open across an SMTP timeout would pin a
        # connection for the duration.
        with db_session.tenant_session(tenant_id) as own:
            try:
                handover_service.compile_and_deliver(
                    own,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    handover_id=handover_id,
                    reason=reason,
                    provider=default_provider(),
                    channel=channels.build_channel(),
                )
            except Exception:  # noqa: BLE001 - never surface to the visitor
                logger.exception("handover notification failed")

    if config.HANDOVER_NOTIFY_ASYNC:
        threading.Thread(target=_work, name="handover-notify", daemon=True).start()
    else:
        _work()


def _valid_email(value: str) -> str | None:
    """Minimal syntactic check, no MX lookup.

    Deliberately not a full RFC 5322 parser: this address is used for a human reply,
    not for authentication, and a stricter validator would reject real addresses
    (plus-addressing, unusual TLDs) for no security gain. The transcript is what
    carries the PII, and it is sent to the *tenant*, not to this address.
    """
    candidate = (value or "").strip()
    if len(candidate) > 320 or "@" not in candidate:
        return None
    local, _, domain = candidate.rpartition("@")
    if not local or not domain or "." not in domain:
        return None
    if any(ch.isspace() for ch in candidate):
        return None
    return candidate


@router.post("/messages/{message_id}/feedback", status_code=201)
def submit_feedback(
    message_id: str,
    request: Request,
    body: dict,
    authorization: str | None = Header(default=None),
) -> dict:
    """CSAT for one answer on the widget path (WIDG-07, R40)."""
    from app.conversation.router import FeedbackRequest

    token = _verify_token(authorization)
    _check_origin(token, request)
    try:
        parsed = FeedbackRequest(**body)
    except Exception:  # noqa: BLE001 - pydantic's message is not the API contract
        raise _deny(
            "invalid_rating", "rating must be 'up' or 'down'", status.HTTP_422_UNPROCESSABLE_CONTENT
        ) from None
    try:
        mid = uuid.UUID(message_id)
    except ValueError:
        raise _deny("invalid_uuid", "bad message_id", status.HTTP_400_BAD_REQUEST) from None

    with db_session.tenant_session(token.tenant_id) as session:
        row = session.execute(
            text(
                "SELECT m.id, m.role FROM messages m"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE m.id = :id AND m.tenant_id = :tenant_id"
                " AND c.bot_id = :bot_id"
            ),
            {
                "id": str(mid),
                "tenant_id": str(token.tenant_id),
                "bot_id": str(token.bot_id),
            },
        ).mappings().first()
        if row is None:
            raise _deny("message_not_found", "unknown message", status.HTTP_404_NOT_FOUND)
        if row["role"] != "assistant":
            raise _deny(
                "not_an_answer",
                "only assistant messages can be rated",
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        session.execute(
            text("DELETE FROM feedback WHERE message_id = :id AND tenant_id = :tenant_id"),
            {"id": str(mid), "tenant_id": str(token.tenant_id)},
        )
        feedback_id = session.execute(
            text(
                "INSERT INTO feedback (tenant_id, message_id, rating, comment)"
                " VALUES (:tenant_id, :message_id, :rating, :comment) RETURNING id"
            ),
            {
                "tenant_id": str(token.tenant_id),
                "message_id": str(mid),
                "rating": parsed.rating,
                "comment": parsed.comment,
            },
        ).scalar_one()

        # HAND-01's third trigger. A thumbs-down is the visitor saying the answer
        # was wrong, which is exactly the signal an owner wants; whether it opens a
        # handover is the tenant's choice, because a noisy channel gets ignored.
        handover_payload = None
        if parsed.rating == "down":
            from app.handover import policy as handover_policy
            from app.handover import service as handover_service

            owner = session.execute(
                text(
                    "SELECT t.settings, c.id AS conversation_id,"
                    "       c.visitor_email, c.status"
                    "  FROM conversations c"
                    "  JOIN tenants t ON t.id = c.tenant_id"
                    " WHERE c.tenant_id = :tenant_id AND c.id ="
                    "       (SELECT conversation_id FROM messages WHERE id = :mid)"
                ),
                {"tenant_id": str(token.tenant_id), "mid": str(mid)},
            ).mappings().first()
            if owner is not None and handover_policy.settings(owner["settings"])[
                "notify_on_negative_feedback"
            ]:
                hid = handover_service.open_handover(
                    session,
                    tenant_id=token.tenant_id,
                    conversation_id=owner["conversation_id"],
                    reason="negative_feedback",
                    channel="email",
                )
                handover_payload = {
                    "id": str(hid),
                    "reason": "negative_feedback",
                    # No email on the conversation means the widget must ask for one,
                    # otherwise the notification has nobody to reply to.
                    "requires_contact": not owner["visitor_email"],
                }
                _notify(
                    session,
                    tenant_id=token.tenant_id,
                    conversation_id=owner["conversation_id"],
                    handover_id=hid,
                    reason="negative_feedback",
                )
        session.commit()
    return {
        "id": str(feedback_id),
        "message_id": str(mid),
        "rating": parsed.rating,
        "handover": handover_payload,
    }


@router.post("/conversations/{conversation_id}/files", status_code=201)
async def upload_file(
    conversation_id: str,
    request: Request,
    filename: str = "",
    authorization: str | None = Header(default=None),
) -> dict:
    """Attach an image or PDF to a conversation (WIDG-14, R45).

    The body is raw bytes with the name in ``?filename=``; multipart parsing needs
    `python-multipart`, which is not an approved dependency, and the bytes are the
    same either way. The cap is enforced *while* streaming so an oversized body is
    never fully buffered.
    """
    token = _verify_token(authorization)
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        raise _deny("invalid_uuid", "bad conversation_id", status.HTTP_400_BAD_REQUEST) from None

    with db_session.tenant_session(token.tenant_id) as session:
        row = session.execute(
            text(
                "SELECT bot_id, visitor_session_id FROM conversations"
                " WHERE id = :id AND tenant_id = :tenant_id"
            ),
            {"id": str(cid), "tenant_id": str(token.tenant_id)},
        ).mappings().first()
    if row is None or str(row["bot_id"]) != str(token.bot_id):
        raise _deny("conversation_not_found", "unknown conversation", status.HTTP_404_NOT_FOUND)
    _check_origin(token, request)

    # Limiter is checked *before* the body is read: an unauthenticated-ish endpoint
    # that buffers 5 MB before deciding to refuse is a memory amplifier as well as a
    # storage one. Same visitor budget as a turn, deliberately — it is one abuse
    # budget per visitor, not one per endpoint.
    _rate_limit(token, request, row["visitor_session_id"])

    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > VISITOR_MAX_UPLOAD_BYTES:
            raise _deny(
                "file_too_large",
                f"the limit is {VISITOR_MAX_UPLOAD_BYTES} bytes",
                status.HTTP_413_CONTENT_TOO_LARGE,
            )
    try:
        mime = uploads.validate(bytes(payload), max_bytes=VISITOR_MAX_UPLOAD_BYTES)
    except uploads.UploadRejected as exc:
        if exc.code == "unsupported_format":
            logger.warning(
                "visitor upload rejected as unsupported: tenant=%s conversation=%s",
                token.tenant_id,
                cid,
            )
        raise _deny(exc.code, exc.message, exc.status_code) from None

    with db_session.tenant_session(token.tenant_id) as session:
        file_id = session.execute(
            text(
                "INSERT INTO visitor_files"
                " (tenant_id, conversation_id, storage_path, mime, size_bytes)"
                " VALUES (:tenant_id, :conversation_id, :storage_path, :mime, :size)"
                " RETURNING id"
            ),
            {
                "tenant_id": str(token.tenant_id),
                "conversation_id": str(cid),
                "storage_path": uploads.storage_key(
                    tenant_id=token.tenant_id, conversation_id=cid
                ),
                "mime": mime,
                "size": len(payload),
            },
        ).scalar_one()
        session.commit()
    return {
        "id": str(file_id),
        "mime": mime,
        "size_bytes": len(payload),
        "name": filename[:255],
    }
