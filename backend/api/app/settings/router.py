"""Tenant BYOK credentials (SEC-04, ARCHITECTURE §16.2, R14).

§17.5 specifies ``POST /api/v1/settings/credentials`` as **write-only**. That is the
central property of this module, and it is enforced structurally rather than by
remembering to omit a field:

* No response model here can carry a secret. There is no endpoint that returns one,
  so "never returned by any API" is a property of the router's shape rather than of
  each handler's discipline.
* ``GET`` returns ``provider``, ``created_at`` and a ``last4`` hint — the
  dashboard's ``••••`` + last-4 (§16.2). The hint is stored at write time in the
  audit trail, not recomputed on read: recomputing means decrypting the key to
  display a masked version of it, which would defeat the encryption.

Why store four characters at all: §16.2 requires the masked hint so an owner can
tell which of two keys is live. It is a deliberate, bounded disclosure — four
characters, written once, never the key itself. The key appears in no response, no
audit row and no log line.
"""

import json

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.auth.service import is_valid_email, password_problem
from app.security import passwords
from app.handover import policy as handover_policy
from app.security import vault
from app.security.client_ip import client_ip

router = APIRouter(prefix="/settings", tags=["settings"])


class SetCredentialRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    api_key: str = Field(min_length=8, max_length=4096)


class CredentialSummary(BaseModel):
    """Everything the API is allowed to say about a credential.

    There is deliberately no field that could carry the key. Adding one would be a
    visible diff on this model rather than a quiet extra dict key.
    """

    provider: str
    last4: str
    created_at: str


def _error(code: str, message: str, http: int) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": message}}
    )


def _bad_provider() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": {
                "code": "unknown_provider",
                "message": f"provider must be one of {sorted(vault.ALLOWED_PROVIDERS)}",
            }
        },
    )


@router.post(
    "/credentials",
    response_model=CredentialSummary,
    status_code=status.HTTP_201_CREATED,
)
def set_credential(
    body: SetCredentialRequest,
    user: RequireAdmin,
    session: ScopedSession,
    request: Request,
) -> CredentialSummary:
    """Store a tenant's provider key, encrypted, and never return it.

    Order matters: encrypt to the vault *before* the row is written, so a vault
    failure leaves no ``api_credentials`` row pointing at a file that does not
    exist. The reverse order produces a dangling reference that looks like a working
    credential until the tenant's first turn.
    """
    if body.provider not in vault.ALLOWED_PROVIDERS:
        raise _bad_provider()

    reference = vault.key_reference(user.tenant_id, body.provider)
    try:
        vault.put(reference, body.api_key)
    except vault.VaultError as exc:
        # 503, not 500: the request was well formed and the tenant can retry once the
        # operator fixes the vault. The message names the setting, never the key.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "vault_unavailable", "message": str(exc)}},
        ) from None

    # No UNIQUE (tenant_id, provider) constraint exists, and adding one is a schema
    # change outside §16 (G3.2). So: update in place when the provider is already
    # configured, insert otherwise. Done explicitly rather than via ON CONFLICT.
    updated = session.execute(
        text(
            "UPDATE api_credentials SET key_reference = :ref, created_at = now()"
            " WHERE tenant_id = :t AND provider = :p"
            " RETURNING provider, created_at"
        ),
        {"t": str(user.tenant_id), "p": body.provider, "ref": reference},
    ).mappings().first()
    if updated is None:
        updated = session.execute(
            text(
                "INSERT INTO api_credentials (tenant_id, provider, key_reference)"
                " VALUES (:t, :p, :ref) RETURNING provider, created_at"
            ),
            {"t": str(user.tenant_id), "p": body.provider, "ref": reference},
        ).mappings().one()

    last4 = body.api_key[-4:]
    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.CREDENTIAL_SET,
        entity_type="api_credentials",
        actor_user_id=user.user_id,
        # Provider and hint only. The audit trail is append-only, so a key written
        # there would be a permanently unrotatable leak.
        detail={"provider": body.provider, "last4": last4},
        ip=client_ip(request),
    )

    # `tenant_session` closes without committing, so an uncommitted route discards
    # its writes and still returns 201. Commit before returning; the after_begin
    # hook re-applies the tenant GUC on the next transaction if one opens.
    session.commit()

    return CredentialSummary(
        provider=updated["provider"],
        last4=last4,
        created_at=updated["created_at"].isoformat(),
    )


