"""The conversation turn loop (CONV-01..05, CONV-09, CONV-10).

One visitor question in, an ordered sequence of stream events out, with the
assistant's answer persisted alongside its citations and usage.

The ordering in ``turn_events`` is the product contract:

1. persist the visitor message — before retrieval, so a crash mid-turn still
   leaves the visitor's words in the transcript rather than losing them;
2. retrieve from the *published* pointer only (the pipeline has no other mode);
3. if the floor declined, emit the refusal and stop: **no completion is
   requested**. Asking a model to answer a question the retriever already
   rejected is an invitation to hallucinate;
4. otherwise stream the grounded completion, then persist it **with its
   citations** (CONV-05: a citation-free assistant row cannot be distinguished
   from an ungrounded answer during an incident review);
5. a provider failure degrades to the offline message plus handover, never an
   exception (CONV-13's contract, minus the fallback chain WP-13 adds).

Read-only is structural (CONV-10): nothing here calls anything but the LLM and
the database, and no tool definitions are ever constructed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.config import LLM_DEFAULT_MODEL
from app.conversation import prompt as prompt_builder
from app.handover import service as handover
from app.conversation import controls
from app.conversation import suggestions as suggestion_engine
from app.conversation.llm import ChatMessage, LlmProvider
from app.conversation.memory import load_history
from app.retrieval import pipeline as retrieval
from app.retrieval.embedding import EmbeddingProvider
from app.security import injection

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = (
    "I'm having trouble answering right now. I can pass your question to the team "
    "and someone will get back to you."
)

# CONV-09: an idle conversation is closed rather than left `active`. A visitor who
# walked away mid-question and comes back an hour later is starting a new
# conversation, not continuing one: replaying that gap as history is misleading and
# costs tokens for no benefit.
SESSION_TIMEOUT_SECONDS = 30 * 60

EVENT_TOKEN = "token"
EVENT_FINAL = "final"
EVENT_ERROR = "error"


@dataclass
class TurnResult:
    """Everything the SSE layer and the audit trail need after one turn."""

    visitor_message_id: UUID
    assistant_message_id: UUID | None = None
    declined: bool = False
    decline_reason: str | None = None
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    threshold: float = 0.0
    model: str | None = None
    usage: dict = field(default_factory=dict)
    degraded: bool = False


def _insert_message(
    session: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    suggestions: list[str] | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    confidence: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> UUID:
    return session.execute(
        text(
            """
            INSERT INTO messages (tenant_id, conversation_id, role, content, citations,
                                  suggestions, model, latency_ms, confidence,
                                  prompt_tokens, completion_tokens)
            VALUES (:tenant_id, :conversation_id, :role, :content,
                    CAST(:citations AS jsonb), CAST(:suggestions AS jsonb), :model,
                    :latency_ms, :confidence, :prompt_tokens, :completion_tokens)
            RETURNING id
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "role": role,
            "content": content,
            "citations": json.dumps(citations) if citations else None,
            "suggestions": json.dumps(suggestions) if suggestions else None,
            "model": model,
            "latency_ms": latency_ms,
            "confidence": confidence,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    ).scalar_one()


def _idle_expired(context: dict) -> bool:
    """Has this conversation been idle past the timeout?

    ``last_activity_at`` is a timestamptz and the comparison is done by the database
    at read time, so this is a plain datetime subtraction; a NULL is treated as not
    expired because the column is NOT NULL with a default.
    """
    from datetime import datetime, timezone

    last = context.get("last_activity_at")
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() > SESSION_TIMEOUT_SECONDS


def _close_idle(session: Session, *, tenant_id: UUID, conversation_id: UUID) -> None:
    session.execute(
        text(
            "UPDATE conversations SET status = 'closed', ended_at = now()"
            " WHERE id = :id AND tenant_id = :tenant_id AND status = 'active'"
        ),
        {"id": str(conversation_id), "tenant_id": str(tenant_id)},
    )
    session.commit()


def _touch(session: Session, *, tenant_id: UUID, conversation_id: UUID) -> None:
    """Advance ``last_activity_at``; CONV-09's timeout reads this column."""
    session.execute(
        text(
            "UPDATE conversations SET last_activity_at = now()"
            " WHERE id = :id AND tenant_id = :tenant_id"
        ),
        {"id": str(conversation_id), "tenant_id": str(tenant_id)},
    )


