"""SEC-15: domain ownership verification by DNS TXT record.

What this file has to prove, in the order the code could fail:

1. **A domain is not allowed because it was added.** The row is inserted unverified and
   the widget's own verified-only filter keeps refusing that origin until the record
   matches. Asserted through `POST /widget/session`, the endpoint the loader calls, so
   the test cannot pass while the product denies the visitor.
2. **Verification reads DNS for real.** A UDP server on the loopback interface answers
   the query; nothing replaces `socket.sendto`/`recvfrom` or the parser, so a broken
   packet builder or a mis-read label fails the test rather than being skipped by a mock.
   Only the *address* of the resolver is substituted — `query_txt` takes one, and it
   carries a port only in tests.
3. **"Could not ask" never reads as "no record".** Truncation, SERVFAIL, a mismatched
   query id and an unreachable resolver each raise, and the route maps them to 503 while
   a genuine missing record is a 200 with the reason. Getting this backwards sends an
   owner to edit a record that was already correct.
4. **One tenant cannot touch another's domains**, in either direction, and a refused
   attempt leaves no state behind.
5. **Every change is audited**, without the token in the append-only table.

Fixtures are synthetic per the confidentiality charter.
"""

import json
import os

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import socket
import struct
import threading
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config
from app.widget import domains

from test_conversation import (  # noqa: F401  (module-scoped app/db fixtures)
    client,
    published_bot,
    stub_embedder,
    tenant,
)

PASSWORD = "correct-horse-battery"
FIXTURE = "domver-"

# `tokens.configured()` fails closed without it, so the two tests that drive the widget
# would see 503 for the wrong reason. Same value `test_widget_tokens.py` uses.
WIDGET_SECRET = "test-widget-secret-that-is-long-enough"


@pytest.fixture(autouse=True)
def widget_secret(monkeypatch):
    monkeypatch.setenv("WIDGET_TOKEN_SECRET", WIDGET_SECRET)


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def _purge_tenant(migrated_db: str, tenant_id: str) -> None:
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute("ALTER TABLE audit_logs DISABLE TRIGGER USER")
        conn.execute("DELETE FROM audit_logs WHERE tenant_id = %s", (tenant_id,))
        conn.execute("ALTER TABLE audit_logs ENABLE TRIGGER USER")
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def _register(client: TestClient) -> dict:
    email = f"{FIXTURE}{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": "Domain Verify"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    body["email"] = email
    return body


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no session cookie")


