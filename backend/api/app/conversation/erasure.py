"""Data-subject erasure: delete a conversation and the visitor identifiers in it (SEC-10).

ARCHITECTURE §19.3 states the right in one line: *"deletion = delete conversation
records + visitor identifiers; data-export = transcript export (R32 doubles as the
SAR mechanism)"*. The export half is ``archive.py`` (ADMN-07); this module is the
deletion half, and it is on demand rather than scheduled — SEC-09's purge deletes on
a retention clock, which is data minimisation, not a response to a data subject.

**Why a separate module rather than a function in ``archive.py``.** That module is a
read path with a streamed export; a destructive route beside it invites the next
person to factor the two together and get the transaction order wrong. The ordering
here is the whole correctness argument, so it stays where it can be read in full.

**What the delete actually removes.** Deleting the ``conversations`` row cascades:
``messages``, ``feedback`` (through ``messages``), ``handovers`` and ``visitor_files``
all declare ``ON DELETE CASCADE`` on ``conversation_id``. That cascade is what makes
the visitor identifiers go with it — ``visitor_session_id``, ``visitor_name``,
``visitor_email`` and ``page_url`` are columns on the conversation itself.

Two things deliberately survive:

* ``usage_records`` — ``conversation_id`` is ``ON DELETE SET NULL``, so the tenant's
  token and cost totals stay correct while the link to the erased conversation goes.
  The row holds a kind, a model, units and cost; no visitor identifier.
* ``audit_logs`` — append-only by trigger (R29) and never purged. Nothing here writes
  visitor PII into it: the detail carries counts and the conversation id, never the
  name, address, session id or page URL. That is asserted by a test rather than
  trusted, because the obvious way to make an erasure auditable is to put the erased
  record in the log, which would undo it.

**There are no visitor bytes to unlink.** Visitor uploads store metadata only
(``app/widget/uploads.py`` records mime, size and a server-chosen key; the payload is
discarded and the codebase performs zero filesystem writes), so the cascade leaves
nothing behind. If that ever changes, this module is where the unlink belongs — which
is why it is stated rather than assumed.

**Any status is erasable, unlike the retention purge.** The purge takes only
``closed``/``handed_over`` conversations because it deletes behind a visitor's back.
Erasure is the opposite: an explicit request that overrides the retention floor, and
refusing a live conversation would deny the right for exactly as long as the visitor
keeps talking. The cost is stated rather than hidden: an ``active`` conversation erased
this way leaves the visitor's next turn answering ``conversation_not_found``, which is
the widget's ordinary answer for a conversation that does not exist.

**404, not 403, for another tenant's conversation.** A 403 confirms the id exists,
which turns a guessed UUID into a known one. Same rule as the read path, and the
reason the isolation test probes with a real id from a second tenant.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.observability import logs as obs_logs
from app.security.client_ip import client_ip

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Counted before the delete so the audit row can say what the erasure removed. After
# the cascade they are gone, and "we deleted something" is not evidence — an account
# of what went is (the lesson SEC-09's purge is built on).
_COUNT_CHILDREN = text(
    """
    SELECT (SELECT count(*) FROM messages
             WHERE conversation_id = :id AND tenant_id = :tenant_id)      AS messages,
           (SELECT count(*) FROM feedback f
              JOIN messages m ON m.id = f.message_id AND m.tenant_id = f.tenant_id
             WHERE m.conversation_id = :id AND f.tenant_id = :tenant_id)  AS feedback,
           (SELECT count(*) FROM handovers
             WHERE conversation_id = :id AND tenant_id = :tenant_id)      AS handovers,
           (SELECT count(*) FROM visitor_files
             WHERE conversation_id = :id AND tenant_id = :tenant_id)      AS visitor_files
    """
)


def _uuid(value: str) -> uuid.UUID:
    """Parse the path id, answering 400 the way every sibling route on this prefix does.

    A different status for the same malformed input on GET and DELETE would make
    "not found" and "not parsed" harder to tell apart for whoever debugs a client.
    """
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "invalid_uuid", "message": "bad conversation_id"}},
        ) from None


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def erase_conversation(
    conversation_id: str,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
) -> None:
    """Erase one conversation and the visitor identifiers it holds (SEC-10, R30).

    Admin rather than viewer, following the other destructive routes (credentials,
    webhooks): a viewer may read the archive, and reading is not consent to destroy it.
    """
    cid = _uuid(conversation_id)
    obs_logs.bind(conversation_id=cid)
    params = {"id": str(cid), "tenant_id": str(user.tenant_id)}

    erased: Any = session.execute(_COUNT_CHILDREN, params).mappings().first()
    deleted = session.execute(
        text(
            "DELETE FROM conversations WHERE id = :id AND tenant_id = :tenant_id"
            " RETURNING id"
        ),
        params,
    ).first()
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "conversation_not_found",
                    "message": "unknown conversation",
                }
            },
        )

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.CONVERSATION_ERASED,
        entity_type="conversation",
        entity_id=cid,
        actor_user_id=user.user_id,
        detail=dict(erased),
        ip=client_ip(request),
    )
    # tenant_session closes without committing, so without this the route answers 204
    # and the conversation is still there.
    session.commit()