def conversation_context(
    session: Session, *, tenant_id: UUID, conversation_id: UUID
) -> dict:
    """Conversation status plus the bot settings the turn needs, in one query."""
    row = session.execute(
        text(
            """
            SELECT c.id AS conversation_id, c.status, c.visitor_session_id,
                   c.last_activity_at, b.id AS bot_id, b.name,
                   b.instructions, b.confidence_threshold, b.suggestion_settings,
                   t.settings AS tenant_settings
              FROM conversations c
              JOIN bots b ON b.id = c.bot_id
              JOIN tenants t ON t.id = c.tenant_id
             WHERE c.id = :conversation_id AND c.tenant_id = :tenant_id
            """
        ),
        {"conversation_id": str(conversation_id), "tenant_id": str(tenant_id)},
    ).mappings().first()
    return dict(row) if row else {}


async def _follow_ups(
    provider: LlmProvider,
    *,
    settings: dict,
    model: str,
    question: str,
    answer: str,
    declined: bool,
) -> list[str]:
    """Dynamic chips for the turn, or the owner's curated list.

    A refusal does not get generated follow-ups: the honest next step after "I
    don't know" is handover or a rephrase, and a model asked to suggest questions
    from an answer it did not give will invent them. The owner's curated chips are
    still useful there, because they are questions the knowledge base can answer.
    """
    proactive = settings["proactive"]
    if declined or not settings["dynamic"]:
        return proactive
    # The provider call is blocking; run it off the event loop so tokens already
    # sent to the visitor are not delayed behind a second round trip.
    return await asyncio.to_thread(
        suggestion_engine.generate,
        provider,
        model=model,
        question=question,
        answer=answer,
        fallback=proactive,
    )


