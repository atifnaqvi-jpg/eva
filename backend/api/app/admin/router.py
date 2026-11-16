"""Tenant membership administration (AUTH-04).

Every route here is server-side role-gated. Reads need admin, mutations need
admin, and only an owner may mint another owner.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.auth.service import AuthError, validate_credentials_input
from app.rbac.policy import Role, require
from app.security.client_ip import client_ip
from app.security.passwords import hash_password

router = APIRouter(prefix="/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return client_ip(request)


class UserSummary(BaseModel):
    id: str
    email: str
    role: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "viewer"


@router.get("/users", response_model=list[UserSummary])
def list_users(user: RequireAdmin, session: ScopedSession) -> list[UserSummary]:
    rows = session.execute(
        text("SELECT id, email, role FROM users ORDER BY created_at")
    ).mappings().all()
    return [UserSummary(id=str(r["id"]), email=r["email"], role=r["role"]) for r in rows]


@router.post("/users", response_model=UserSummary, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest, request: Request, user: RequireAdmin,
    session: ScopedSession,
) -> UserSummary:
    if payload.role not in {r.value for r in Role}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "invalid_role", "message": "unknown role"}},
        )
    if payload.role == Role.OWNER.value:
        # Only an owner can appoint another owner; an admin cannot escalate.
        require(user.role, Role.OWNER)

    try:
        validate_credentials_input(payload.email, payload.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "invalid_input", "message": str(exc)}},
        ) from exc

    email = payload.email.strip().lower()
    new_id = uuid.uuid4()
    try:
        session.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role)"
                " VALUES (:id, :t, :e, :p, :r)"
            ),
            {
                "id": str(new_id),
                "t": str(user.tenant_id),
                "e": email,
                "p": hash_password(payload.password),
                "r": payload.role,
            },
        )
        # Same transaction as the insert: the audit row cannot commit without
        # the user, nor the user without the record.
        record(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.user_id,
            action=Action.USER_CREATED,
            entity_type="user",
            entity_id=new_id,
            detail={"email": email, "role": payload.role},
            ip=_client_ip(request),
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        if "uq_users_email" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": {"code": "email_taken", "message": "email already registered"}
                },
            ) from exc
        raise

    return UserSummary(id=str(new_id), email=email, role=payload.role)
