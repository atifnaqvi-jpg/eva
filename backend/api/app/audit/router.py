"""Audit trail endpoint (ARCHITECTURE 17.5, R29).

Read access is admin-only: a viewer has no legitimate need for who-changed-what.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.auth.dependencies import RequireAdmin, ScopedSession

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditEntry(BaseModel):
    id: str
    actor_user_id: str | None
    action: str
    entity_type: str
    entity_id: str | None
    detail: dict
    ip: str | None
    created_at: datetime


@router.get("", response_model=list[AuditEntry])
def list_audit_logs(
    user: RequireAdmin,
    session: ScopedSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime | None, Query()] = None,
) -> list[AuditEntry]:
    """Newest first. Keyset pagination on ``created_at`` rather than OFFSET, so a
    long trail does not get slower as it grows."""
    sql = (
        "SELECT id, actor_user_id, action, entity_type, entity_id, detail, ip,"
        " created_at FROM audit_logs"
    )
    params: dict[str, object] = {"limit": limit}
    if before is not None:
        sql += " WHERE created_at < :before"
        params["before"] = before
    sql += " ORDER BY created_at DESC LIMIT :limit"

    rows = session.execute(text(sql), params).mappings().all()
    return [
        AuditEntry(
            id=str(r["id"]),
            actor_user_id=str(r["actor_user_id"]) if r["actor_user_id"] else None,
            action=r["action"],
            entity_type=r["entity_type"],
            entity_id=str(r["entity_id"]) if r["entity_id"] else None,
            detail=r["detail"],
            ip=str(r["ip"]) if r["ip"] else None,
            created_at=r["created_at"],
        )
        for r in rows
    ]
