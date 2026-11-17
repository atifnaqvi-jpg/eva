"""Authentication: register, login (with rotation), logout, session resolution.

Tenant identity always comes from the database (the user row, or the session row),
never from request input — AUTH-05 / 16.3.
"""

import functools
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.audit.service import Action, record
from app.config import SESSION_TTL_SECONDS
from app.db.session import SessionLocal, tenant_session
from app.security.passwords import hash_password, hash_token, verify_password

ROLES = ("owner", "admin", "viewer")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    """Base for auth failures that map to a 4xx response."""


class InvalidCredentials(AuthError):
    pass


class EmailAlreadyRegistered(AuthError):
    pass


class WeakPassword(AuthError):
    pass


class InvalidEmail(AuthError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    session_id: uuid.UUID


def is_valid_email(email: str) -> bool:
    """The one email rule in the product, so settings and signup cannot disagree."""
    return bool(_EMAIL_RE.match(email.strip()))


def password_problem(password: str) -> str | None:
    """The password rule as a value.

    Returned rather than raised so callers that only need to *report* the rule (password
    change) do not have to catch an exception whose class identity depends on whether the
    module was reloaded.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


def validate_credentials_input(email: str, password: str) -> None:
    if not is_valid_email(email):
        raise InvalidEmail("email address is not valid")
    problem = password_problem(password)
    if problem is not None:
        raise WeakPassword(problem)


def _issue_session(
    tenant_id: uuid.UUID, user_id: uuid.UUID, ip: str | None, user_agent: str | None
) -> str:
    """Create a session row and return the raw cookie token (only ever sent once)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS)
    with tenant_session(tenant_id) as session:
        session.execute(
            text(
                "INSERT INTO sessions (tenant_id, user_id, token_hash, expires_at, ip,"
                " user_agent) VALUES (:t, :u, :h, :e, :ip, :ua)"
            ),
            {
                "t": str(tenant_id),
                "u": str(user_id),
                "h": hash_token(token),
                "e": expires,
                "ip": ip,
                "ua": user_agent[:512] if user_agent else None,
            },
        )
        session.commit()
    return token


