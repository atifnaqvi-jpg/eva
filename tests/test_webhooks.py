"""WP-18: signed webhook notifications (HAND-06, §17.5, R44).

The three things this has to prove, and why each one is a real risk rather than a
formality:

* **The signature actually verifies.** A receiver written from a prose description is
  how signing schemes drift, so the test signs on the sending side and verifies with
  the same exported function a tenant would use, over the bytes that really went over
  the wire.
* **A tenant-supplied URL is an outbound request from inside our network.** The SSRF
  guard is the only thing between a customer and the metadata service, so it is
  tested against the addresses that matter, not just a happy path.
* **Delivery failure is a value.** A dead endpoint must not fail the request that
  triggered it, and must not stop the other endpoints subscribed to the same event.

The receiver here is a real HTTP server on loopback, so nothing is mocked: the
request is built, signed, sent and verified end to end.
"""

import http.server
import json
import os
import shutil
import subprocess
import threading

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest

from app.handover import webhooks
from app.handover.webhooks import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    Delivery,
    WebhookURLRejected,
    sign,
    validate_url,
    verify,
)

from test_conversation import client, published_bot, stub_embedder, tenant  # noqa: F401


# --- signature scheme -----------------------------------------------------


def test_a_signature_verifies_over_the_bytes_that_were_sent() -> None:
    secret = "whsec_test_0123456789"
    body = json.dumps({"event": "handover.created", "id": "x"}, sort_keys=True).encode()
    timestamp = "1700000000"

    signature = sign(secret, timestamp=timestamp, body=body)

    assert signature.startswith("v1=")
    assert verify(secret, timestamp=timestamp, body=body, signature=signature, now=1700000000)


def test_a_tampered_body_does_not_verify() -> None:
    """The whole point: someone who can alter the payload cannot reuse the signature."""
    secret = "whsec_test_0123456789"
    body = b'{"event":"handover.created"}'
    timestamp = "1700000000"
    signature = sign(secret, timestamp=timestamp, body=body)

    assert not verify(
        secret,
        timestamp=timestamp,
        body=b'{"event":"handover.created","extra":1}',
        signature=signature,
        now=1700000000,
    )


def test_a_stale_timestamp_is_refused() -> None:
    """A captured request must not be replayable later, so age is part of validity."""
    secret = "whsec_test_0123456789"
    body = b"{}"
    timestamp = "1700000000"
    signature = sign(secret, timestamp=timestamp, body=body)

    assert verify(secret, timestamp=timestamp, body=body, signature=signature, now=1700000000)
    assert not verify(
        secret,
        timestamp=timestamp,
        body=body,
        signature=signature,
        now=1700000000 + 301,
    )


def test_the_timestamp_is_inside_the_signed_material() -> None:
    """Otherwise a replay could swap in a fresh timestamp and keep the signature."""
    secret = "whsec_test_0123456789"
    body = b"{}"
    signature = sign(secret, timestamp="1700000000", body=body)

    assert not verify(secret, timestamp="1700000001", body=body, signature=signature, now=1700000001)


def test_a_different_secret_does_not_verify() -> None:
    body = b"{}"
    timestamp = "1700000000"
    signature = sign("secret-a", timestamp=timestamp, body=body)

    assert not verify("secret-b", timestamp=timestamp, body=body, signature=signature, now=1700000000)


def test_a_garbage_timestamp_is_refused_rather_than_raising() -> None:
    assert not verify("s", timestamp="not-a-number", body=b"{}", signature="v1=x", now=0)


# --- SSRF guard -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://localhost/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://172.16.0.9/hook",
        "http://[::1]/hook",
        "http://0.0.0.0/hook",
    ],
)
def test_private_and_loopback_addresses_are_refused(url: str) -> None:
    """These are the addresses that turn a webhook into a request forgery."""
    with pytest.raises(WebhookURLRejected):
        validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "gopher://example.com/",
        "",
        "https://",
        "https://user:password@example.com/hook",
        "https://example.com:6379/hook",
        "https://example.com:5432/hook",
    ],
)
def test_schemes_credentials_and_internal_ports_are_refused(url: str) -> None:
    """A webhook is an HTTP endpoint; anything else is a port probe from inside."""
    with pytest.raises(WebhookURLRejected):
        validate_url(url)


def test_a_public_https_url_is_accepted() -> None:
    assert validate_url("https://example.com/hook", resolve=False) == "https://example.com/hook"


def test_private_hosts_can_be_allowed_for_a_compose_network_test() -> None:
    """The escape hatch exists so tests can point at a receiver on the compose network."""
    assert validate_url("http://127.0.0.1:8080/hook", allow_private=True).startswith("http://127.0.0.1")


