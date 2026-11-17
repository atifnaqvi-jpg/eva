"""Request dependencies: authenticated identity and the tenant-scoped session.

The tenant id used for scoping comes from the resolved session row, never from a
header, query parameter or body field (AUTH-05).

**Per-user rate limiting (SEC-02) lives here rather than in each router.** It has to
satisfy two constraints that a router-level decorator cannot:

* it applies only to callers who *have* an identity, so anonymous traffic is never
  funnelled into one shared bucket keyed on `None`; and
* it must cover every authenticated route, including ones added later — a limit each
  router opts into is a limit that the next router forgets.

`current_user` is the single place every authenticated request passes through, which
is why the check goes there.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.service import AuthenticatedUser, resolve_session
from app.config import COOKIE_NAME
from app.db.session import tenant_session
from app.conversation.controls import RateLimited, limiter
from app.observability import logs as obs_logs
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
    try:
        limiter().check_admin(user_id=identity.user_id)
    except RateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"code": exc.code, "message": "slow down"}},
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    # OPS-01. Every authenticated request passes through here, which is why the tenant
    # id is bound here rather than in each router: this is the same argument the rate
    # limit above makes, and the same failure mode if it is repeated per router.
    obs_logs.bind(tenant_id=identity.tenant_id)
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
