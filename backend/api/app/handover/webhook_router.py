"""Webhook management (R44, HAND-06, §17.5).

``POST /api/v1/webhooks`` and ``POST /api/v1/webhooks/{id}/test``, plus ``DELETE`` so
an owner can revoke an endpoint they no longer control.

**The signing secret is returned exactly once, at creation.** That is the same
write-only property as the BYOK endpoints (SEC-04), with one unavoidable difference:
a tenant needs the secret to verify inbound signatures, so it has to be disclosed
somewhere. Creation is the only moment it can be, and the response is the only place
it appears — ``GET`` cannot return it because the row stores a vault reference, not
the secret, and building a read model that decrypts would be the leak.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import config
from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.security import vault
from app.security.client_ip import client_ip
from app.handover import webhooks

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class CreateWebhookRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    events: list[str] = Field(min_length=1, max_length=len(webhooks.EVENTS))


class WebhookSummary(BaseModel):
    """Everything the API may say about a webhook after creation.

    No field can carry the secret; that is why it is absent rather than omitted in
    each handler.
    """

    id: str
    url: str
    events: list[str]
    active: bool
    created_at: str


class CreatedWebhook(WebhookSummary):
    """Creation response: the one time the secret is disclosed."""

    secret: str


class TestResult(BaseModel):
    ok: bool
    status: int | None = None
    detail: str = ""


def _bad(code: str, message: str, http_status: int = status.HTTP_400_BAD_REQUEST):
    return HTTPException(
        status_code=http_status, detail={"error": {"code": code, "message": message}}
    )


@router.post("", response_model=CreatedWebhook, status_code=status.HTTP_201_CREATED)
def create_webhook(
    body: CreateWebhookRequest, user: RequireAdmin, session: ScopedSession, request: Request
) -> CreatedWebhook:
    unknown = sorted(set(body.events) - set(webhooks.EVENTS))
    if unknown:
        raise _bad("unknown_event", f"events must be a subset of {list(webhooks.EVENTS)}")

    try:
        # resolve=False: a transient DNS failure should not block saving a legitimate
        # endpoint. The send path resolves again, which is where it actually matters.
        # Same flag as the send path: a deployment that allows a compose-network
        # receiver must be able to create that webhook, or the flag only works for
        # URLs nobody could have stored.
        url = webhooks.validate_url(
            body.url, resolve=False, allow_private=config.WEBHOOK_ALLOW_PRIVATE_HOSTS
        )
    except webhooks.WebhookURLRejected as exc:
        raise _bad("invalid_url", str(exc)) from exc

    webhook_id = uuid.uuid4()
    reference = webhooks.secret_reference(user.tenant_id, webhook_id)
    secret = webhooks.generate_secret()
    # Vault write precedes the row, mirroring the credential endpoint: a vault
    # failure must not leave a webhook whose secret cannot be read at send time.
    vault.put(reference, secret)

    row = session.execute(
        text(
            "INSERT INTO webhooks (id, tenant_id, url, events, secret_ref, active)"
            " VALUES (:id, :tenant_id, :url, CAST(:events AS text[]), :ref, true)"
            " RETURNING id, url, events, active, created_at"
        ),
        {
            "id": str(webhook_id),
            "tenant_id": str(user.tenant_id),
            "url": url,
            "events": body.events,
            "ref": reference,
        },
    ).mappings().one()

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.WEBHOOK_CREATED,
        entity_type="webhooks",
        entity_id=webhook_id,
        actor_user_id=user.user_id,
        # The URL is recorded; it is the tenant's own data. The secret is not, and
        # the audit trail is append-only, so a secret written here could never be
        # withdrawn.
        detail={"url": url, "events": body.events},
        ip=client_ip(request),
    )
    session.commit()

    return CreatedWebhook(
        id=str(row["id"]),
        url=row["url"],
        events=list(row["events"]),
        active=row["active"],
        created_at=row["created_at"].isoformat(),
        secret=secret,
    )


@router.get("", response_model=list[WebhookSummary])
def list_webhooks(user: RequireAdmin, session: ScopedSession) -> list[WebhookSummary]:
    rows = session.execute(
        text(
            "SELECT id, url, events, active, created_at FROM webhooks"
            " WHERE tenant_id = :tenant_id ORDER BY created_at"
        ),
        {"tenant_id": str(user.tenant_id)},
    ).mappings().all()
    return [
        WebhookSummary(
            id=str(r["id"]),
            url=r["url"],
            events=list(r["events"]),
            active=r["active"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    webhook_id: uuid.UUID, user: RequireAdmin, session: ScopedSession, request: Request
) -> None:
    """Revoke an endpoint and destroy its signing secret.

    The secret is removed *after* the row: if the vault delete fails, the endpoint is
    already unreachable (no row means ``emit`` never selects it), which is the safe
    direction. Deleting first would leave a row pointing at a missing file.
    """
    row = session.execute(
        text(
            "DELETE FROM webhooks WHERE tenant_id = :tenant_id AND id = :id"
            " RETURNING url, secret_ref"
        ),
        {"tenant_id": str(user.tenant_id), "id": str(webhook_id)},
    ).mappings().first()
    if row is None:
        raise _bad("not_found", "no such webhook", status.HTTP_404_NOT_FOUND)

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.WEBHOOK_DELETED,
        entity_type="webhooks",
        entity_id=webhook_id,
        actor_user_id=user.user_id,
        detail={"url": row["url"]},
        ip=client_ip(request),
    )
    session.commit()
    vault.delete(row["secret_ref"])


@router.post("/{webhook_id}/test", response_model=TestResult)
def test_webhook(
    webhook_id: uuid.UUID, user: RequireAdmin, session: ScopedSession, request: Request
) -> TestResult:
    """Send a signed ``ping`` so an owner can prove their receiver verifies it.

    Runs inline, unlike real deliveries: the whole point is to tell the caller what
    happened, so returning before the result exists would make the endpoint useless.
    """
    row = session.execute(
        text(
            "SELECT url, secret_ref FROM webhooks WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"tenant_id": str(user.tenant_id), "id": str(webhook_id)},
    ).mappings().first()
    if row is None:
        raise _bad("not_found", "no such webhook", status.HTTP_404_NOT_FOUND)

    try:
        secret = vault.get(row["secret_ref"])
    except Exception:  # noqa: BLE001 - the secret may be gone; report, do not 500
        raise _bad("secret_unreadable", "signing secret is not readable") from None

    delivery = webhooks.deliver(
        url=row["url"],
        secret=secret,
        event="ping",
        payload={"event": "ping", "webhook_id": str(webhook_id)},
        allow_private=config.WEBHOOK_ALLOW_PRIVATE_HOSTS,
    )

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.WEBHOOK_TESTED,
        entity_type="webhooks",
        entity_id=webhook_id,
        actor_user_id=user.user_id,
        detail={"url": row["url"], "ok": delivery.ok, "status": delivery.status},
        ip=client_ip(request),
    )
    session.commit()
    return TestResult(ok=delivery.ok, status=delivery.status, detail=delivery.detail)