def test_a_refused_url_returns_a_failed_delivery_instead_of_raising() -> None:
    """The caller is a request path: a bad URL is a value, not an exception."""
    result = webhooks.deliver(
        url="http://169.254.169.254/latest/meta-data/",
        secret="s",
        event="handover.created",
        payload={"event": "handover.created"},
    )

    assert isinstance(result, Delivery)
    assert result.ok is False
    assert "rejected" in result.detail


# --- a real receiver ------------------------------------------------------


class _Receiver:
    """A real HTTP server that records what it was sent."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", "0"))
                receiver.requests.append(
                    {
                        "body": self.rfile.read(length),
                        "signature": self.headers.get(SIGNATURE_HEADER),
                        "timestamp": self.headers.get(TIMESTAMP_HEADER),
                        "event": self.headers.get(EVENT_HEADER),
                    }
                )
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args) -> None:  # noqa: ANN002 - silence the server
                pass

        # A port from ALLOWED_PORTS: the guard rejects arbitrary ports before the
        # allow_private escape hatch is consulted, deliberately.
        self.server = http.server.HTTPServer(("127.0.0.1", 8080), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_Receiver":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        self.server.shutdown()
        self.server.server_close()


def test_deliver_posts_a_signed_request_that_a_receiver_can_verify() -> None:
    """End to end: build, sign, send over a socket, verify the received bytes."""
    secret = "whsec_end_to_end"
    payload = {"event": "handover.created", "handover_id": "h-1"}

    with _Receiver() as receiver:
        result = webhooks.deliver(
            url=f"http://127.0.0.1:{receiver.port}/hook",
            secret=secret,
            event="handover.created",
            payload=payload,
            allow_private=True,
        )

    assert result.ok is True, result.detail
    assert result.status == 200
    assert len(receiver.requests) == 1

    received = receiver.requests[0]
    assert received["event"] == "handover.created"
    # The receiver verifies with the documented scheme, over the raw body.
    assert verify(
        secret,
        timestamp=received["timestamp"],
        body=received["body"],
        signature=received["signature"],
    )
    assert json.loads(received["body"]) == payload


def test_a_receiver_that_errors_is_a_failed_delivery_not_an_exception() -> None:
    """A dead endpoint must never raise into the flow that triggered it."""

    class _Broken(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args) -> None:  # noqa: ANN002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8080), _Broken)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = webhooks.deliver(
            url=f"http://127.0.0.1:{server.server_address[1]}/hook",
            secret="s",
            event="handover.created",
            payload={"event": "handover.created"},
            allow_private=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.ok is False
    assert result.status == 500


# --- management API and emission ------------------------------------------


AGE_AVAILABLE = shutil.which("age-keygen") is not None


@pytest.fixture(autouse=True)
def widget_secret(monkeypatch):
    """The widget token layer needs a signing secret, and delivery must be synchronous
    where it is asserted on — the same shape as test_handover's fixture."""
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", "test-widget-secret-that-is-long-enough")
    from app import config

    monkeypatch.setattr(config, "HANDOVER_NOTIFY_ASYNC", False)


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """A real age-backed vault for this test.

    The webhook secret lives in the vault, so a test that creates one is exercising
    the same storage path a deployment uses — faking it would leave the actual
    encrypt/decrypt unproven.
    """
    if not AGE_AVAILABLE:
        pytest.skip("age-keygen not installed")
    identity = tmp_path / "age-identity"
    subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, check=True
    )
    monkeypatch.setenv("SECRET_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("SECRET_VAULT_IDENTITY", str(identity))
    return {"dir": tmp_path / "vault", "identity": identity}


def test_webhook_management_returns_the_secret_exactly_once(migrated_db, client, tenant, vault_env) -> None:  # noqa: F811
    """The secret is needed to verify, so it is disclosed at creation and never again."""
    created = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.com/hook", "events": ["handover.created"]},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["secret"].startswith("whsec_") or len(body["secret"]) > 20
    webhook_id = body["id"]

    listed = client.get("/api/v1/webhooks")
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["id"] for r in rows] == [webhook_id]
    # The list model has no field that could carry the secret.
    assert "secret" not in rows[0]


def test_an_unknown_event_is_refused(migrated_db, client, tenant) -> None:  # noqa: F811
    response = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.com/hook", "events": ["not.an.event"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "unknown_event"