def register_tenant(
    email: str, password: str, tenant_name: str, ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[AuthenticatedUser, str]:
    """Create tenant + owner account, then log them in."""
    validate_credentials_input(email, password)
    email = email.strip().lower()

    password_hash = hash_password(password)

    # The tenant does not exist yet, so this runs on an unscoped connection: the
    # INSERT policy is satisfied because the new row's tenant_id is the one we
    # just created. Guarded by RLS WITH CHECK via the tenant_id we pass in.
    session = SessionLocal()
    try:
        session.begin()
        tenant_id = uuid.uuid4()
        session.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, :name)"),
            {"id": str(tenant_id), "name": tenant_name.strip() or "My workspace"},
        )
        session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        # Registration runs as eva_app, which cannot see other tenants' users;
        # the global unique index is what actually guarantees uniqueness.
        session.execute(
            text(
                "INSERT INTO users (tenant_id, email, password_hash, role)"
                " VALUES (:t, :e, :p, 'owner')"
            ),
            {"t": str(tenant_id), "e": email, "p": password_hash},
        )
        user_id = session.execute(
            text("SELECT id FROM users WHERE tenant_id = :t AND email = :e"),
            {"t": str(tenant_id), "e": email},
        ).scalar_one()
        # One bot per tenant in the MVP, created with the tenant so that publish
        # and the widget have something to point at from the first login. The
        # public_id is the unguessable handle the widget loader will use.
        session.execute(
            text(
                "INSERT INTO bots (tenant_id, public_id, name)"
                " VALUES (:t, :p, :n)"
            ),
            {
                "t": str(tenant_id),
                "p": secrets.token_urlsafe(12),
                "n": tenant_name.strip() or "My workspace",
            },
        )
        record(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action=Action.TENANT_REGISTERED,
            entity_type="tenant",
            entity_id=tenant_id,
            detail={"email": email, "tenant_name": tenant_name},
            ip=ip,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        session.close()
        if "uq_users_email" in str(exc):
            raise EmailAlreadyRegistered("email already registered") from exc
        raise
    session.close()

    token = _issue_session(tenant_id, user_id, ip, user_agent)
    return (
        AuthenticatedUser(user_id, tenant_id, email, "owner", uuid.uuid4()),
        token,
    )


@functools.cache
def _dummy_hash() -> str:
    """A real hash of a throwaway value, used only for timing parity on unknown
    emails. Built lazily: at production Argon2 cost this takes minutes, so doing
    it at import time would stall startup."""
    return hash_password(secrets.token_urlsafe(24))


def _lookup_user(email: str) -> dict[str, object] | None:
    """Find the user by email before any tenant context exists (0002 function)."""
    session = SessionLocal()
    try:
        row = session.execute(
            text(
                "SELECT id, tenant_id, password_hash, role, auth_provider"
                " FROM auth_user_by_email(:e)"
            ),
            {"e": email},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        session.close()


def login(
    email: str, password: str, ip: str | None = None, user_agent: str | None = None
) -> tuple[AuthenticatedUser, str]:
    """Email+password -> rotated session.

    Always performs one hash comparison, even when the email is unknown, so the
    response time does not reveal whether an account exists.
    """
    email = email.strip().lower()
    user = _lookup_user(email)

    if user is None:
        # Burn a hash to keep timing comparable. Constant work, discarded result.
        verify_password(password, _dummy_hash())
        # No tenant exists for this address, so there is nowhere to file the event
        # that RLS would allow; the app log carries it instead.
        raise InvalidCredentials("invalid email or password")

    if user["auth_provider"] != "password" or not user["password_hash"]:
        verify_password(password, _dummy_hash())
        _audit_login(user["tenant_id"], user["id"], email, succeeded=False, ip=ip)  # type: ignore[arg-type]
        raise InvalidCredentials("invalid email or password")

    if not verify_password(password, str(user["password_hash"])):
        _audit_login(user["tenant_id"], user["id"], email, succeeded=False, ip=ip)  # type: ignore[arg-type]
        raise InvalidCredentials("invalid email or password")

    tenant_id = user["tenant_id"]
    _audit_login(tenant_id, user["id"], email, succeeded=True, ip=ip)  # type: ignore[arg-type]
    token = _issue_session(tenant_id, user["id"], ip, user_agent)  # type: ignore[arg-type]
    return (
        AuthenticatedUser(
            user_id=user["id"],  # type: ignore[arg-type]
            tenant_id=tenant_id,  # type: ignore[arg-type]
            email=email,
            role=str(user["role"]),
            session_id=uuid.uuid4(),
        ),
        token,
    )


def resolve_session(token: str) -> AuthenticatedUser | None:
    """Turn a cookie value into an authenticated identity, or None."""
    if not token:
        return None
    session = SessionLocal()
    try:
        row = session.execute(
            text(
                "SELECT session_id, tenant_id, user_id, role, email, expires_at"
                " FROM auth_session_by_token(:h)"
            ),
            {"h": hash_token(token)},
        ).mappings().first()
    finally:
        session.close()
    if not row:
        return None
    return AuthenticatedUser(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        role=row["role"],
        session_id=row["session_id"],
    )


def _audit_login(
    tenant_id: uuid.UUID, user_id: uuid.UUID, email: str, *, succeeded: bool,
    ip: str | None,
) -> None:
    """Record an authentication attempt in its own short transaction.

    A failed login must still be recorded, so this cannot ride on the caller's
    transaction (there is none) — and it must not be rolled back with the failure.
    """
    with tenant_session(tenant_id) as session:
        record(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action=Action.LOGIN_SUCCEEDED if succeeded else Action.LOGIN_FAILED,
            entity_type="user",
            entity_id=user_id,
            detail={"email": email, "succeeded": succeeded},
            ip=ip,
        )
        session.commit()


def logout(token: str) -> None:
    """Revoke the current session. Idempotent."""
    identity = resolve_session(token)
    if identity is None:
        return
    with tenant_session(identity.tenant_id) as session:
        session.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id = :id"),
            {"id": str(identity.session_id)},
        )
        record(
            session,
            tenant_id=identity.tenant_id,
            actor_user_id=identity.user_id,
            action=Action.LOGOUT,
            entity_type="session",
            entity_id=identity.session_id,
            detail={},
        )
        session.commit()