def _as(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set("eva_session", token)


# --- a real DNS server ----------------------------------------------------


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\x00"


def _read_question_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    while True:
        length = data[offset]
        offset += 1
        if length == 0:
            break
        labels.append(data[offset : offset + length].decode())
        offset += length
    return ".".join(labels), offset


class FakeResolver:
    """A UDP server that speaks just enough DNS to be answered for real.

    Deliberately a socket, not a monkeypatched `query_txt`: the packet builder, the
    length-prefixed TXT reader and the compression-pointer handling are the parts most
    likely to be wrong, and a stub above them would prove none of them.

    `mode` selects the failure to inject: ``ok`` answers, ``empty`` returns NODATA,
    ``nxdomain`` rcode 3, ``servfail`` rcode 2, ``truncated`` sets TC, ``wrong_id``
    echoes a different query id, ``forward_pointer`` points a name pointer forward.
    """

    def __init__(self, answers: dict[str, list[str]], mode: str = "ok") -> None:
        self.answers = answers
        self.mode = mode
        self.queries: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeResolver":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def address(self) -> str:
        """The `host:port` form `query_txt` accepts. Production passes a bare host."""
        return f"127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            try:
                self._sock.sendto(self._reply(data), peer)
            except OSError:
                continue

    def _reply(self, data: bytes) -> bytes:
        query_id, _flags, _qdcount = struct.unpack("!HHH", data[:6])
        name, offset = _read_question_name(data, 12)
        self.queries.append(name)
        question = data[12 : offset + 4]

        if self.mode == "wrong_id":
            query_id = (query_id + 1) % 65536
        if self.mode == "truncated":
            return struct.pack("!HHHHHH", query_id, 0x0380, 1, 0, 0, 0) + question
        if self.mode == "forward_pointer":
            # An answer whose owner name points at a later offset: a reader without the
            # backwards-only guard loops or reads whatever follows.
            answer_at = 12 + len(question)
            later = answer_at + 16
            return (
                struct.pack("!HHHHHH", query_id, 0x8180, 1, 2, 0, 0)
                + question
                + struct.pack("!H", 0xC000 | later)
                + struct.pack("!HHIH", 16, 1, 60, 0)
                + struct.pack("!H", 0xC000 | (answer_at + 2))
                + struct.pack("!HHIH", 16, 1, 60, 0)
            )
        if self.mode in ("nxdomain", "servfail"):
            rcode = 3 if self.mode == "nxdomain" else 2
            return struct.pack("!HHHHHH", query_id, 0x8180 | rcode, 1, 0, 0, 0) + question

        values = [] if self.mode == "empty" else self.answers.get(name, [])
        records = b""
        for value in values:
            payload = bytes([len(value)]) + value.encode()
            records += (
                b"\xc0\x0c"  # the question name, compressed
                + struct.pack("!HHIH", 16, 1, 60, len(payload))
                + payload
            )
        return (
            struct.pack("!HHHHHH", query_id, 0x8180, 1, len(values), 0, 0)
            + question
            + records
        )


@pytest.fixture
def answering(monkeypatch):
    """Point the API's lookup at a loopback resolver, and return a factory for one."""

    def _make(answers: dict[str, list[str]], mode: str = "ok") -> FakeResolver:
        server = FakeResolver(answers, mode)
        server.__enter__()
        monkeypatch.setattr(config, "DNS_RESOLVER", server.address())
        return server

    yield _make
    # `FakeResolver.__exit__` is called by the test that owns the server, so a failing
    # assertion cannot leave a thread behind; the daemon flag is the backstop.


# --- pure: names, tokens, matching ---------------------------------------


def test_record_name_is_prefixed_below_the_host() -> None:
    assert domains.record_name("shop.example.com") == "_eva-verify.shop.example.com"


def test_record_name_accepts_what_an_owner_types() -> None:
    """A bare host, a full origin and a pasted URL all name the same record."""
    expected = "_eva-verify.shop.example.com"
    assert domains.record_name("shop.example.com") == expected
    assert domains.record_name("https://shop.example.com") == expected
    assert domains.record_name("https://Shop.Example.com/about?x=1") == expected


def test_record_name_refuses_a_name_the_widget_would_never_match() -> None:
    """One normaliser, not two: a `http://` origin cannot match, so it cannot verify."""
    with pytest.raises(domains.DomainError):
        domains.record_name("ftp://files.example.com")


def test_tokens_are_unique_and_hex() -> None:
    tokens = {domains.new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) == 32 and all(c in "0123456789abcdef" for c in t) for t in tokens)


def test_matching_requires_the_marker_and_the_whole_value() -> None:
    token = domains.new_token()
    assert domains.matches([f"eva-verify={token}"], token)
    # Case-insensitive: some panels upper-case the value.
    assert domains.matches([f"EVA-VERIFY={token.upper()}"], token)
    # A bare token is not the documented form, so an unrelated provider record that
    # happens to contain it is not accepted.
    assert not domains.matches([token], token)
    # A superstring is not a match either.
    assert not domains.matches([f"eva-verify={token}extra"], token)
    assert not domains.matches(["eva-verify=someone-elses-token"], token)
    assert not domains.matches([], token)


def test_a_second_record_at_the_same_name_still_verifies() -> None:
    """Providers commonly add their own TXT values; one match is enough."""
    token = domains.new_token()
    values = ["v=spf1 -all", f"eva-verify={token}", "google-site-verification=abc"]
    assert domains.matches(values, token)


