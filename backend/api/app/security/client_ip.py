"""Which IP is the client? The answer decides whether rate limiting means anything.

Behind the Caddy ingress every request arrives from the proxy, so `request.client.host`
is the proxy's address and a per-IP limit keyed on it is a single global bucket —
useless, and actively harmful, because one visitor would then use up the budget for
everyone else. But `X-Forwarded-For` is client-controlled: a request can arrive with
any value the sender chose.

The rule here is the one that is safe under both conditions:

* **Only trust the header when explicitly configured** (`TRUST_PROXY_HEADERS`). A
  service exposed directly must not, or any caller sets their own identity and the
  per-IP limit is decorative.
* **When trusting, take the LAST hop, not the first.** Caddy *appends* the address it
  saw to whatever the client sent, so the final entry is the peer Caddy actually
  observed and the earlier ones are the attacker's fiction. Taking `[0]` is the
  common bug: it reads as "the original client" and is in fact the attacker's claim.

The deployment enables the flag where Caddy is the only ingress (compose.yaml), and
leaves it off by default, because the failure of trusting it wrongly (spoofable
identity) is worse than the failure of not trusting it (an over-broad limit).
"""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request


def trust_proxy_headers() -> bool:
    return os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def client_ip(request: Request) -> str | None:
    """The client address for rate-limit keying and the audit trail, or None.

    The result must be a real IP: callers write it to ``inet`` columns, and the
    forwarded header is attacker-controlled text. Without this check a request
    carrying ``X-Forwarded-For: whatever`` raises ``InvalidTextRepresentation``
    inside the insert, which turns every audited route — login included — into a
    500 that any anonymous caller can trigger. Validation belongs here rather than
    at each call site for the same reason the header parsing does: one place, or
    the next caller forgets.
    """
    if trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [part.strip() for part in forwarded.split(",") if part.strip()]
            if hops:
                # The last entry is the one our own proxy appended.
                parsed = _parse(hops[-1])
                if parsed is not None:
                    return parsed
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    return _parse(host) if host else None


def _parse(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None
