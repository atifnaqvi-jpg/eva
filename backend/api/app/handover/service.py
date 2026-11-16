"""Handover: decide, record, notify (§13, HAND-01/02/04/05).

The flow the blueprint specifies is *trigger → contact form → lock to AI →
compile transcript + summary → email → optional webhook*. This module owns the
middle: deciding whether a trigger fired, writing the ``handovers`` row and locking
the conversation, then compiling and delivering.

**Ordering matters and is deliberate.** The row and the conversation lock are
committed *before* delivery is attempted. If SMTP hangs or fails, the visitor still
gets a consistent "a human will follow up" and the owner still sees the handover;
the alternative (notify, then commit) loses the record exactly when the thing that
records it is broken. Delivery outcome is written back onto the row afterwards.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.handover import channels as channel_mod
from app.handover import policy, transcript
from app import config
from app.conversation import llm

logger = logging.getLogger(__name__)

# Mirrors the `ck_handovers_reason` check constraint. Duplicated on purpose: a typo
# here should fail in Python with a clear message, not as an IntegrityError.
REASONS = ("explicit", "low_confidence", "negative_feedback", "rule")


@dataclass(frozen=True)
class Trigger:
    """Why a handover is happening, or that it is not."""

    reason: str
    detail: str = ""


def evaluate_turn_triggers(
    *,
    question: str,
    tenant_settings: Any,
    declined: bool,
    decline_reason: str | None,
) -> Trigger | None:
    """Turn-time triggers: keyword rule and low confidence (§13.1).

    Explicit requests and negative feedback arrive through their own endpoints, where
    they have a contact form to go with them; these two are noticed mid-conversation
    and escalate without asking the visitor anything first.
    """
    handover_settings = policy.settings(tenant_settings)

    # A keyword match wins over low confidence: the visitor asked for a person, and
    # "declined because retrieval was weak" would mislabel the reason they see.
    if policy.keyword_match(question, handover_settings["keywords"]):
        return Trigger(reason="rule", detail="keyword")

    if (
        declined
        and decline_reason == "below_confidence_floor"
        and handover_settings["notify_on_low_confidence"]
    ):
        return Trigger(reason="low_confidence", detail=decline_reason or "")

    return None


def open_handover(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reason: str,
    channel: str,
    visitor_name: str | None = None,
    visitor_email: str | None = None,
) -> uuid.UUID:
    """Record a handover and lock the conversation to the AI.

    Idempotent per conversation: a second trigger returns the existing row instead of
    opening another one. A visitor who says "human" twice, or whose message both
    matches a keyword and contains a request, must not produce two emails — and
    ``handovers`` has no unique constraint to catch that for us.
    """
    if reason not in REASONS:
        raise ValueError(f"unknown handover reason: {reason!r}")

    existing = session.execute(
        text(
            "SELECT id FROM handovers"
            " WHERE tenant_id = :tenant_id AND conversation_id = :conversation_id"
            " ORDER BY id LIMIT 1"
        ),
        {"tenant_id": str(tenant_id), "conversation_id": str(conversation_id)},
    ).scalar()
    if existing is not None:
        return existing

    handover_id = session.execute(
        text(
            "INSERT INTO handovers (tenant_id, conversation_id, reason, channel, status)"
            " VALUES (:tenant_id, :conversation_id, :reason, :channel, 'pending')"
            " RETURNING id"
        ),
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "reason": reason,
            "channel": channel,
        },
    ).scalar_one()

    # The lock HAND-05 depends on: `conversation.turn` refuses a non-active
    # conversation, so this one UPDATE is what stops the AI answering.
    session.execute(
        text(
            "UPDATE conversations SET status = 'handed_over'"
            " WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"tenant_id": str(tenant_id), "id": str(conversation_id)},
    )
    # Contact details live on the conversation, not the handover: they belong to the
    # visitor's session and the dashboard shows them there (§16.1).
    if visitor_name or visitor_email:
        session.execute(
            text(
                "UPDATE conversations"
                " SET visitor_name = COALESCE(:name, visitor_name),"
                "     visitor_email = COALESCE(:email, visitor_email)"
                " WHERE tenant_id = :tenant_id AND id = :id"
            ),
            {
                "name": visitor_name,
                "email": visitor_email,
                "tenant_id": str(tenant_id),
                "id": str(conversation_id),
            },
        )
    session.commit()
    return handover_id


def compile_and_deliver(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    handover_id: uuid.UUID,
    reason: str,
    provider: Any = None,
    model: str | None = None,
    channel: channel_mod.HandoverChannel | None = None,
) -> channel_mod.Delivery | None:
    """Compile the transcript, summarise it, and send it (HAND-04).

    Returns ``None`` when there is nothing to send to (no owner address, no channel),
    which is not a failure: the handover row is the record, and the dashboard reads
    it. Every other outcome is written onto the row so a later reader can tell
    "notified" from "tried and failed" without grepping logs.
    """
    context = session.execute(
        text(
            "SELECT c.page_url, c.started_at, c.visitor_name, c.visitor_email,"
            "       b.name AS bot_name, t.settings, t.name AS tenant_name"
            "  FROM conversations c"
            "  JOIN bots b ON b.id = c.bot_id"
            "  JOIN tenants t ON t.id = c.tenant_id"
            " WHERE c.tenant_id = :tenant_id AND c.id = :id"
        ),
        {"tenant_id": str(tenant_id), "id": str(conversation_id)},
    ).mappings().first()
    if context is None:
        return None

    messages = session.execute(
        text(
            "SELECT role, content, citations, confidence, created_at FROM messages"
            " WHERE tenant_id = :tenant_id AND conversation_id = :id"
            " ORDER BY created_at, id"
        ),
        {"tenant_id": str(tenant_id), "id": str(conversation_id)},
    ).mappings().all()

    owner_email = policy.settings(context["settings"])["email"]
    body = transcript.compile_transcript(
        messages=messages,
        bot_name=context["bot_name"],
        started_at=context["started_at"],
        page_url=context["page_url"],
        reason=reason,
        contact={"name": context["visitor_name"], "email": context["visitor_email"]},
    )

    summary = transcript.fallback_summary(messages=messages, reason=reason)
    if provider is not None:
        summary = _summarise(provider, model=model, transcript_text=body) or summary

    body = f"Summary: {summary}\n\n{body}"

    if channel is None or not owner_email:
        _mark(session, tenant_id=tenant_id, handover_id=handover_id, status="recorded")
        return None

    notification = channel_mod.Notification(
        channel="email",
        to=owner_email,
        subject=f"Handover: {context['bot_name']} needs you",
        body=body,
        reply_to=context["visitor_email"],
        conversation_id=str(conversation_id),
    )
    delivery = channel.send(notification)
    _mark(
        session,
        tenant_id=tenant_id,
        handover_id=handover_id,
        status="notified" if delivery.ok else "failed",
        detail=delivery.detail,
    )
    return delivery


def _summarise(provider: Any, *, model: str | None, transcript_text: str) -> str | None:
    """Cheap-model summary (ARCHITECTURE §11.6). Never raises.

    A summary is a convenience on top of the transcript, so a provider outage must
    degrade to ``None`` (the caller falls back) rather than lose the notification.
    """
    try:
        prompt = transcript.summary_prompt(transcript_text)
        result = provider.complete(
            [llm.ChatMessage(role=m["role"], content=m["content"]) for m in prompt],
            model=model or config.LLM_DEFAULT_MODEL,
        )
        if result.failed:
            return None
        return result.text.strip() or None
    except Exception:  # noqa: BLE001 - the summary is optional, the email is not
        logger.warning("handover summary failed; sending transcript only", exc_info=True)
        return None


def _mark(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    handover_id: uuid.UUID,
    status: str,
    detail: str = "",
) -> None:
    session.execute(
        text(
            "UPDATE handovers SET status = :status, notified_at = now()"
            " WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"status": status, "tenant_id": str(tenant_id), "id": str(handover_id)},
    )
    session.commit()
    if detail:
        logger.info("handover %s -> %s (%s)", handover_id, status, detail)