@pytest.mark.parametrize(
    "entry",
    [
        # Not a public name: verifying it would make this endpoint an existence oracle
        # for services only reachable inside a network.
        "db.internal",
        "printer.local",
        "10.0.0.5.in-addr.arpa",
        "something.onion",
        # Single-label: no public record exists to publish.
        "intranet",
        # An IP has no name to publish a record under.
        "127.0.0.1",
        "https://192.168.1.10:8443",
    ],
)
def test_non_public_entries_are_refused(entry: str) -> None:
    with pytest.raises(domains.DomainError) as caught:
        domains.hostname(entry)
    assert caught.value.code == "invalid_domain"


# --- the real UDP path ---------------------------------------------------


def test_query_reads_a_txt_answer_from_a_real_socket(answering) -> None:
    server = answering({"_eva-verify.shop.example.com": ["eva-verify=abc123"]})
    assert domains.query_txt("_eva-verify.shop.example.com", server=server.address()) == [
        "eva-verify=abc123"
    ]
    assert server.queries == ["_eva-verify.shop.example.com"]


def test_query_returns_empty_for_nodata_and_nxdomain(answering) -> None:
    """A name with no TXT is an honest "no record", not a failure."""
    for mode in ("empty", "nxdomain"):
        server = answering({}, mode=mode)
        assert domains.query_txt("_eva-verify.shop.example.com", server=server.address()) == []
        server.__exit__(None, None, None)


@pytest.mark.parametrize("mode", ["truncated", "servfail", "wrong_id", "forward_pointer"])
def test_a_partial_or_wrong_answer_raises_rather_than_reporting_no_record(
    answering, mode
) -> None:
    """The defect this guards: reading a broken answer as an empty record list.

    A truncated reply, a mismatch on the query id and a forward-pointing compression
    pointer must each surface as `lookup_failed`. Reporting "no record" would tell an
    owner to fix DNS that is already correct — and, for the forged-id case, would let
    anything on the path decide that a verified domain is not.
    """
    server = answering({"_eva-verify.shop.example.com": ["eva-verify=abc"]}, mode=mode)
    with pytest.raises(domains.DomainError) as caught:
        domains.query_txt("_eva-verify.shop.example.com", server=server.address())
    assert caught.value.code == "lookup_failed"
    server.__exit__(None, None, None)


