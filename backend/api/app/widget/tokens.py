"""Widget session tokens and `Origin`-to-`bot_domains` verification (WIDG-05, §12.3).

ARCHITECTURE §12.3 is explicit that the bot's public id is *not* a secret: "security
comes from allowed-domain verification + per-session signed tokens, not from hiding
ids". This module is those two things and nothing else.

**Token format** is `base64url(payload).base64url(hmac_sha256(payload))`, where the
payload is compact JSON. Signed rather than stored because a widget mints a token per
visit, and storing one row per pageview turns an unauthenticated endpoint into a
database write amplifier. Stateless tokens mean a mint costs no IO.

Revocation is therefore *not* possible per token. The mitigation is the TTL, which
is the right trade for a 30-minute chat: the alternative is a lookup on every SSE
turn to check a row that existed only to be revocable.

Standard library HMAC, not a JWT library: a JWT adds a dependency to get a header we
do not use, plus a family of algorithm-confusion footguns we would have to disable.
The algorithm here is fixed by construction — there is no `alg` field for an attacker
to rewrite, and `hmac.compare_digest` is the constant-time comparison.

**What the token binds.** `tenant_id`, `bot_id`, `origin` and an expiry. The tenant
is in the token so the widget route can set the RLS context from the token rather
than from client input (§12.3: "tenant_id derived server-side from the token, never
from client input"). The origin is in it so a token minted for `shop.example` cannot
be replayed from `evil.example` — an origin check at *mint* time alone would not
prevent that.

**Domain matching is exact after normalisation**, with no implicit subdomain
wildcard. A `*.example.com` wildcard would let any user-content or forgotten-staging
subdomain impersonate the tenant's widget. Explicit entries only: `www.example.com`
and `example.com` are two entries. More setup friction, correct default for a
security boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

# 30 minutes: long enough for a support conversation, short enough that a token
# left in a shared browser is not durable. The widget refreshes silently.
WIDGET_TOKEN_TTL_SECONDS = 30 * 60

_MAX_PAYLOAD_BYTES = 2048

DEFAULT_PORTS = {"http": 80, "https": 443}


class TokenError(Exception):
    """Raised for any token that must not be honoured. Deliberately one class.

    The visitor sees "please reload" either way; distinguishing "expired" from "bad
    signature" in the response would tell a prober which half of a forged token to
    fix.
    """


def _secret() -> bytes:
    """The signing key. Absent means the feature is off, never a default key.

    A hardcoded fallback would be the worst bug in this file: every deployment would
    share it, so anyone could mint a token for any tenant. Unset fails closed.
    """
    value = os.environ.get("WIDGET_TOKEN_SECRET", "").strip()
    if len(value) < 32:
        raise TokenError("widget token secret is not configured")
    return value.encode()


def configured() -> bool:
    try:
        _secret()
    except TokenError:
        return False
    return True


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class WidgetToken:
    tenant_id: UUID
    bot_id: UUID
    origin: str
    expires_at: int


def issue(
    *,
    tenant_id: UUID,
    bot_id: UUID,
    origin: str,
    ttl_seconds: int = WIDGET_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    payload = {
        "t": str(tenant_id),
        "b": str(bot_id),
        "o": normalise_origin(origin),
        "e": issued + ttl_seconds,
        # A nonce guarantees two tokens minted in the same second are distinct, so
        # a log or cache cannot correlate them.
        "n": secrets.token_urlsafe(8),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_secret(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def verify(token: str, *, now: float | None = None) -> WidgetToken:
    """Parse and validate a token, or raise ``TokenError``."""
    if not isinstance(token, str) or token.count(".") != 1:
        raise TokenError("malformed token")
    encoded_payload, encoded_signature = token.split(".", 1)
    try:
        raw = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
    except (ValueError, TypeError):
        raise TokenError("malformed token") from None
    if len(raw) > _MAX_PAYLOAD_BYTES:
        raise TokenError("malformed token")

    expected = hmac.new(_secret(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise TokenError("bad signature")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise TokenError("malformed token") from None
    if not isinstance(payload, dict):
        raise TokenError("malformed token")

    try:
        tenant_id = UUID(str(payload["t"]))
        bot_id = UUID(str(payload["b"]))
        origin = str(payload["o"])
        expires_at = int(payload["e"])
    except (KeyError, ValueError, TypeError):
        raise TokenError("malformed token") from None

    if expires_at <= int(now if now is not None else time.time()):
        raise TokenError("expired token")

    return WidgetToken(
        tenant_id=tenant_id, bot_id=bot_id, origin=origin, expires_at=expires_at
    )


def normalise_origin(value: str) -> str:
    """`https://WWW.Example.com:443/path` -> `https://www.example.com`.

    Lowercased, default port dropped, path and query discarded. An origin is a
    scheme+host+port triple: keeping the path would make every page a separate
    allowed domain, and keeping the case would let one site be two entries.

    Raises on anything unusable, including the `null` origin sent by `file://` pages
    and sandboxed iframes — such a page has no host to verify and must not match.
    """
    if not value:
        raise TokenError("missing origin")
    value = value.strip()
    if value == "null":
        raise TokenError("null origin")
    if "://" not in value:
        raise TokenError("origin must include a scheme")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise TokenError("unsupported origin scheme")
    if not parts.hostname:
        raise TokenError("origin has no host")
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        raise TokenError("invalid port") from None
    if port and port != DEFAULT_PORTS[parts.scheme]:
        return f"{parts.scheme}://{host}:{port}"
    return f"{parts.scheme}://{host}"


def origin_matches(origin: str, allowed: list[str]) -> bool:
    """Exact match against the tenant's allowed domains, both normalised.

    Owners type bare hosts ("shop.example.com"), so entries are normalised as
    `https://` — that is what an owner expects to be able to type, and production
    widget traffic is TLS-only anyway.
    """
    try:
        candidate = normalise_origin(origin)
    except TokenError:
        return False

    for entry in allowed:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            normalised = normalise_origin(
                entry if "://" in entry else f"https://{entry}"
            )
        except TokenError:
            continue
        if normalised == candidate:
            return True
    return False
