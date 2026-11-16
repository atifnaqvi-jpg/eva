"""SEC-02: per-IP and per-user rate limiting, and the proxy-header rule that makes
the per-IP key mean anything.

The interesting part is not "does the counter increment" — it is *which address* the
counter is keyed on. Behind the ingress every request comes from the proxy, so a
naive `request.client.host` gives one global bucket: one visitor exhausts the limit
for all of them. The fix reads `X-Forwarded-For`, which the client controls, so the
rule has to be "only when configured, and only the last hop" — and both halves are
tested here, because either one alone is a vulnerability rather than a control.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest
from starlette.requests import Request

from app.conversation import controls
from app.security import client_ip as ip_module


def _request(headers: dict[str, str], client: tuple[str, int] = ("10.0.0.9", 1234)):
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": client,
        "query_string": b"",
        "server": ("testserver", 443),
        "scheme": "https",
    }
    return Request(scope)


# --- which address is the client ------------------------------------------


def test_direct_client_address_is_used_when_proxy_headers_are_not_trusted(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    request = _request({"x-forwarded-for": "1.2.3.4"})
    assert ip_module.client_ip(request) == "10.0.0.9"


def test_a_spoofed_forwarded_header_is_ignored_by_default(monkeypatch) -> None:
    """Trusting it by default would make the per-IP limit decorative."""
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    request = _request({"x-forwarded-for": "1.2.3.4, 10.0.0.9"})
    assert ip_module.client_ip(request) == "10.0.0.9"


def test_the_last_forwarded_hop_is_used_when_trusted(monkeypatch) -> None:
    """Caddy *appends* the peer it saw; earlier entries are the client's fiction."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    request = _request({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
    assert ip_module.client_ip(request) == "203.0.113.7"


def test_the_first_forwarded_entry_is_never_used(monkeypatch) -> None:
    """Taking [0] reads as 'the original client' and is the attacker's claim."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    request = _request({"x-forwarded-for": "6.6.6.6, 203.0.113.7"})
    assert ip_module.client_ip(request) != "6.6.6.6"


def test_missing_forwarded_header_falls_back_to_the_peer(monkeypatch) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    assert ip_module.client_ip(_request({})) == "10.0.0.9"


# --- the limiter's scopes -------------------------------------------------


@pytest.fixture
def limiter():
    fresh = controls.RateLimiter()
    fresh.visitor_limit = 2
    fresh.ip_limit = 3
    fresh.user_limit = 2
    fresh.tenant_limit = 100
    return fresh


def test_a_fresh_visitor_session_does_not_escape_the_ip_limit(limiter) -> None:
    """The reason the IP scope exists: mint a new visitor id per request, one IP."""
    tenant_id = uuid.uuid4()
    for i in range(3):
        limiter.check(
            tenant_id=tenant_id, visitor_session_id=f"visit-{i}", ip="203.0.113.7"
        )
    with pytest.raises(controls.RateLimited) as exc:
        limiter.check(
            tenant_id=tenant_id, visitor_session_id="one-more", ip="203.0.113.7"
        )
    assert exc.value.code == "ip_rate_limited"


def test_a_different_ip_is_unaffected_by_a_burnt_one(limiter) -> None:
    """One office behind a NAT must not exhaust capacity for another."""
    tenant_id = uuid.uuid4()
    for i in range(3):
        limiter.check(
            tenant_id=tenant_id, visitor_session_id=f"v{i}", ip="203.0.113.7"
        )
    limiter.check(
        tenant_id=tenant_id, visitor_session_id="other", ip="198.51.100.4"
    )


def test_the_visitor_limit_still_applies_below_the_ip_limit(limiter) -> None:
    tenant_id = uuid.uuid4()
    for _ in range(2):
        limiter.check(tenant_id=tenant_id, visitor_session_id="same", ip="1.1.1.1")
    with pytest.raises(controls.RateLimited) as exc:
        limiter.check(tenant_id=tenant_id, visitor_session_id="same", ip="1.1.1.1")
    assert exc.value.code == "visitor_rate_limited"


def test_an_unknown_ip_does_not_share_one_global_bucket(limiter) -> None:
    """`ip=None` must skip the scope, not key every such request on 'None'."""
    tenant_id = uuid.uuid4()
    for i in range(6):
        limiter.check(
            tenant_id=tenant_id, visitor_session_id=f"unknown-{i}", ip=None
        )


def test_ip_and_tenant_are_counted_even_when_the_visitor_trips_first(limiter) -> None:
    """A short-circuit would hide an abusive visitor from the tenant's count."""
    tenant_id = uuid.uuid4()
    for _ in range(2):
        limiter.check(tenant_id=tenant_id, visitor_session_id="same", ip="1.1.1.1")
    with pytest.raises(controls.RateLimited):
        limiter.check(tenant_id=tenant_id, visitor_session_id="same", ip="1.1.1.1")
    with limiter._lock:
        assert limiter._counts[("tenant", str(tenant_id))] == 3
        assert limiter._counts[("ip", "1.1.1.1")] == 3


def test_admin_limit_is_per_user(limiter) -> None:
    """Per-user, not per-tenant: one runaway script must not lock out a colleague."""
    first, second = uuid.uuid4(), uuid.uuid4()
    for _ in range(2):
        limiter.check_admin(user_id=first)
    with pytest.raises(controls.RateLimited) as exc:
        limiter.check_admin(user_id=first)
    assert exc.value.code == "user_rate_limited"
    limiter.check_admin(user_id=second)


def test_reset_clears_every_scope(limiter) -> None:
    tenant_id = uuid.uuid4()
    for _ in range(2):
        limiter.check(tenant_id=tenant_id, visitor_session_id="s", ip="1.1.1.1")
    limiter.reset()
    limiter.check(tenant_id=tenant_id, visitor_session_id="s", ip="1.1.1.1")


def test_a_non_ip_forwarded_header_cannot_500_an_audited_route() -> None:
    """A garbage X-Forwarded-For used to reach an `inet` column and raise.

    Any anonymous caller could trigger it on login, because the insert fails after
    the work is done and the response is a 500 rather than a 400. The header is
    attacker-controlled text, so it is validated where it is parsed.
    """
    from app.security.client_ip import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"not-an-ip")],
        "client": ("testclient", 1234),
    }
    monkey = Request(scope)
    os.environ["TRUST_PROXY_HEADERS"] = "true"
    try:
        # Falls back to the transport address, which is not an IP either here, so
        # the answer is None -- never the unvalidated string.
        assert client_ip(monkey) is None
    finally:
        os.environ.pop("TRUST_PROXY_HEADERS", None)


def test_a_valid_last_hop_is_still_returned_when_earlier_hops_are_junk() -> None:
    from app.security.client_ip import client_ip

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"junk, 198.51.100.9")],
        "client": ("10.0.0.1", 1234),
    }
    os.environ["TRUST_PROXY_HEADERS"] = "true"
    try:
        assert client_ip(Request(scope)) == "198.51.100.9"
    finally:
        os.environ.pop("TRUST_PROXY_HEADERS", None)