@router.get("/credentials", response_model=list[CredentialSummary])
def list_credentials(user: RequireAdmin, session: ScopedSession) -> list[CredentialSummary]:
    """Providers this tenant has a key for, masked.

    ``last4`` is read back from the audit trail because §16.2 keeps the credentials
    table to references only — no hint column. That keeps the schema constraint and
    still lets the dashboard show the mask. A tenant whose audit rows have aged out
    gets ``""``, which renders as ``••••`` with no hint rather than as an error.
    """
    rows = session.execute(
        text(
            """
            SELECT c.provider, c.created_at,
                   (
                     SELECT a.detail ->> 'last4'
                       FROM audit_logs a
                      WHERE a.tenant_id = c.tenant_id
                        AND a.action = :action
                        AND a.detail ->> 'provider' = c.provider
                        AND a.detail ->> 'last4' IS NOT NULL
                      ORDER BY a.created_at DESC
                      LIMIT 1
                   ) AS last4
              FROM api_credentials c
             WHERE c.tenant_id = :t
             ORDER BY c.provider
            """
        ),
        {"t": str(user.tenant_id), "action": Action.CREDENTIAL_SET},
    ).mappings()
    return [
        CredentialSummary(
            provider=r["provider"],
            last4=r["last4"] or "",
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.delete("/credentials/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def delete_credential(
    provider: str, user: RequireAdmin, session: ScopedSession, request: Request
) -> None:
    """Remove a credential: the row first, then the vault file.

    The reference must never outlive the secret it points at, or the next turn tries
    to decrypt a missing file and reports a provider error instead of "no key
    configured". A leftover ciphertext without its row is inert, so that ordering is
    the safe one.
    """
    if provider not in vault.ALLOWED_PROVIDERS:
        raise _bad_provider()

    deleted = session.execute(
        text(
            "DELETE FROM api_credentials WHERE tenant_id = :t AND provider = :p"
            " RETURNING key_reference"
        ),
        {"t": str(user.tenant_id), "p": provider},
    ).mappings().first()
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "no such credential"}},
        )

    try:
        vault.delete(deleted["key_reference"])
    except vault.VaultError:
        pass

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.CREDENTIAL_DELETED,
        entity_type="api_credentials",
        actor_user_id=user.user_id,
        detail={"provider": provider},
        ip=client_ip(request),
    )
    session.commit()


# --------------------------------------------------------------------------- profile
#
# §18.1's settings screen starts with the account the owner signed up with, but `/auth/me`
# only knows the session's identity. The tenant name and the profile live in different
# tables, so the screen would otherwise need three calls to render one heading.


class ProfileResponse(BaseModel):
    user_id: str
    email: str
    role: str
    tenant_id: str
    tenant_name: str
    created_at: str


@router.get("/profile", response_model=ProfileResponse)
def get_profile(user: RequireAdmin, session: ScopedSession) -> ProfileResponse:
    row = session.execute(
        text(
            "SELECT u.id, u.email, u.role, u.created_at, t.id AS tenant_id, t.name"
            "  FROM users u JOIN tenants t ON t.id = u.tenant_id"
            " WHERE u.id = :user_id AND u.tenant_id = :tenant_id"
        ),
        {"user_id": str(user.user_id), "tenant_id": str(user.tenant_id)},
    ).mappings().first()
    if row is None:
        raise _error("profile_not_found", "unknown user", status.HTTP_404_NOT_FOUND)
    return ProfileResponse(
        user_id=str(row["id"]),
        email=row["email"],
        role=row["role"],
        tenant_id=str(row["tenant_id"]),
        tenant_name=row["name"],
        created_at=row["created_at"].isoformat(),
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=4096)
    new_password: str = Field(min_length=1, max_length=4096)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
    response: Response,
) -> Response:
    """Change the signed-in user's password, re-proving the current one first.

    The current password is required even though the caller already holds a session:
    otherwise a borrowed unlocked laptop is enough to take an account over permanently.

    Every other session for this user is revoked, because a password change is what an
    owner does when they believe someone else has their old one. The caller's own session
    is kept so the screen they are on does not log them out mid-edit.
    """
    row = session.execute(
        text("SELECT password_hash FROM users WHERE id = :id AND tenant_id = :t"),
        {"id": str(user.user_id), "t": str(user.tenant_id)},
    ).first()
    if row is None:
        raise _error("profile_not_found", "unknown user", status.HTTP_404_NOT_FOUND)

    if not passwords.verify_password(body.current_password, row[0]):
        raise _error(
            "invalid_credentials",
            "current password is not correct",
            status.HTTP_401_UNAUTHORIZED,
        )
    problem = password_problem(body.new_password)
    if problem is not None:
        raise _error("weak_password", problem, status.HTTP_422_UNPROCESSABLE_CONTENT)

    session.execute(
        text("UPDATE users SET password_hash = :h WHERE id = :id AND tenant_id = :t"),
        {
            "h": passwords.hash_password(body.new_password),
            "id": str(user.user_id),
            "t": str(user.tenant_id),
        },
    )
    session.execute(
        text(
            "UPDATE sessions SET revoked_at = now()"
            " WHERE user_id = :id AND id <> :current AND revoked_at IS NULL"
        ),
        {"id": str(user.user_id), "current": str(user.session_id)},
    )
    record(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=Action.PASSWORD_CHANGED,
        entity_type="user",
        entity_id=user.user_id,
        detail={},
        ip=client_ip(request),
    )
    # tenant_session closes without committing (see set_credential): without this the
    # password change is discarded and the endpoint still returns 204.
    session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# ------------------------------------------------------------------- tenant settings
