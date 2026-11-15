"""Auth endpoints (ARCHITECTURE 17.1).

Paths are relative; the router is mounted under /api/v1.
"""

import ipaddress
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.dependencies import CurrentUser
from app.auth.service import (
    AuthError,
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidEmail,
    WeakPassword,
    login,
    logout,
    register_tenant,
)
from app.config import (
    COOKIE_NAME,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    SESSION_TTL_SECONDS,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_ERROR_STATUS = {
    EmailAlreadyRegistered: status.HTTP_409_CONFLICT,
    InvalidCredentials: status.HTTP_401_UNAUTHORIZED,
    WeakPassword: status.HTTP_422_UNPROCESSABLE_ENTITY,
    InvalidEmail: status.HTTP_422_UNPROCESSABLE_ENTITY,
}


class RegisterRequest(BaseModel):
    email: str
    password: str
    tenant_name: str = Field(default="My workspace", max_length=200)


class LoginRequest(BaseModel):
    email: str
    password: str


class SessionResponse(BaseModel):
    user_id: str
    tenant_id: str
    email: str
    role: str


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
    )


def _client_ip(request: Request) -> str | None:
    """Client address for the audit trail, or None if it is not a real IP.

    The ASGI server usually supplies one, but the value is only as trustworthy as
    the transport (and non-IP placeholders do occur), so it is validated before it
    reaches an ``inet`` column.
    """
    if request.client is None:
        return None
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return None


def _error(exc: AuthError) -> HTTPException:
    code = "invalid_request"
    if isinstance(exc, EmailAlreadyRegistered):
        code = "email_taken"
    elif isinstance(exc, InvalidCredentials):
        code = "invalid_credentials"
    elif isinstance(exc, WeakPassword):
        code = "weak_password"
    elif isinstance(exc, InvalidEmail):
        code = "invalid_email"
    return HTTPException(
        status_code=_ERROR_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST),
        detail={"error": {"code": code, "message": str(exc)}},
    )


@router.post("/register", response_model=SessionResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, response: Response) -> SessionResponse:
    try:
        identity, token = register_tenant(
            payload.email, payload.password, payload.tenant_name,
            ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise _error(exc) from exc

    _set_session_cookie(response, token)
    return SessionResponse(
        user_id=str(identity.user_id),
        tenant_id=str(identity.tenant_id),
        email=identity.email,
        role=identity.role,
    )


@router.post("/login", response_model=SessionResponse)
def login_endpoint(
    payload: LoginRequest, request: Request, response: Response
) -> SessionResponse:
    try:
        identity, token = login(
            payload.email, payload.password,
            ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        raise _error(exc) from exc

    # Rotation: a brand-new session row and token on every login.
    _set_session_cookie(response, token)
    return SessionResponse(
        user_id=str(identity.user_id),
        tenant_id=str(identity.tenant_id),
        email=identity.email,
        role=identity.role,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_endpoint(
    response: Response,
    session_token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> Response:
    if session_token:
        logout(session_token)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=SessionResponse)
def me(user: CurrentUser) -> SessionResponse:
    return SessionResponse(
        user_id=str(user.user_id),
        tenant_id=str(user.tenant_id),
        email=user.email,
        role=user.role,
    )
