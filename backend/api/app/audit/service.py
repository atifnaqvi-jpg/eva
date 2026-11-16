"""Append-only audit trail (R29).

Writes and reads are always tenant-scoped: the tenant comes from the authenticated
identity, and RLS is the backstop if application code ever gets it wrong.

The row is written on the caller's transaction, so an admin mutation and its audit
entry commit atomically: a mutation that rolls back leaves no audit claiming it
happened, and a mutation cannot commit without its record.
"""

import json
import uuid
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session


def record(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    detail: Mapping[str, object] | None = None,
    ip: str | None = None,
) -> None:
    """Append one audit row inside the caller's transaction."""
    session.execute(
        text(
            "INSERT INTO audit_logs"
            " (tenant_id, actor_user_id, action, entity_type, entity_id, detail, ip)"
            " VALUES (:t, :a, :action, :etype, :eid, CAST(:detail AS jsonb), :ip)"
        ),
        {
            "t": str(tenant_id),
            "a": str(actor_user_id) if actor_user_id else None,
            "action": action,
            "etype": entity_type,
            "eid": str(entity_id) if entity_id else None,
            "detail": _as_json(detail or {}),
            "ip": ip,
        },
    )


def _as_json(detail: Mapping[str, object]) -> str:
    return json.dumps(detail, default=str, sort_keys=True)


# Actions worth recording. Keeping the vocabulary closed prevents typo'd action
# names from silently fragmenting the trail.
class Action:
    USER_CREATED = "user.created"
    USER_ROLE_CHANGED = "user.role_changed"
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    TENANT_REGISTERED = "tenant.registered"
    HANDOVER_CREATED = "handover.created"
    CREDENTIAL_SET = "credential.set"
    CREDENTIAL_DELETED = "credential.deleted"
