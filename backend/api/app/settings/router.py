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

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
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
