"""Request dependencies: authenticated identity and the tenant-scoped session.

The tenant id used for scoping comes from the resolved session row, never from a
header, query parameter or body field (AUTH-05).
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.service import AuthenticatedUser, resolve_session
from app.config import COOKIE_NAME
from app.db.session import tenant_session
from app.rbac.policy import Role, require


def current_user(
    session_token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> AuthenticatedUser:
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthenticated", "message": "login required"}},
        )
    identity = resolve_session(session_token)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "unauthenticated", "message": "login required"}},
        )
    return identity


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]


def scoped_session(user: CurrentUser) -> Iterator[Session]:
    """A session whose RLS context is the authenticated user's tenant."""
    with tenant_session(user.tenant_id) as session:
        yield session


ScopedSession = Annotated[Session, Depends(scoped_session)]


def require_role(minimum: Role):
    """Dependency factory: 403 unless the caller's role meets ``minimum``."""

    def _dependency(user: CurrentUser) -> AuthenticatedUser:
        require(user.role, minimum)
        return user

    return _dependency


RequireOwner = Annotated[AuthenticatedUser, Depends(require_role(Role.OWNER))]
RequireAdmin = Annotated[AuthenticatedUser, Depends(require_role(Role.ADMIN))]
RequireViewer = Annotated[AuthenticatedUser, Depends(require_role(Role.VIEWER))]
