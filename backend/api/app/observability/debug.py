"""Owner-only conversation debugging, and the audit row that makes it accountable (OPS-11, §22).

§22's last bullet: *"Access control on observability: conversation-level debugging
requires owner role + is itself audit-logged."*

**What is gated here, and what is deliberately not.** The product already lets a
viewer read a conversation — the transcript, its citations, the visitor's CSAT vote
and the confidence the retrieval produced. That is what an owner needs to answer a
customer, and it is what the handover email and the SAR export carry (R32/SEC-10).
The facts this route adds are the ones that describe *how* an answer was produced
rather than what it said: the model that served each turn, its latency, its
prompt/completion token counts, and the tenant's usage ledger rows for the
conversation. Those are observability data, and §22 puts them behind the owner role.

The same line is why ``archive.get_conversation`` now answers ``model``,
``latency_ms`` and the token counts as ``null`` for an admin or viewer: leaving them
on the transcript view would have made this route the *second* door to the same
facts, and a control with a second door is decorative. ``confidence`` stays visible
on purpose — ``compile_transcript`` has always rendered it into the text the owner
receives by email, so gating the field would leave the same number in the body
beside it.

**The export is untouched, deliberately.** ``GET /conversations/{id}/export`` is the
data-subject access mechanism (R32). Narrowing it to protect operational data would
answer a legal request with a partial record, which is a worse defect than the one
this row fixes.

**Every successful read writes its own receipt.** The audit row is written in this
request's transaction and committed explicitly: ``tenant_session`` closes without
committing, so the obvious implementation returns 200 and records nothing. The
detail carries counts only — never content and never a visitor identifier — because
``audit_logs`` is append-only and never purged (R29), so a receipt that quoted the
transcript would deposit the conversation in the one table nothing can delete.

**A stated limit: refusals are not audited.** A non-owner is stopped by
``RequireOwner`` before this body runs, so a 403 leaves no trace. Auditing denials
needs a role-check-shaped dependency that can write, which is a wider change than
this row; what §22 asks to be recorded is the debugging access, and every granted one
is.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireOwner, ScopedSession
from app.observability import logs as obs_logs
from app.security.client_ip import client_ip

router = APIRouter(prefix="/conversations", tags=["conversations"])


class TurnDiagnostics(BaseModel):
    """Per-turn operational facts: which model, how slow, how many tokens."""

    message_id: str
    role: str
    created_at: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class UsageRow(BaseModel):
    """One ledger row for this conversation (R31's token/cost record)."""

    kind: str
    provider: str | None = None
    model: str | None = None
    units: int
    cost_micros: int
    created_at: str | None = None


class ConversationDebug(BaseModel):
    """The debugging view. No visitor identifiers, no message content.

    Both are deliberately absent: they are on the transcript route and in the export
    already, so carrying them here would only widen where a visitor's name and
    address can be read from, for no debugging benefit.
    """

    conversation_id: str
    status: str
    bot_id: str
    channel: str
    locale: str
    started_at: str | None = None
    last_activity_at: str | None = None
    ended_at: str | None = None
    message_count: int
    turns: list[TurnDiagnostics]
    usage_records: list[UsageRow]


def _uuid(value: str) -> uuid.UUID:
    """400 for a malformed id, the way every sibling route on this prefix answers.

    A different status for the same input here would make "not found" and "not
    parsed" indistinguishable for whoever is debugging a client.
    """
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "invalid_uuid", "message": "bad conversation_id"}},
        ) from None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/{conversation_id}/debug", response_model=ConversationDebug)
def debug_conversation(
    conversation_id: str,
    request: Request,
    user: RequireOwner,
    session: ScopedSession,
) -> ConversationDebug:
    """Provider/model/latency/token facts for one conversation (OPS-11).

    404 rather than 403 for another tenant's id, matching the read path: a 403 would
    confirm the id exists and turn a guessed UUID into a known one. The role check
    runs first and answers about the *caller*, which is why a viewer gets 403 before
    the tenant check can answer 404 — the same ordering the erasure route uses.

    **What actually stops the cross-tenant read is RLS, not the predicate.** Measured,
    not assumed: removing ``AND tenant_id = :tenant_id`` from the lookup below leaves
    this suite green, because ``tenant_session`` has already set ``app.tenant_id`` and
    the policy refuses the row. The predicate stays as defence in depth and for
    legibility — the same finding WP-19 and WP-23 recorded for the read and erasure
    paths, and the reason no test claims the application layer is the guard.
    """
    cid = _uuid(conversation_id)
    obs_logs.bind(conversation_id=cid)
    params = {"id": str(cid), "tenant_id": str(user.tenant_id)}

    conversation = session.execute(
        text(
            "SELECT id, status, bot_id, channel, locale, started_at,"
            " last_activity_at, ended_at FROM conversations"
            " WHERE id = :id AND tenant_id = :tenant_id"
        ),
        params,
    ).mappings().first()
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "conversation_not_found",
                    "message": "unknown conversation",
                }
            },
        )

    turns = session.execute(
        text(
            "SELECT id, role, model, latency_ms, prompt_tokens, completion_tokens,"
            " created_at FROM messages"
            " WHERE conversation_id = :id AND tenant_id = :tenant_id"
            " ORDER BY created_at, id"
        ),
        params,
    ).mappings().all()

    usage = session.execute(
        text(
            "SELECT kind, provider, model, units, cost_micros, created_at"
            " FROM usage_records"
            " WHERE conversation_id = :id AND tenant_id = :tenant_id"
            " ORDER BY created_at, id"
        ),
        params,
    ).mappings().all()

    # Counts only: `audit_logs` is append-only and never purged, so anything content-
    # or identifier-shaped written here would survive every erasure (R29, SEC-10).
    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.CONVERSATION_DEBUG_VIEWED,
        entity_type="conversation",
        entity_id=cid,
        actor_user_id=user.user_id,
        detail={"turns": len(turns), "usage_records": len(usage)},
        ip=client_ip(request),
    )
    # Without this the receipt exists only until the session closes, and the route
    # still answers 200 — `tenant_session` never commits on its own.
    session.commit()

    return ConversationDebug(
        conversation_id=str(conversation["id"]),
        status=conversation["status"],
        bot_id=str(conversation["bot_id"]),
        channel=conversation["channel"],
        locale=conversation["locale"],
        started_at=_iso(conversation["started_at"]),
        last_activity_at=_iso(conversation["last_activity_at"]),
        ended_at=_iso(conversation["ended_at"]),
        message_count=len(turns),
        turns=[
            TurnDiagnostics(
                message_id=str(t["id"]),
                role=t["role"],
                created_at=_iso(t["created_at"]),
                model=t["model"],
                latency_ms=t["latency_ms"],
                prompt_tokens=t["prompt_tokens"],
                completion_tokens=t["completion_tokens"],
            )
            for t in turns
        ],
        usage_records=[
            UsageRow(
                kind=u["kind"],
                provider=u["provider"],
                model=u["model"],
                units=u["units"],
                cost_micros=u["cost_micros"],
                created_at=_iso(u["created_at"]),
            )
            for u in usage
        ],
    )
