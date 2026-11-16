"""Session memory: what the model gets to remember (CONV-01).

History is loaded from ``messages`` for the *current conversation only*, scoped by
tenant through RLS and by conversation id explicitly. Two deliberate limits:

* **Bounded.** ``HISTORY_TURNS`` recent messages, not the whole transcript. An
  unbounded replay both costs money and lets a very old off-topic turn steer an
  unrelated question.
* **No cross-conversation recall.** Returning-visitor history (R42) is opt-in and
  belongs behind explicit consent; reading it by default would make one visitor's
  conversation context leak into another session of the same tenant.

Only ``visitor`` and ``assistant`` turns are replayed. ``system`` rows are internal
audit notes, and replaying them as conversation would let an operator note become
model instruction.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.conversation.llm import ChatMessage
from app.conversation.prompt import HISTORY_TURNS

REPLAYABLE_ROLES = ("visitor", "assistant")


def load_history(
    session: Session, *, tenant_id: UUID, conversation_id: UUID, limit: int = HISTORY_TURNS,
) -> list[ChatMessage]:
    """The last ``limit`` replayable turns, oldest first."""
    rows = session.execute(
        text(
            """
            SELECT role, content
              FROM messages
             WHERE tenant_id = :tenant_id
               AND conversation_id = :conversation_id
               AND role = ANY(:roles)
             ORDER BY created_at DESC, id DESC
             LIMIT :limit
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "roles": list(REPLAYABLE_ROLES),
            "limit": limit,
        },
    ).mappings().all()
    # Fetched newest-first so LIMIT keeps the *recent* turns; replay oldest-first.
    ordered = list(reversed(rows))
    return [
        ChatMessage(
            role="user" if row["role"] == "visitor" else "assistant",
            content=row["content"],
        )
        for row in ordered
    ]
