"""WP-16: widget session tokens, domain verification, public §17.4 routes (WIDG-05).

The threat model, because "the bot id is not a secret" invites the wrong conclusion:
anyone can learn a public id from a customer's page source. What must *not* be
possible is using it from anywhere — so the defences tested here are origin binding,
signature integrity, expiry, and tenant scoping derived from the token.

The token tests are pure (no database, no app) and cover the forgery cases that
matter: a re-signed payload with a stale signature, an expired token, a token for
another bot, an `alg=none`-style payload swap, and the `null` origin from a
`file://` page. The route tests cover the cross-tenant and cross-bot attempts that
must fail.
"""

import os
import time
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import base64
import json

import pytest

from app.widget import tokens

from test_conversation import client, published_bot, stub_embedder, tenant  # noqa: F401

SECRET = "test-widget-secret-that-is-long-enough"


@pytest.fixture(autouse=True)
def widget_secret(monkeypatch):
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", SECRET)


# --- token issuing and verification ---------------------------------------


def test_token_round_trips_the_binding_facts() -> None:
    tenant_id, bot_id = uuid.uuid4(), uuid.uuid4()
    raw = tokens.issue(
        tenant_id=tenant_id, bot_id=bot_id, origin="https://shop.example.com"
    )
    token = tokens.verify(raw)
    assert token.tenant_id == tenant_id
    assert token.bot_id == bot_id
    assert token.origin == "https://shop.example.com"


def test_two_tokens_for_the_same_bot_are_distinct() -> None:
    """A nonce, so a log or cache cannot correlate two visits."""
    kwargs = dict(tenant_id=uuid.uuid4(), bot_id=uuid.uuid4(), origin="https://a.test")
    assert tokens.issue(**kwargs) != tokens.issue(**kwargs)


def test_tampered_payload_is_rejected() -> None:
    tenant_id, bot_id = uuid.uuid4(), uuid.uuid4()
    raw = tokens.issue(tenant_id=tenant_id, bot_id=bot_id, origin="https://a.test")
    payload, signature = raw.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["t"] = str(uuid.uuid4())
    forged = base64.urlsafe_b64encode(
        json.dumps(decoded, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    with pytest.raises(tokens.TokenError):
        tokens.verify(f"{forged}.{signature}")


def test_a_token_signed_with_another_key_is_rejected(monkeypatch) -> None:
    raw = tokens.issue(
        tenant_id=uuid.uuid4(), bot_id=uuid.uuid4(), origin="https://a.test"
    )
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", "a-completely-different-secret-value")
    with pytest.raises(tokens.TokenError):
        tokens.verify(raw)


def test_expired_token_is_rejected() -> None:
    raw = tokens.issue(
        tenant_id=uuid.uuid4(),
        bot_id=uuid.uuid4(),
        origin="https://a.test",
        ttl_seconds=10,
        now=time.time() - 60,
    )
    with pytest.raises(tokens.TokenError):
        tokens.verify(raw)


def test_malformed_tokens_are_rejected_without_raising_anything_else() -> None:
    for bad in ["", "one-part", "a.b.c", "!!!!.????", "abc.def"]:
        with pytest.raises(tokens.TokenError):
            tokens.verify(bad)


def test_an_unset_secret_disables_the_feature_instead_of_using_a_default(
    monkeypatch,
) -> None:
    """A shared fallback key would let anyone mint a token for any tenant."""
    monkeypatch.delenv("WIDGET_TOKEN_SECRET", raising=False)
    assert tokens.configured() is False
    with pytest.raises(tokens.TokenError):
        tokens.issue(tenant_id=uuid.uuid4(), bot_id=uuid.uuid4(), origin="https://a.test")


def test_a_short_secret_is_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", "tooshort")
    assert tokens.configured() is False


# --- origin normalisation and matching ------------------------------------


def test_normalise_origin_folds_case_port_and_path() -> None:
    assert (
        tokens.normalise_origin("HTTPS://WWW.Example.COM:443/checkout?x=1")
        == "https://www.example.com"
    )
    assert tokens.normalise_origin("http://example.com:80/") == "http://example.com"
    assert tokens.normalise_origin("https://example.com:8443/a") == (
        "https://example.com:8443"
    )


def test_null_origin_is_rejected() -> None:
    """A `file://` page or sandboxed iframe has no host to verify."""
    with pytest.raises(tokens.TokenError):
        tokens.normalise_origin("null")


def test_origin_matching_is_exact_and_has_no_wildcard() -> None:
    allowed = ["shop.example.com"]
    assert tokens.origin_matches("https://shop.example.com", allowed)
    assert tokens.origin_matches("https://SHOP.example.com", allowed)
    # A subdomain must not inherit the parent's permission: any user-content or
    # forgotten staging subdomain would otherwise impersonate the tenant.
    assert not tokens.origin_matches("https://evil.shop.example.com", allowed)
    assert not tokens.origin_matches("https://example.com", allowed)
    assert not tokens.origin_matches("https://shop.example.com.evil.test", allowed)


def test_a_bare_domain_entry_matches_https_only_by_default() -> None:
    """Owners type bare hosts; those normalise as https."""
    assert tokens.origin_matches("https://shop.example.com", ["shop.example.com"])


def test_blank_and_invalid_entries_do_not_match_everything() -> None:
    assert not tokens.origin_matches("https://a.test", ["", "   ", "not a url"])


def test_http_and_https_are_distinct_origins() -> None:
    assert not tokens.origin_matches("http://shop.example.com", ["shop.example.com"])


# --- routes ---------------------------------------------------------------


def _add_domain(tenant_id: str, bot_id: str, domain: str = "https://shop.example.com") -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant_id)) as session:
        session.execute(
            text(
                "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)"
                " VALUES (:t, :b, :d, true)"
                " ON CONFLICT (bot_id, domain) DO NOTHING"
            ),
            {"t": tenant_id, "b": bot_id, "d": domain},
        )
        session.commit()