#
# The handover email already had a reader (``handover.policy.settings``) and no writer,
# so it could only be set by editing the database. Writes here reuse that same normaliser
# for the reason the bot-config module documents: the value shown after saving must be the
# value the handover path will act on, or the screen is lying about what will happen.


class HandoverSettings(BaseModel):
    email: str | None = None
    keywords: list[str] = Field(default_factory=list)
    notify_on_low_confidence: bool = False
    notify_on_negative_feedback: bool = True


class TenantSettings(BaseModel):
    handover: HandoverSettings


class UpdateTenantSettingsRequest(BaseModel):
    """A patch: absent keys are unchanged, so two screens can edit one blob."""

    handover: dict | None = None


@router.get("/tenant", response_model=TenantSettings)
def get_tenant_settings(user: RequireAdmin, session: ScopedSession) -> TenantSettings:
    raw = session.execute(
        text("SELECT settings FROM tenants WHERE id = :t"),
        {"t": str(user.tenant_id)},
    ).scalar() or {}
    return TenantSettings(handover=HandoverSettings(**handover_policy.settings(raw)))


@router.patch("/tenant", response_model=TenantSettings)
def update_tenant_settings(
    body: UpdateTenantSettingsRequest,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
) -> TenantSettings:
    """Merge the handover block into ``tenants.settings``.

    ``coalesce`` + ``||`` merges at the database rather than reading the blob into Python
    and writing it back, so a concurrent edit to an unrelated key (retrieval config, say)
    is not lost by a stale read.
    """
    if body.handover is None:
        raise _error("nothing_to_update", "no settings supplied", status.HTTP_400_BAD_REQUEST)

    candidate = body.handover
    if not isinstance(candidate, dict):
        raise _error(
            "invalid_settings", "handover must be an object", status.HTTP_400_BAD_REQUEST
        )
    email = candidate.get("email")
    if email is not None and not isinstance(email, str):
        raise _error(
            "invalid_settings", "handover.email must be a string", status.HTTP_400_BAD_REQUEST
        )
    # Validate through the reader's own normaliser; a value it would silently drop or
    # rewrite must be rejected here instead, or the owner saves an email that is never used.
    if email is not None and email.strip() and not is_valid_email(email):
        # The reader only strips, so it would happily store "not-an-email" and the
        # handover path would then try to send to it. Validate here with the same rule
        # signup uses.
        raise _error(
            "invalid_email", "that is not a usable email address", status.HTTP_400_BAD_REQUEST
        )
    normalised = handover_policy.settings({"handover": {**candidate}})

    session.execute(
        text(
            "UPDATE tenants SET settings = coalesce(settings, '{}'::jsonb)"
            " || jsonb_build_object('handover',"
            "    coalesce(settings->'handover', '{}'::jsonb) || CAST(:patch AS jsonb))"
            " WHERE id = :tenant_id"
        ),
        {"patch": json.dumps(normalised), "tenant_id": str(user.tenant_id)},
    )
    record(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=Action.TENANT_SETTINGS_UPDATED,
        entity_type="tenant",
        entity_id=user.tenant_id,
        detail={"handover_email": normalised["email"]},
        ip=client_ip(request),
    )
    session.commit()
    return TenantSettings(handover=HandoverSettings(**normalised))