def test_an_unreachable_resolver_raises() -> None:
    """A closed port is "could not ask", never "no such record"."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # The port is closed by the `with` exit above: anything sent there is refused.
    with pytest.raises(domains.DomainError) as caught:
        domains.query_txt("_eva-verify.shop.example.com", server=f"127.0.0.1:{port}", timeout=0.5)
    assert caught.value.code == "lookup_failed"


def test_a_resolver_that_never_answers_times_out(answering) -> None:
    """A silent resolver is a failure, not a `no_txt_record` for the owner to chase."""
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    port = silent.getsockname()[1]
    try:
        with pytest.raises(domains.DomainError) as caught:
            domains.query_txt(
                "_eva-verify.shop.example.com", server=f"127.0.0.1:{port}", timeout=0.3
            )
        assert caught.value.code == "lookup_failed"
    finally:
        silent.close()


def test_verify_record_reports_a_missing_record_with_the_values_to_publish(answering) -> None:
    server = answering({}, mode="empty")
    with pytest.raises(domains.DomainError) as caught:
        domains.verify_record("shop.example.com", "abc123", server=server.address())
    assert caught.value.code == "no_txt_record"
    assert "_eva-verify.shop.example.com" in caught.value.message
    assert "eva-verify=abc123" in caught.value.message


def test_verify_record_rejects_a_record_carrying_another_token(answering) -> None:
    """A TXT record exists but says something else — distinct from "nothing there"."""
    server = answering({"_eva-verify.shop.example.com": ["eva-verify=not-mine"]})
    with pytest.raises(domains.DomainError) as caught:
        domains.verify_record("shop.example.com", "abc123", server=server.address())
    assert caught.value.code == "token_mismatch"


def test_verify_record_accepts_a_matching_record(answering) -> None:
    server = answering({"_eva-verify.shop.example.com": ["eva-verify=abc123"]})
    domains.verify_record("shop.example.com", "abc123", server=server.address())


# --- the routes ----------------------------------------------------------


@pytest.fixture
def owner(migrated_db: str, client: TestClient):
    """A registered owner, a session token, and the bot that registration created."""
    registered = _register(client)
    token = _login(client, registered["email"])
    _as(client, token)
    tenant_id = client.get("/api/v1/auth/me").json()["tenant_id"]
    bot = client.get("/api/v1/bots").json()[0]
    yield {"client": client, "tenant_id": tenant_id, "bot_id": bot["id"], "token": token}
    _purge_tenant(migrated_db, tenant_id)


def _add(owner, domain: str = "shop.example.com"):
    return owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains", json={"domain": domain}
    )


def _listed(owner) -> list[dict]:
    response = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}/domains")
    assert response.status_code == 200, response.text
    return response.json()


def _set_public_id(migrated_db: str, bot_id: str) -> str:
    public_id = f"bot-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute("UPDATE bots SET public_id = %s WHERE id = %s", (public_id, bot_id))
    return public_id


def test_adding_a_domain_stores_it_unverified(owner, migrated_db: str) -> None:
    response = _add(owner)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["verified"] is False
    assert body["record_name"] == "_eva-verify.shop.example.com"
    assert body["record_value"].startswith("eva-verify=")

    with psycopg.connect(_admin(migrated_db)) as conn:
        row = conn.execute(
            "SELECT domain, verified, verification_token FROM bot_domains"
            " WHERE tenant_id = %s",
            (owner["tenant_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == "shop.example.com", "stored as the host, not the pasted origin"
    assert row[1] is False
    assert body["record_value"] == f"eva-verify={row[2]}"


def test_the_widget_keeps_refusing_an_unverified_domain(
    owner, migrated_db: str, client: TestClient, answering
) -> None:
    """The property the whole row exists for.

    Adding a domain is the owner's claim. The widget's own verified-only filter must
    keep returning 403 for that origin until the record matches — asserted through
    `POST /widget/session`, which is what the loader calls.
    """
    public_id = _set_public_id(migrated_db, owner["bot_id"])
    _add(owner, "shop.example.com")

    denied = client.post(
        "/api/v1/widget/session",
        json={"bot_public_id": public_id},
        headers={"Origin": "https://shop.example.com"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["error"]["code"] == "no_allowed_domains"

    token = _listed(owner)[0]["record_value"].split("=", 1)[1]
    answering({"_eva-verify.shop.example.com": [f"eva-verify={token}"]})
    verified = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True

    allowed = client.post(
        "/api/v1/widget/session",
        json={"bot_public_id": public_id},
        headers={"Origin": "https://shop.example.com"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["token"]


def test_verify_with_no_record_is_a_200_carrying_the_reason(owner, answering) -> None:
    _add(owner)
    answering({}, mode="empty")
    response = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is False
    assert "_eva-verify.shop.example.com" in body["detail"]


def test_verify_with_a_wrong_token_stays_unverified(owner, migrated_db: str, answering) -> None:
    _add(owner)
    answering({"_eva-verify.shop.example.com": ["eva-verify=someone-else"]})
    response = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    assert response.status_code == 200
    assert response.json()["verified"] is False
    with psycopg.connect(_admin(migrated_db)) as conn:
        assert conn.execute(
            "SELECT verified FROM bot_domains WHERE tenant_id = %s",
            (owner["tenant_id"],),
        ).fetchone()[0] is False


def test_a_resolver_failure_is_503_and_never_changes_the_row(
    owner, migrated_db: str, answering
) -> None:
    """The defect this guards: showing an owner "your record is missing" when we could
    not read it, and marking a domain verified on the way past."""
    _add(owner)
    answering({}, mode="servfail")
    response = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"]["code"] == "lookup_failed"
    with psycopg.connect(_admin(migrated_db)) as conn:
        assert conn.execute(
            "SELECT verified FROM bot_domains WHERE tenant_id = %s",
            (owner["tenant_id"],),
        ).fetchone()[0] is False


def test_adding_the_same_domain_twice_keeps_one_token(owner, migrated_db: str) -> None:
    """Two live tokens for one domain means an old, forgotten record still verifies."""
    first = _add(owner).json()
    second = _add(owner).json()
    assert first["id"] == second["id"]
    assert first["record_value"] == second["record_value"]
    with psycopg.connect(_admin(migrated_db)) as conn:
        count = conn.execute(
            "SELECT count(*) FROM bot_domains WHERE tenant_id = %s",
            (owner["tenant_id"],),
        ).fetchone()[0]
    assert count == 1


def test_adding_an_internal_name_is_refused(owner) -> None:
    response = _add(owner, "db.internal")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "invalid_domain"


def test_listing_shows_verified_state_and_the_record_to_publish(owner) -> None:
    _add(owner, "shop.example.com")
    _add(owner, "www.shop.example.com")
    rows = _listed(owner)
    assert [r["domain"] for r in rows] == ["shop.example.com", "www.shop.example.com"]
    assert all(r["verified"] is False for r in rows)
    assert rows[1]["record_name"] == "_eva-verify.www.shop.example.com"
    # Distinct tokens: two domains must not share one proof of control.
    assert rows[0]["record_value"] != rows[1]["record_value"]


def test_deleting_a_domain_stops_the_widget_serving_it(
    owner, migrated_db: str, client: TestClient
) -> None:
    public_id = _set_public_id(migrated_db, owner["bot_id"])
    _add(owner, "shop.example.com")
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute(
            "UPDATE bot_domains SET verified = true WHERE tenant_id = %s",
            (owner["tenant_id"],),
        )

    origin = {"Origin": "https://shop.example.com"}
    before = client.post(
        "/api/v1/widget/session", json={"bot_public_id": public_id}, headers=origin
    )
    assert before.status_code == 200, before.text

    removed = owner["client"].delete(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}"
    )
    assert removed.status_code == 204

    after = client.post(
        "/api/v1/widget/session", json={"bot_public_id": public_id}, headers=origin
    )
    assert after.status_code == 403
    assert after.json()["detail"]["error"]["code"] == "no_allowed_domains"


def test_a_legacy_row_the_module_refuses_to_parse_does_not_break_the_listing(
    owner, migrated_db: str
) -> None:
    """The dev seed inserts `http://127.0.0.1:8099` directly into `bot_domains`.

    `record_name` refuses an IP, and the listing must survive it: one unparseable row
    must not hide every other domain on the bot behind a 500.
    """
    _add(owner, "shop.example.com")
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)"
            " VALUES (%s, %s, %s, true)",
            (owner["tenant_id"], owner["bot_id"], "http://127.0.0.1:8099"),
        )

    rows = _listed(owner)
    assert [r["domain"] for r in rows] == ["http://127.0.0.1:8099", "shop.example.com"]
    assert rows[0]["record_name"] == ""
    assert rows[1]["record_name"] == "_eva-verify.shop.example.com"


def test_a_second_bot_in_the_same_tenant_cannot_see_the_first_bots_domains(
    owner, migrated_db: str
) -> None:
    """RLS scopes by *tenant*, so it cannot catch a missing `bot_id` predicate.

    The MVP creates one bot per tenant, which makes a cross-bot leak invisible to every
    other test here — the query would still be correct for the only bot that exists. That
    is the shape §16.1 warns about (a widget must not serve a bot's domain through a
    sibling bot's token), so the second bot is seeded explicitly and the rule is violated
    on purpose.
    """
    _add(owner, "shop.example.com")
    sibling = uuid.uuid4()
    with psycopg.connect(_admin(migrated_db), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO bots (id, tenant_id, public_id, name) VALUES (%s, %s, %s, %s)",
            (sibling, owner["tenant_id"], f"bot-{sibling.hex[:12]}", "Sibling"),
        )

    assert owner["client"].get(f"/api/v1/bots/{sibling}/domains").json() == [], (
        "a sibling bot must not inherit another bot's allowed domains"
    )
    assert (
        owner["client"].post(
            f"/api/v1/bots/{sibling}/domains/{_listed(owner)[0]['id']}/verify"
        ).status_code
        == 404
    )
    assert (
        owner["client"].delete(
            f"/api/v1/bots/{sibling}/domains/{_listed(owner)[0]['id']}"
        ).status_code
        == 404
    )


def test_a_foreign_tenant_cannot_see_or_touch_domains(
    owner, migrated_db: str, client: TestClient
) -> None:
    """404, not 403: whether another tenant's bot exists is not this endpoint's answer."""
    _add(owner, "shop.example.com")
    domain_id = _listed(owner)[0]["id"]

    other = _register(client)
    other_token = _login(client, other["email"])
    _as(client, other_token)

    assert client.get(f"/api/v1/bots/{owner['bot_id']}/domains").status_code == 404
    assert (
        client.post(
            f"/api/v1/bots/{owner['bot_id']}/domains", json={"domain": "evil.example.com"}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/v1/bots/{owner['bot_id']}/domains/{domain_id}/verify").status_code
        == 404
    )
    assert (
        client.delete(f"/api/v1/bots/{owner['bot_id']}/domains/{domain_id}").status_code == 404
    )

    with psycopg.connect(_admin(migrated_db)) as conn:
        held = conn.execute(
            "SELECT domain FROM bot_domains WHERE tenant_id = %s", (owner["tenant_id"],)
        ).fetchall()
    assert held == [("shop.example.com",)], "the foreign caller changed nothing"
    _purge_tenant(migrated_db, other["tenant_id"])


def test_domains_require_a_session(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get(f"/api/v1/bots/{uuid.uuid4()}/domains")
    assert response.status_code == 401


def test_a_domain_for_an_unknown_bot_is_404(owner) -> None:
    response = owner["client"].post(
        f"/api/v1/bots/{uuid.uuid4()}/domains", json={"domain": "shop.example.com"}
    )
    assert response.status_code == 404


def test_verify_for_an_unknown_domain_is_404(owner) -> None:
    _add(owner)
    response = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{uuid.uuid4()}/verify"
    )
    assert response.status_code == 404


# --- audit ---------------------------------------------------------------


def test_every_change_is_audited_and_no_token_reaches_the_log(
    owner, migrated_db: str, answering
) -> None:
    """The token is public by design, but `audit_logs` is append-only and never purged,
    so a value written there can never be withdrawn. Domain and reason only."""
    added = _add(owner).json()
    token = added["record_value"].split("=", 1)[1]

    answering({}, mode="empty")
    owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    owner["client"].delete(f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}")

    with psycopg.connect(_admin(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT action, detail FROM audit_logs WHERE tenant_id = %s ORDER BY created_at",
            (owner["tenant_id"],),
        ).fetchall()

    actions = [row[0] for row in rows]
    assert "domain.added" in actions
    assert "domain.verify_failed" in actions
    assert "domain.removed" in actions
    assert token not in json.dumps([str(row[1]) for row in rows])


def test_a_successful_verification_is_audited_as_such(owner, migrated_db: str, answering) -> None:
    added = _add(owner).json()
    token = added["record_value"].split("=", 1)[1]
    answering({"_eva-verify.shop.example.com": [f"eva-verify={token}"]})
    response = owner["client"].post(
        f"/api/v1/bots/{owner['bot_id']}/domains/{_listed(owner)[0]['id']}/verify"
    )
    assert response.status_code == 200 and response.json()["verified"] is True

    with psycopg.connect(_admin(migrated_db)) as conn:
        actions = conn.execute(
            "SELECT action FROM audit_logs WHERE tenant_id = %s", (owner["tenant_id"],)
        ).fetchall()
    assert ("domain.verified",) in actions