def _session(client, public_id: str, origin: str | None = "https://shop.example.com"):
    headers = {"Origin": origin} if origin else {}
    return client.post(
        "/api/v1/widget/session",
        json={"bot_public_id": public_id},
        headers=headers,
    )


def test_session_mints_a_token_on_an_allowed_origin(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    response = _session(client, published_bot["public_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token"]
    assert body["bot"]["name"]
    assert body["expires_in"] > 0
    assert "tenant_id" not in json.dumps(body["bot"]), (
        "the public config must not leak the tenant id"
    )
    token = tokens.verify(body["token"])
    assert str(token.bot_id) == published_bot["bot_id"]


def test_session_is_refused_from_a_disallowed_origin(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    response = _session(client, published_bot["public_id"], origin="https://evil.test")
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "origin_not_allowed"


def test_session_is_refused_without_an_origin(client, tenant, published_bot) -> None:
    """No Origin means a non-browser client; there is nothing to verify."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    response = _session(client, published_bot["public_id"], origin=None)
    assert response.status_code == 403


def test_session_is_refused_when_no_domains_are_configured(
    client, tenant, published_bot
) -> None:
    response = _session(client, published_bot["public_id"])
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "no_allowed_domains"


def test_unknown_public_id_is_a_404(client, tenant) -> None:
    response = _session(client, "no-such-bot-public-id")
    assert response.status_code == 404


def test_session_creates_no_conversation_row(client, tenant, published_bot) -> None:
    """Opening the page must leave no trace — consent-by-design (§12.2, R41)."""
    from sqlalchemy import text

    from app.db.session import tenant_session

    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        before = session.execute(
            text("SELECT count(*) FROM conversations WHERE tenant_id = :t"),
            {"t": tenant["tenant_id"]},
        ).scalar_one()
    assert _session(client, published_bot["public_id"]).status_code == 200
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        after = session.execute(
            text("SELECT count(*) FROM conversations WHERE tenant_id = :t"),
            {"t": tenant["tenant_id"]},
        ).scalar_one()
    assert after == before


def test_message_requires_a_token(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    response = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": "v1"},
        headers={"Origin": "https://shop.example.com"},
    )
    assert response.status_code == 401


def test_widget_disabled_when_secret_is_unset(
    client, tenant, published_bot, monkeypatch
) -> None:
    """Fail closed: an unset secret must not mean 'no checking'."""
    monkeypatch.delenv("WIDGET_TOKEN_SECRET", raising=False)
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    response = _session(client, published_bot["public_id"])
    assert response.status_code == 503


def test_token_for_one_bot_cannot_open_another_bots_conversation(
    client, tenant, published_bot
) -> None:
    """A token for bot A must not become a proxy for bot B in the same tenant."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    started = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": "v1"},
        headers={
            "Origin": "https://shop.example.com",
            "Authorization": f"Bearer {body['token']}",
        },
    )
    assert started.status_code == 201, started.text
    conversation_id = started.json()["conversation_id"]

    other_bot = uuid.uuid4()
    forged = tokens.issue(
        tenant_id=uuid.UUID(tenant["tenant_id"]),
        bot_id=other_bot,
        origin="https://shop.example.com",
    )
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/messages",
        json={"text": "hello"},
        headers={
            "Origin": "https://shop.example.com",
            "Authorization": f"Bearer {forged}",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "token_bot_mismatch"


def test_widget_turn_reuses_the_owner_turn_loop(
    client, tenant, published_bot, stub_embedder
) -> None:
    """The §17.4 path is an adapter: same events, same grounding, one implementation."""
    from test_conversation import FakeProvider, _events

    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    headers = {
        "Origin": "https://shop.example.com",
        "Authorization": f"Bearer {body['token']}",
    }
    started = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": "v1"},
        headers=headers,
    )
    conversation_id = started.json()["conversation_id"]

    from app.widget import router as widget_router

    # The deployment has no embedding model installed and no provider key, so both
    # are injected: this tests the route's wiring, not the vendor.
    original_provider = widget_router.default_provider
    original_embedder = widget_router.default_embedder
    widget_router.default_provider = lambda: FakeProvider(reply="3-5 business days.")
    widget_router.default_embedder = lambda: stub_embedder
    try:
        response = client.post(
            f"/api/v1/widget/conversations/{conversation_id}/messages",
            json={"text": "How long does standard shipping take?"},
            headers=headers,
        )
    finally:
        widget_router.default_provider = original_provider
        widget_router.default_embedder = original_embedder

    assert response.status_code == 200
    events = _events(response)
    assert events[-1][0] == "final"
    assert events[-1][1]["citations"], "the widget path must cite too"


def test_cross_tenant_conversation_is_invisible_to_a_widget_token(
    client, tenant, published_bot
) -> None:
    """A token scoped to tenant A must not reach tenant B's conversation id."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    other_conversation = str(uuid.uuid4())
    response = client.post(
        f"/api/v1/widget/conversations/{other_conversation}/messages",
        json={"text": "hello"},
        headers={
            "Origin": "https://shop.example.com",
            "Authorization": f"Bearer {body['token']}",
        },
    )
    assert response.status_code == 404


def test_feedback_over_the_widget_path(client, tenant, published_bot) -> None:
    from test_conversation import FakeProvider, _run_turn, _start

    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    headers = {
        "Origin": "https://shop.example.com",
        "Authorization": f"Bearer {body['token']}",
    }
    conversation_id = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=FakeProvider(reply="3-5 days."),
    )
    message_id = events[-1][1]["message_id"]

    response = client.post(
        f"/api/v1/widget/messages/{message_id}/feedback",
        json={"rating": "up"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["rating"] == "up"


def test_widget_feedback_rejects_a_bad_rating(client, tenant, published_bot) -> None:
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    headers = {
        "Origin": "https://shop.example.com",
        "Authorization": f"Bearer {body['token']}",
    }
    response = client.post(
        f"/api/v1/widget/messages/{uuid.uuid4()}/feedback",
        json={"rating": "sideways"},
        headers=headers,
    )
    assert response.status_code == 422


# --- visitor uploads (WIDG-14) --------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 64


def test_sniff_identifies_allowed_formats_by_magic_bytes() -> None:
    from app.widget import uploads

    assert uploads.sniff(PNG) == "image/png"
    assert uploads.sniff(PDF) == "application/pdf"
    assert uploads.sniff(JPEG) == "image/jpeg"
    assert uploads.sniff(WEBP) == "image/webp"


def test_sniff_rejects_html_even_when_it_is_named_a_png() -> None:
    """The filename and the client Content-Type are both attacker-controlled."""
    from app.widget import uploads

    assert uploads.sniff(b"<html><script>alert(1)</script>") is None
    assert uploads.looks_like_html(b"  <!DOCTYPE html><p>x")


def test_sniff_does_not_accept_a_marker_that_is_not_at_offset_zero() -> None:
    from app.widget import uploads

    assert uploads.sniff(b"<!-- " + b"%PDF-1.7\n") is None


def test_validate_enforces_the_cap_before_sniffing() -> None:
    from app.widget import uploads

    with pytest.raises(uploads.UploadRejected) as exc:
        uploads.validate(PNG + b"\x00" * 100, max_bytes=50)
    assert exc.value.code == "file_too_large"
    assert exc.value.status_code == 413


def test_validate_rejects_an_empty_file() -> None:
    from app.widget import uploads

    with pytest.raises(uploads.UploadRejected) as exc:
        uploads.validate(b"", max_bytes=100)
    assert exc.value.code == "empty_file"


def test_storage_key_does_not_contain_the_visitor_filename() -> None:
    """A filename is untrusted text; a path built from it invites traversal."""
    from app.widget import uploads

    key = uploads.storage_key(tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4())
    assert ".." not in key
    assert key.count("/") == 2


def _widget_conversation(client, tenant, published_bot):
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    headers = {
        "Origin": "https://shop.example.com",
        "Authorization": f"Bearer {body['token']}",
    }
    started = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": "v1"},
        headers=headers,
    )
    return started.json()["conversation_id"], headers


def test_upload_accepts_a_png_and_records_the_sniffed_type(
    client, tenant, published_bot
) -> None:
    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=shot.png",
        content=PNG,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["mime"] == "image/png"
    assert response.json()["size_bytes"] == len(PNG)


def test_upload_records_the_sniffed_type_not_the_declared_one(
    client, tenant, published_bot
) -> None:
    """A PDF named .png is stored as a PDF; the extension is display only."""
    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=lie.png",
        content=PDF,
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["mime"] == "application/pdf"


def test_upload_rejects_an_html_file_renamed_to_png(
    client, tenant, published_bot
) -> None:
    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=innocent.png",
        content=b"<html><script>alert(1)</script></html>",
        headers=headers,
    )
    assert response.status_code == 415
    assert response.json()["detail"]["error"]["code"] == "unsupported_format"


def test_upload_over_the_cap_is_refused(client, tenant, published_bot, monkeypatch) -> None:
    from app.widget import router as widget_router

    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    monkeypatch.setattr(widget_router, "VISITOR_MAX_UPLOAD_BYTES", 32)
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=big.png",
        content=PNG,
        headers=headers,
    )
    assert response.status_code == 413


def test_upload_requires_a_token(client, tenant, published_bot) -> None:
    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    without = {k: v for k, v in headers.items() if k != "Authorization"}
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=a.png",
        content=PNG,
        headers=without,
    )
    assert response.status_code == 401


def test_upload_to_another_tenants_conversation_is_a_404(
    client, tenant, published_bot
) -> None:
    from app.widget import tokens as token_module

    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    forged = token_module.issue(
        tenant_id=uuid.uuid4(),
        bot_id=uuid.UUID(published_bot["bot_id"]),
        origin="https://shop.example.com",
    )
    conversation_id, _ = _widget_conversation(client, tenant, published_bot)
    response = client.post(
        f"/api/v1/widget/conversations/{conversation_id}/files?filename=a.png",
        content=PNG,
        headers={
            "Origin": "https://shop.example.com",
            "Authorization": f"Bearer {forged}",
        },
    )
    assert response.status_code == 404


# --- verified domains only (§16.1, §27) -----------------------------------

def _add_unverified_domain(tenant_id: str, bot_id: str, domain: str) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant_id)) as session:
        session.execute(
            text(
                "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)"
                " VALUES (:t, :b, :d, false)"
                " ON CONFLICT (bot_id, domain) DO UPDATE SET verified = false"
            ),
            {"t": tenant_id, "b": bot_id, "d": domain},
        )
        session.commit()


def test_an_unverified_domain_must_not_mint_a_token(client, tenant, published_bot) -> None:
    """§16.1: "widget only serves on verified domains"; §27: refuses unverified
    origins. An unverified row is the owner's claim, not a proven control."""
    _add_unverified_domain(
        tenant["tenant_id"], published_bot["bot_id"], "https://shop.example.com"
    )
    response = _session(client, published_bot["public_id"])
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "no_allowed_domains"


def test_an_unverified_domain_does_not_authorise_an_existing_token(
    client, tenant, published_bot
) -> None:
    """Verification is re-read on every use, so revoking it takes effect at once."""
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    body = _session(client, published_bot["public_id"]).json()
    headers = {
        "Origin": "https://shop.example.com",
        "Authorization": f"Bearer {body['token']}",
    }
    assert (
        client.post(
            "/api/v1/widget/conversations",
            json={"visitor_session_id": "v1"},
            headers=headers,
        ).status_code
        == 201
    )

    _add_unverified_domain(
        tenant["tenant_id"], published_bot["bot_id"], "https://shop.example.com"
    )
    response = client.post(
        "/api/v1/widget/conversations",
        json={"visitor_session_id": "v2"},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "no_allowed_domains"


def test_uploads_are_rate_limited(client, tenant, published_bot) -> None:
    """The upload route accepts 5 MB per request from an unauthenticated visitor.

    It had no limiter when WP-16 shipped: every other widget route had one, and a
    live run of twelve uploads all returned 201. Found by exercising the deployed
    stack, not by reading the code — so it gets a test that fails without the fix.
    """
    from app.conversation import controls

    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    original = controls._limiter
    controls._limiter = controls.RateLimiter()
    controls._limiter.visitor_limit = 2
    try:
        statuses = [
            client.post(
                f"/api/v1/widget/conversations/{conversation_id}/files?filename=s{i}.png",
                content=PNG,
                headers=headers,
            ).status_code
            for i in range(3)
        ]
    finally:
        controls._limiter = original

    assert statuses[:2] == [201, 201], statuses
    assert statuses[2] == 429, statuses


def test_the_rate_limit_is_checked_before_the_body_is_read(
    client, tenant, published_bot
) -> None:
    """A refusal that happens after buffering 5 MB is still a memory amplifier."""
    from app.conversation import controls

    conversation_id, headers = _widget_conversation(client, tenant, published_bot)
    original = controls._limiter
    controls._limiter = controls.RateLimiter()
    controls._limiter.visitor_limit = 0
    try:
        response = client.post(
            f"/api/v1/widget/conversations/{conversation_id}/files?filename=big.png",
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000,
            headers=headers,
        )
    finally:
        controls._limiter = original

    assert response.status_code == 429, response.text