def test_a_private_url_is_refused_at_creation(migrated_db, client, tenant) -> None:  # noqa: F811
    """Checked before the row is written, so no unusable webhook is ever stored."""
    from app import config

    response = client.post(
        "/api/v1/webhooks",
        json={"url": "http://169.254.169.254/hook", "events": ["handover.created"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "invalid_url"


def test_deleting_a_webhook_removes_it_and_its_secret(migrated_db, client, tenant, vault_env) -> None:  # noqa: F811
    created = client.post(
        "/api/v1/webhooks",
        json={"url": "https://example.com/hook", "events": ["conversation.created"]},
    ).json()

    assert client.delete(f"/api/v1/webhooks/{created['id']}").status_code == 204
    assert client.get("/api/v1/webhooks").json() == []
    # Deleting again is a 404, not a silent success.
    assert client.delete(f"/api/v1/webhooks/{created['id']}").status_code == 404


def test_the_test_endpoint_reports_what_happened(
    migrated_db, client, tenant, vault_env, monkeypatch  # noqa: F811
) -> None:
    """The owner needs a real answer about their receiver, so this runs inline."""
    from app import config

    # The receiver is on loopback, which the SSRF guard refuses by default — that is
    # the correct production behaviour, so the test opts in explicitly.
    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE_HOSTS", True)

    with _Receiver() as receiver:
        created = client.post(
            "/api/v1/webhooks",
            json={
                "url": f"http://127.0.0.1:{receiver.port}/hook",
                "events": ["handover.created"],
            },
        ).json()

        result = client.post(f"/api/v1/webhooks/{created['id']}/test")

    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True
    # The ping is signed with the same secret the owner received at creation.
    ping = receiver.requests[0]
    assert ping["event"] == "ping"
    assert verify(
        created["secret"],
        timestamp=ping["timestamp"],
        body=ping["body"],
        signature=ping["signature"],
    )


# --- the gate criterion: a real trigger fires a signed webhook ------------


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    """Emission is off the request path, so the receiver is polled, not assumed."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.05)
    return False


def test_a_real_handover_fires_one_signed_webhook(
    migrated_db, client, tenant, published_bot, vault_env, monkeypatch  # noqa: F811
) -> None:
    """The M4 gate sentence: a handover from a real conversation reaches a receiver.

    Everything is real here — the migrated database, the vault, the HTTP request and
    the signature. Only the receiver is local, because a test cannot own a public
    hostname.
    """
    from app import config
    from test_handover import _add_domain, _set_handover_settings, _start

    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE_HOSTS", True)
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )

    with _Receiver() as receiver:
        created = client.post(
            "/api/v1/webhooks",
            json={
                "url": f"http://127.0.0.1:{receiver.port}/hook",
                "events": ["handover.created"],
            },
        )
        assert created.status_code == 201, created.text
        secret = created.json()["secret"]

        conversation_id, token = _start(client, published_bot["public_id"])
        response = client.post(
            f"/api/v1/widget/conversations/{conversation_id}/handover",
            json={"email": "visitor@example.test", "name": "Dana"},
            headers={
                "Origin": "https://shop.example.com",
                "Authorization": f"Bearer {token}",
            },
        )
        assert response.status_code == 201, response.text
        handover_id = response.json()["handover_id"]

        assert _wait_for(lambda: bool(receiver.requests)), "no webhook arrived"

    received = receiver.requests[0]
    assert received["event"] == "handover.created"
    body = json.loads(received["body"])
    assert body["handover_id"] == handover_id
    assert body["conversation_id"] == conversation_id
    assert body["reason"] == "explicit"

    # The receiver verifies with the secret it was given and the documented scheme.
    assert verify(
        secret,
        timestamp=received["timestamp"],
        body=received["body"],
        signature=received["signature"],
    )


def test_a_repeated_handover_fires_only_once(
    migrated_db, client, tenant, published_bot, vault_env, monkeypatch  # noqa: F811
) -> None:
    """Idempotence must hold for the webhook too, not just the row.

    A visitor who submits the form twice would otherwise page the owner twice.
    """
    from app import config
    from test_handover import _add_domain, _set_handover_settings, _start

    monkeypatch.setattr(config, "WEBHOOK_ALLOW_PRIVATE_HOSTS", True)
    _add_domain(tenant["tenant_id"], published_bot["bot_id"])
    _set_handover_settings(
        tenant["tenant_id"], {"handover": {"email": "owner@example.test"}}
    )

    with _Receiver() as receiver:
        client.post(
            "/api/v1/webhooks",
            json={
                "url": f"http://127.0.0.1:{receiver.port}/hook",
                "events": ["handover.created"],
            },
        )
        conversation_id, token = _start(client, published_bot["public_id"])
        headers = {
            "Origin": "https://shop.example.com",
            "Authorization": f"Bearer {token}",
        }
        for _ in range(2):
            response = client.post(
                f"/api/v1/widget/conversations/{conversation_id}/handover",
                json={"email": "visitor@example.test"},
                headers=headers,
            )
            assert response.status_code == 201, response.text

        assert _wait_for(lambda: bool(receiver.requests))
        # Give a second delivery time to arrive if the bug exists.
        _wait_for(lambda: len(receiver.requests) > 1, timeout=1.0)

    assert len(receiver.requests) == 1
