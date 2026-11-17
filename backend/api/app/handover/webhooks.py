"""Signed webhook notifications (R44, HAND-06, §17.5).

A webhook is the tenant telling this platform to make an outbound HTTP request to an
address they choose, from inside our network. That sentence is the whole security
problem, and it drives three decisions here:

* **The signing secret is generated server-side and stored in the vault** (SEC-03),
  never accepted from the client and never returned by an API. A tenant-supplied
  secret would let a caller sign as another tenant, and a returned secret defeats the
  point of signing.
* **The URL is validated before every send, not only at creation.** DNS is mutable:
  a hostname that resolved to a public address when the webhook was created can
  resolve to ``169.254.169.254`` an hour later. Checking at creation alone is a
  time-of-check/time-of-use hole.
* **Redirects are not followed.** A permitted URL that 302s to a private address
  would otherwise bypass the check, and the check is the only guard.

Delivery mirrors ``channels.EmailChannel``: a failure is a value, not an exception.
The visitor has already been told a human is coming, so a dead webhook endpoint must
never turn into an error they see or a lost handover row.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.observability import logs as obs_logs

logger = logging.getLogger(__name__)

EVENTS = ("conversation.created", "handover.created")

# The signature scheme. Sent as a header so a receiver knows how to verify without
# guessing, and versioned so a future scheme does not silently break existing ones.
SIGNATURE_HEADER = "X-Eva-Signature"
SIGNATURE_VERSION = "v1"
TIMESTAMP_HEADER = "X-Eva-Timestamp"
EVENT_HEADER = "X-Eva-Event"

# A receiver that hangs must not hold a delivery thread forever. Webhooks run off the
# request path, but an unbounded socket would still leak a thread per delivery.
TIMEOUT_SECONDS = 10.0

# Ports allowed on an outbound webhook. A webhook is an HTTP endpoint; permitting
# arbitrary ports invites SMTP/Redis/Postgres probing from inside the network.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


class WebhookURLRejected(ValueError):
    """The URL is not a permissible webhook target."""


@dataclass(frozen=True)
class Delivery:
    ok: bool
    status: int | None = None
    detail: str = ""


def secret_reference(tenant_id: uuid.UUID, webhook_id: uuid.UUID) -> str:
    """Where the signing secret lives in the vault.

    Deliberately *not* keyed by URL or by an incrementing id: rotating a webhook means
    deleting one row and creating another, and the reference must not collide with a
    previous secret for a different endpoint.
    """
    return f"{tenant_id}/webhook-{webhook_id}.age"


def generate_secret() -> str:
    """32 bytes of entropy, hex-encoded.

    HMAC key length beyond the hash block size buys nothing, so 32 bytes is the
    ceiling rather than an arbitrary choice.
    """
    return secrets.token_hex(32)


def sign(secret: str, *, timestamp: str, body: bytes) -> str:
    """``v1=<hex hmac-sha256 over "timestamp.body">``.

    The timestamp is inside the signed material so a captured request cannot be
    replayed with a fresh timestamp: the receiver checks the timestamp is recent and
    that the signature covers the one it received.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def verify(
    secret: str,
    *,
    timestamp: str,
    body: bytes,
    signature: str,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Receiver-side check. Exported so tests verify the *documented* scheme.

    Kept in the same module as ``sign`` on purpose: a verifier written from a prose
    description is how signing schemes drift apart.
    """
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else int(time.time())) - sent) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp=timestamp, body=body)
    return hmac.compare_digest(expected, signature)


def validate_url(url: str, *, resolve: bool = True, allow_private: bool = False) -> str:
    """Return the URL, or raise ``WebhookURLRejected`` with a reason.

    ``resolve=False`` skips DNS (used at creation time, where a transient resolver
    failure should not reject a legitimate endpoint), but every *send* resolves.

    ``allow_private`` exists for tests and local verification, where the receiving
    endpoint is on the compose network. It is read from
    ``config.WEBHOOK_ALLOW_PRIVATE_HOSTS``, which defaults to *false*; turning it on
    disables the SSRF guard and must never be set in a deployment that accepts
    tenant-supplied webhook URLs.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise WebhookURLRejected("url must be http or https")
    if not parts.hostname:
        raise WebhookURLRejected("url must include a host")
    if parts.username or parts.password:
        raise WebhookURLRejected("url must not embed credentials")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise WebhookURLRejected(f"port {port} is not allowed")

    host = parts.hostname
    # A literal IP is checked directly; a name is resolved and every answer checked,
    # because a name with one public and one private A record would otherwise pass.
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise WebhookURLRejected(f"could not resolve host: {exc}") from exc
        addresses = {info[4][0] for info in infos}
    else:
        # Creation time. An IP literal is still classified here — a name cannot be
        # judged without resolving, but a literal needs no DNS to be known bad, and
        # skipping it would let `http://169.254.169.254/` be stored. A hostname is
        # left to the send path, which resolves and re-checks every answer.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            addresses = set()
        else:
            addresses = {host}

    if allow_private:
        return url

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise WebhookURLRejected(f"unparsable address: {address}") from None
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            # Blocks the metadata service, the compose network (postgres, redis, the
            # api itself) and localhost. This is the guard that makes a tenant-supplied
            # URL safe to fetch.
            raise WebhookURLRejected(f"address {ip} is not a public address")
    return url


def deliver(
    *,
    url: str,
    secret: str,
    event: str,
    payload: dict,
    allow_private: bool = False,
) -> Delivery:
    """POST a signed event. Returns a ``Delivery``; never raises.

    ``validate_url`` is called here rather than trusted from the caller: this is the
    function that opens the socket, so it re-checks (same reasoning as
    ``vault._path_for``).
    """
    try:
        validate_url(url, allow_private=allow_private)
    except WebhookURLRejected as exc:
        logger.warning("webhook %s refused before send: %s", url, exc)
        return Delivery(ok=False, detail=f"rejected: {exc}")

    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "eva-webhooks/1",
            SIGNATURE_HEADER: sign(secret, timestamp=timestamp, body=body),
            TIMESTAMP_HEADER: timestamp,
            EVENT_HEADER: event,
        },
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """A redirect is a way to reach an address ``validate_url`` refused."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return Delivery(ok=True, status=response.status, detail=f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        # A 3xx arrives here too once redirect handling is disabled; distinguishing
        # it from a genuine 5xx matters when debugging a tenant's endpoint.
        return Delivery(ok=False, status=exc.code, detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Delivery(ok=False, detail=f"{type(exc).__name__}: {exc}")


def active_for(session, *, tenant_id: uuid.UUID, event: str) -> list[dict]:
    """Active webhooks of a tenant subscribed to ``event``.

    ``events`` is a Postgres array; the containment operator keeps the filter in the
    database rather than in Python, so a tenant with many webhooks does not pay for
    all of them on every event.
    """
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT id, url, secret_ref FROM webhooks"
            " WHERE tenant_id = :tenant_id AND active AND events @> ARRAY[:event]::text[]"
        ),
        {"tenant_id": str(tenant_id), "event": event},
    ).mappings().all()
    return [dict(row) for row in rows]


def emit(
    session,
    *,
    tenant_id: uuid.UUID,
    event: str,
    payload: dict,
    allow_private: bool = False,
) -> list[Delivery]:
    """Deliver one event to every subscribed webhook (HAND-06).

    Runs off the request path (see ``widget._notify``); a failure for one endpoint
    must not stop the others, so each is attempted independently.
    """
    from app.security import vault

    results: list[Delivery] = []
    for hook in active_for(session, tenant_id=tenant_id, event=event):
        try:
            secret = vault.get(hook["secret_ref"])
        except Exception:  # noqa: BLE001 - a missing secret must not break the rest
            logger.warning("webhook %s has no readable secret", hook["id"], exc_info=True)
            results.append(Delivery(ok=False, detail="secret unreadable"))
            continue
        results.append(
            deliver(
                url=hook["url"],
                secret=secret,
                event=event,
                payload=payload,
                allow_private=allow_private,
            )
        )
    return results


def emit_async(
    *,
    tenant_id: uuid.UUID,
    event: str,
    payload: dict,
    allow_private: bool = False,
) -> None:
    """Deliver an event off the request path (HAND-06).

    Same shape as ``widget._notify`` and for the same reason: the visitor has already
    been answered, and a tenant endpoint that hangs for the full timeout must not
    hold their connection open. The thread opens its own session because the
    caller's session is closed when its request returns.
    """
    from app.db import session as db_session

    def _work() -> None:
        with db_session.tenant_session(tenant_id) as own:
            try:
                emit(
                    own,
                    tenant_id=tenant_id,
                    event=event,
                    payload=payload,
                    allow_private=allow_private,
                )
            except Exception:  # noqa: BLE001 - a webhook never fails the caller
                logger.exception("webhook emission failed for %s", event)

    # OPS-01: same reason as `widget._notify` — a fresh thread starts with an empty
    # context, and the emission line belongs to the conversation that caused it.
    context = obs_logs.snapshot()
    threading.Thread(
        target=lambda: context.run(_work), name=f"webhook-{event}", daemon=True
    ).start()