async def turn_events(
    session: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    question: str,
    embedder: EmbeddingProvider,
    provider: LlmProvider,
    model: str | None = None,
    reranker=None,
) -> AsyncIterator[tuple[str, dict]]:
    """Run one turn, yielding ``(event, payload)`` pairs.

    An async generator rather than a function returning a list, so the HTTP layer
    can forward tokens as they arrive. The session is passed in and used
    synchronously: the caller's dependency already supplied a tenant-scoped one,
    and opening a second connection mid-stream would give one turn two RLS
    contexts.
    """
    context = conversation_context(
        session, tenant_id=tenant_id, conversation_id=conversation_id
    )
    if not context:
        yield EVENT_ERROR, {"code": "conversation_not_found"}
        return
    # CONV-09 idle timeout. Closing on read rather than on a scheduled sweep means
    # there is no window where an expired conversation can still answer; the sweep
    # (if one is added) then only tidies `ended_at` for analytics.
    if _idle_expired(context):
        _close_idle(session, tenant_id=tenant_id, conversation_id=conversation_id)
        yield EVENT_ERROR, {"code": "conversation_expired"}
        return

    if context["status"] != "active":
        # A handed-over or closed conversation must not keep answering: the
        # visitor believes a human is coming (or has left), and an assistant reply
        # in that state is a support contradiction.
        yield EVENT_ERROR, {
            "code": "conversation_not_active", "status": context["status"],
        }
        return

    # SEC-05. A message that is an injection attempt is refused before it is stored
    # or answered. Two deliberate choices:
    #   * it is refused, not silently rewritten — the visitor may be a legitimate
    #     tester, and a rewrite would answer a question they did not ask;
    #   * the refusal offers handover, because a real customer can word something
    #     that trips a pattern ("can you issue a refund") and must not be stonewalled.
    detection = injection.scan_message(question)
    if detection is not None:
        answer = prompt_builder.compose_injection_refusal()
        assistant_id = _insert_message(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            citations=[],
        )
        session.commit()
        # Recorded, not announced: the pattern name is for the operator's incident
        # review, and telling an attacker which pattern fired is free tuning advice.
        audit.record(
            session,
            tenant_id=tenant_id,
            action="conversation.injection_refused",
            entity_type="conversation",
            entity_id=conversation_id,
            detail={"pattern": detection.pattern},
        )
        session.commit()
        yield EVENT_TOKEN, {"text": answer}
        yield EVENT_FINAL, {
            "message_id": str(assistant_id),
            "visitor_message_id": None,
            "declined": True,
            "reason": "injection_refused",
            "citations": [],
            "suggestions": [],
            "confidence": None,
            "usage": {},
        }
        return

    # CONV-14 / R36: both hard stops run before anything is written or spent.
    try:
        controls.check_budget(session, tenant_id=tenant_id)
    except controls.BudgetExceeded as exc:
        # A budget stop is not an error the visitor caused, so it gets the same
        # offline-plus-handover message as a provider outage rather than a refusal
        # that implies the knowledge base lacked an answer.
        yield EVENT_ERROR, {
            "code": "budget_exceeded",
            "used": exc.used,
            "budget": exc.budget,
        }
        return

    visitor_message_id = _insert_message(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role="visitor",
        content=question,
    )
    _touch(session, tenant_id=tenant_id, conversation_id=conversation_id)
    session.commit()

    # HAND-01. A keyword hit is noticed on the visitor's message, before any
    # retrieval or spend: the visitor asked for a person, so answering first would
    # be talking over them, and the lock must land before the AI speaks.
    trigger = handover.evaluate_turn_triggers(
        question=question,
        tenant_settings=context.get("tenant_settings"),
        declined=False,
        decline_reason=None,
    )
    if trigger is not None:
        handover_id = handover.open_handover(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            reason=trigger.reason,
            channel="email",
        )
        answer = prompt_builder.compose_handover_offer()
        assistant_id = _insert_message(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            citations=[],
        )
        session.commit()
        yield EVENT_TOKEN, {"text": answer}
        yield EVENT_FINAL, {
            "message_id": str(assistant_id),
            "visitor_message_id": str(visitor_message_id),
            "declined": False,
            "handover": {
                "id": str(handover_id),
                "reason": trigger.reason,
                "requires_contact": True,
            },
            "citations": [],
            "suggestions": [],
            "confidence": None,
            "usage": {},
        }
        return

    history = load_history(
        session, tenant_id=tenant_id, conversation_id=conversation_id
    )
    # The visitor message was just persisted, so it is now the last history entry;
    # replaying it as well would duplicate the question in the model's view.
    if history and history[-1].content == question:
        history = history[:-1]

    version_ids = retrieval.active_version_ids(session, tenant_id, context["bot_id"])
    question_vector = embedder.embed([question])[0]
    config = retrieval.BotRetrievalConfig(
        confidence_threshold=float(context["confidence_threshold"]),
    )
    result = retrieval.retrieve(
        session, tenant_id, question_vector, question,
        version_ids=version_ids, config=config, reranker=reranker,
    )

    settings = suggestion_engine.settings(context["suggestion_settings"])
    chosen_model = model or LLM_DEFAULT_MODEL

    if result.declined:
        answer = prompt_builder.compose_refusal(reason=result.reason or "")
        follow_ups = await _follow_ups(
            provider,
            settings=settings,
            model=chosen_model,
            question=question,
            answer=answer,
            declined=True,
        )
        assistant_id = _insert_message(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            citations=[],
            suggestions=follow_ups,
            confidence=result.best_score,
        )
        session.commit()
        yield EVENT_TOKEN, {"text": answer}
        # HAND-01's second trigger. Only when the tenant asked to be told: a small
        # knowledge base would otherwise notify on every unanswerable question.
        handover_trigger = handover.evaluate_turn_triggers(
            question=question,
            tenant_settings=context.get("tenant_settings"),
            declined=True,
            decline_reason=result.reason,
        )
        handover_payload = None
        if handover_trigger is not None:
            handover_payload = {
                "id": str(
                    handover.open_handover(
                        session,
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        reason=handover_trigger.reason,
                        channel="email",
                    )
                ),
                "reason": handover_trigger.reason,
                "requires_contact": True,
            }
        yield EVENT_FINAL, {
            "message_id": str(assistant_id),
            "visitor_message_id": str(visitor_message_id),
            "declined": True,
            "reason": result.reason,
            "handover": handover_payload,
            "citations": [],
            "suggestions": follow_ups,
            "confidence": round(result.best_score, 4),
            "threshold": result.threshold,
            "usage": {},
        }
        return

    # CONV-15. Only the first turn of a conversation is cacheable: with history
    # present the same question can legitimately need a different answer (a
    # follow-up context), and serving a cached one would ignore what was just said.
    cached = None if history else controls.cache().get(tenant_id, question)
    if cached is not None:
        assistant_id = _insert_message(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=cached["answer"],
            citations=cached["citations"],
            suggestions=cached["suggestions"],
            model=cached.get("model"),
            confidence=cached["confidence"],
        )
        session.commit()
        yield EVENT_TOKEN, {"text": cached["answer"]}
        yield EVENT_FINAL, {
            "message_id": str(assistant_id),
            "visitor_message_id": str(visitor_message_id),
            "declined": False,
            "degraded": False,
            "cached": True,
            "citations": cached["citations"],
            "suggestions": cached["suggestions"],
            "confidence": cached["confidence"],
            "threshold": result.threshold,
            "usage": {},
        }
        return

    citations = retrieval.citations(session, tenant_id, result.chunks)
    messages = prompt_builder.build_messages(
        business_name=context["name"],
        question=question,
        chunks=result.chunks,
        history=history,
        extra_instructions=context["instructions"],
    )

    parts: list[str] = []
    try:
        async for chunk in provider.stream(messages, model=chosen_model):
            if chunk.text:
                parts.append(chunk.text)
                yield EVENT_TOKEN, {"text": chunk.text}
    except Exception:  # noqa: BLE001 - a provider crash must not break the stream
        logger.exception("llm stream failed (tenant=%s)", tenant_id)

    if not parts:
        # Either the stream raised or it produced nothing; both are one failure
        # from the visitor's side, so they get one message.
        answer = UNAVAILABLE_MESSAGE
        # No generated chips on the degraded path: the provider just failed, so
        # asking it again for suggestions would be the same broken call twice.
        follow_ups = settings["proactive"]
        assistant_id = _insert_message(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            citations=citations,
            suggestions=follow_ups,
            model=chosen_model,
            confidence=result.best_score,
        )
        session.commit()
        yield EVENT_TOKEN, {"text": answer}
        yield EVENT_FINAL, {
            "message_id": str(assistant_id),
            "visitor_message_id": str(visitor_message_id),
            "declined": False,
            "degraded": True,
            "citations": citations,
            "suggestions": follow_ups,
            "confidence": round(result.best_score, 4),
            "threshold": result.threshold,
            "usage": {},
        }
        return

    answer = "".join(parts)
    follow_ups = await _follow_ups(
        provider,
        settings=settings,
        model=chosen_model,
        question=question,
        answer=answer,
        declined=False,
    )
    assistant_id = _insert_message(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role="assistant",
        content=answer,
        citations=citations,
        suggestions=follow_ups,
        model=chosen_model,
        confidence=result.best_score,
    )
    controls.record_usage(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        model=chosen_model,
        prompt_tokens=None,
        completion_tokens=None,
    )
    # Cache only a clean, grounded answer: caching a degraded one would keep
    # serving the outage message after the provider recovered.
    controls.cache().put(
        tenant_id,
        question,
        {
            "answer": answer,
            "citations": citations,
            "suggestions": follow_ups,
            "confidence": round(result.best_score, 4),
            "model": chosen_model,
        },
    )
    session.commit()
    yield EVENT_FINAL, {
        "message_id": str(assistant_id),
        "visitor_message_id": str(visitor_message_id),
        "declined": False,
        "degraded": False,
        "citations": citations,
        "suggestions": follow_ups,
        "confidence": round(result.best_score, 4),
        "threshold": result.threshold,
        "model": chosen_model,
        "usage": {},
    }


def history_messages(
    session: Session, *, tenant_id: UUID, conversation_id: UUID
) -> list[ChatMessage]:
    """Exposed for tests and the admin transcript view."""
    return load_history(session, tenant_id=tenant_id, conversation_id=conversation_id)
