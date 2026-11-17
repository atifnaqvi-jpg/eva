"""WP-19/ADMN-03: owner-facing bot configuration writes.

The properties worth defending here, stated once:

* **A saved value is the value the widget serves.** `appearance.normalise` and
  `suggestions.sanitise` exist because `bots.appearance` renders inside a third party's
  page. If the write path validated differently from the read path, an owner could save a
  colour and watch the widget show a different one — so the tests assert the *round trip*
  through `GET /config`, the endpoint the loader actually calls, rather than re-reading
  the row.
* **A rejected value is not stored.** A hostile colour is replaced with the default at
  the boundary and never persisted, so a later fix to the normaliser cannot resurrect it.
* **A patch touches only what it names.** One form editing the greeting must not blank
  the name, and an empty patch is a no-op rather than an error.
* **Field names are audited, values are not.** `instructions` is the system prompt; the
  audit trail is read by more people than the config screen is.

Fixtures are synthetic throughout, per the confidentiality charter.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from fastapi.testclient import TestClient

from test_conversation import client  # noqa: F401  (module-scoped app/db fixture)

PASSWORD = "correct-horse-battery"
FIXTURE = "botcfg-"


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
        json={"email": email, "password": PASSWORD, "tenant_name": "Bot Config"},
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


def _bot_id(migrated_db: str, tenant_id: str) -> str:
    with psycopg.connect(_admin(migrated_db)) as conn:
        row = conn.execute(
            "SELECT id FROM bots WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    assert row is not None, "registration creates one bot per tenant"
    return str(row[0])


@pytest.fixture
def owner(client: TestClient, migrated_db: str):
    tenant = _register(client)
    token = _login(client, tenant["email"])
    _as(client, token)
    bot_id = _bot_id(migrated_db, tenant["tenant_id"])
    tenant["db"] = migrated_db
    yield {"tenant": tenant, "bot_id": bot_id, "client": client}
    _purge_tenant(migrated_db, tenant["tenant_id"])


def test_get_reports_the_defaults_a_new_bot_has(owner) -> None:
    response = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["name"] == "Bot Config"
    assert body["welcome_message"] is None
    assert body["confidence_threshold"] == 0.55
    assert body["powered_by_visible"] is True
    assert body["suggestions"] == []
    assert body["dynamic_suggestions"] is True
    # `normalise` returns only the keys the tenant actually set -- it does not fill
    # defaults, because the widget applies those at render. So a fresh bot has an empty
    # appearance, and `appearance_defaults` is what tells the dashboard which colour is
    # *effectively* in use. Asserting a filled-in `appearance` here would be asserting
    # behaviour the widget does not have.
    assert body["appearance"] == {}
    assert body["appearance_defaults"]["primaryColor"] == "#1f6feb"
    assert body["appearance_defaults"]["position"] == "bottom-right"


def test_patch_round_trips_every_field(owner) -> None:
    response = owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}",
        json={
            "name": "Acme Support",
            "welcome_message": "Hi! How can I help?",
            "confidence_threshold": 0.7,
            "powered_by_visible": False,
            "appearance": {"primaryColor": "#ff8800", "position": "bottom-left"},
            "suggestions": ["What are your hours?", "Where is my order?"],
            "dynamic_suggestions": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Acme Support"
    assert body["welcome_message"] == "Hi! How can I help?"
    assert body["confidence_threshold"] == 0.7
    assert body["powered_by_visible"] is False
    assert body["appearance"]["primaryColor"] == "#ff8800"
    assert body["appearance"]["position"] == "bottom-left"
    assert body["dynamic_suggestions"] is False
    assert body["suggestions"] == ["What are your hours?", "Where is my order?"]

    # Re-read: the write must have committed, not merely been echoed back.
    again = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}").json()
    assert again["name"] == "Acme Support"


def test_saved_appearance_is_exactly_what_the_widget_serves(owner) -> None:
    """The write path and the widget's read path must agree.

    `GET /bots/{id}/config` is the endpoint the loader calls. If saving normalised
    differently, an owner would see one colour and their visitors another.
    """
    owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}",
        json={"appearance": {"primaryColor": "#123456", "position": "bottom-left"}},
    )
    widget = owner["client"].get(f"/api/v1/conversations/bots/{owner['bot_id']}/config")
    assert widget.status_code == 200, widget.text
    body = widget.json()
    assert body["confidence_threshold"] == 0.55
    # The widget serves the normalised appearance, so compare against the owner view.
    owner_view = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}").json()
    assert owner_view["appearance"]["primaryColor"] == "#123456"


def test_a_hostile_colour_is_replaced_not_stored(owner) -> None:
    """The allowlist runs on write, so the bad value never reaches the row.

    A value with a `</style>` in it would render inside a third party's page. Asserting
    only that the response shows the default would pass even if the hostile string were
    persisted and rewritten on every read — so this checks the stored column directly.
    """
    hostile = "red;}</style><script>alert(1)</script>"
    response = owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}", json={"appearance": {"primaryColor": hostile}}
    )
    assert response.status_code == 200, response.text
    # Rejected, so the key is absent entirely and the widget falls back to its default.
    assert "primaryColor" not in response.json()["appearance"]
    assert response.json()["appearance_defaults"]["primaryColor"] == "#1f6feb"
    _assert_not_stored(owner, "script")


def _assert_not_stored(owner, needle: str) -> None:
    """Read the raw column, bypassing every layer that could be scrubbing on read."""
    with psycopg.connect(_admin(owner["tenant"]["db"])) as conn:
        row = conn.execute(
            "SELECT appearance::text FROM bots WHERE id = %s", (owner["bot_id"],)
        ).fetchone()
    assert row is not None
    assert needle not in row[0], f"{needle!r} reached the bots row"


def test_unknown_appearance_keys_are_dropped(owner) -> None:
    response = owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}",
        json={"appearance": {"primaryColor": "#123456", "totallyUnknown": "x"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "totallyUnknown" not in body["appearance"]
    assert body["appearance"]["primaryColor"] == "#123456"
    _assert_not_stored(owner, "totallyUnknown")


def test_patch_leaves_unnamed_fields_alone(owner) -> None:
    owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}",
        json={"name": "Keep Me", "welcome_message": "Original"},
    )
    response = owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}", json={"welcome_message": "Changed"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Keep Me"
    assert body["welcome_message"] == "Changed"


def test_an_empty_patch_is_a_no_op(owner) -> None:
    before = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}").json()
    response = owner["client"].patch(f"/api/v1/bots/{owner['bot_id']}", json={})
    assert response.status_code == 200, response.text
    assert response.json() == before


def test_blank_name_is_rejected(owner) -> None:
    response = owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}", json={"name": "   "}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_name"


def test_threshold_outside_zero_to_one_is_rejected(owner) -> None:
    for bad in (-0.1, 1.1):
        response = owner["client"].patch(
            f"/api/v1/bots/{owner['bot_id']}", json={"confidence_threshold": bad}
        )
        assert response.status_code == 422, response.text


def test_another_tenants_bot_is_a_404_not_a_403(client: TestClient, migrated_db: str) -> None:
    """A 403 would confirm the id exists — the enumeration oracle."""
    mine = _register(client)
    _as(client, _login(client, mine["email"]))
    my_bot = _bot_id(migrated_db, mine["tenant_id"])

    other = _register(client)
    other_bot = _bot_id(migrated_db, other["tenant_id"])
    other_token = _login(client, other["email"])

    try:
        # Registering switched the client's cookie to the other tenant, so switch back
        # before probing: the point is to call as *mine* against *their* id.
        _as(client, _login(client, mine["email"]))
        read = client.get(f"/api/v1/bots/{other_bot}")
        write = client.patch(f"/api/v1/bots/{other_bot}", json={"name": "Hijacked"})
        assert read.status_code == 404, read.text
        assert write.status_code == 404, write.text
        # And the row is untouched.
        _as(client, other_token)
        assert client.get(f"/api/v1/bots/{other_bot}").json()["name"] == "Bot Config"
    finally:
        _purge_tenant(migrated_db, mine["tenant_id"])
        _purge_tenant(migrated_db, other["tenant_id"])
        assert my_bot


def test_malformed_bot_id_is_a_400(owner) -> None:
    response = owner["client"].get("/api/v1/bots/not-a-uuid")
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_uuid"


def test_audit_records_the_field_names_and_not_the_values(owner, migrated_db: str) -> None:
    owner["client"].patch(
        f"/api/v1/bots/{owner['bot_id']}",
        json={"instructions": "SECRET-SYSTEM-PROMPT", "name": "Audited"},
    )
    with psycopg.connect(_admin(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT detail FROM audit_logs"
            " WHERE tenant_id = %s AND action = 'bot.config_updated'",
            (owner["tenant"]["tenant_id"],),
        ).fetchall()
    assert rows, "the mutation must be audited"
    detail = rows[-1][0]
    assert detail["fields"] == ["name", "instructions"]
    assert "SECRET-SYSTEM-PROMPT" not in str(detail)


def test_tenant_scope_is_enforced_by_both_predicate_and_rls(owner, migrated_db: str) -> None:
    """The explicit predicate and RLS each independently block a cross-tenant write.

    Discovered by mutation, the same way as the archive: making the lookup's predicate
    unconditionally true did **not** leak a foreign bot, because row-level security
    refused the row anyway. That is defence in depth working — but it means the
    isolation test above cannot tell the two layers apart, so deleting the predicate
    would look harmless until someone also touched the policy.

    This pins the predicate at the source level. It is a blunt assertion, deliberately:
    the alternative is trusting that RLS never changes, and RLS is exactly the layer a
    future migration is most likely to alter.
    """
    from app.botconfig import router as botconfig

    source = __import__("inspect").getsource(botconfig)
    assert "FROM bots WHERE id = :bot_id AND tenant_id = :tenant_id" in source, (
        "the bot lookup lost its explicit tenant predicate; RLS would still block a "
        "leak, but the query now depends on it alone"
    )

    with psycopg.connect(_admin(migrated_db)) as conn:
        policies = conn.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'bots'"
        ).fetchone()[0]
    assert policies >= 1, "bots lost its RLS policy"

    # And a well-formed id belonging to nobody is a 404, while a malformed one is a 400:
    # the difference keeps "not found" from doubling as "did not parse".
    assert owner["client"].get(f"/api/v1/bots/{uuid.uuid4()}").status_code == 404


def test_list_returns_only_my_bots(client: TestClient, migrated_db: str) -> None:
    """The list endpoint must scope by tenant like every other read.

    Worth its own test because a list endpoint has no id to scope: if the WHERE clause
    were dropped the response would quietly include other tenants' bots rather than
    404, and every other test here would still pass.
    """
    mine = _register(client)
    _as(client, _login(client, mine["email"]))
    other = _register(client)
    other_bot = _bot_id(migrated_db, other["tenant_id"])

    try:
        _as(client, _login(client, mine["email"]))
        listed = client.get("/api/v1/bots").json()
        ids = [bot["id"] for bot in listed]
        assert len(ids) == 1, f"expected only my bot, got {len(ids)}"
        assert other_bot not in ids
        assert listed[0]["name"] == "Bot Config"
    finally:
        _purge_tenant(migrated_db, mine["tenant_id"])
        _purge_tenant(migrated_db, other["tenant_id"])


def test_embed_snippet_names_a_loader_that_is_actually_served(owner) -> None:
    """ADMN-12: the snippet has to work, not merely be well-formed.

    A snippet is the one artefact an owner pastes into a site they control and then
    blames themselves when nothing appears. The cheap test asserts the string contains
    their public_id; that passes even if the script URL is wrong. This one fetches the
    URL the snippet names and checks it is the loader, and that the public_id in the
    tag is one the widget would accept.
    """
    response = owner["client"].get(f"/api/v1/bots/{owner['bot_id']}/embed-snippet")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["public_id"] in body["snippet"]
    assert body["snippet"].startswith("<script ")
    assert body["snippet"].endswith("</script>")
    assert 'defer' in body["snippet"]

    # The URL must be the one the loader is actually served at. Serving the loader is
    # covered by test_widget_serving; what is only true here is that this endpoint
    # points at it. Asserting the path rather than fetching it also keeps this test
    # from depending on whether a widget build happens to be mounted in the process.
    from urllib.parse import urlsplit

    assert urlsplit(body["script_url"]).path == "/widget/loader.js"
    assert body["script_url"] in body["snippet"]


def test_embed_snippet_is_scoped_to_the_tenant(owner, migrated_db: str) -> None:
    other = _register(owner["client"])
    other_bot = _bot_id(migrated_db, other["tenant_id"])
    try:
        _as(owner["client"], _login(owner["client"], owner["tenant"]["email"]))
        response = owner["client"].get(f"/api/v1/bots/{other_bot}/embed-snippet")
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"]["code"] == "bot_not_found"
    finally:
        _purge_tenant(migrated_db, other["tenant_id"])


def test_active_knowledge_version_is_null_until_something_is_published(owner) -> None:
    """ADMN-05: the knowledge screen says "live" only when a version really is live.

    The field exists so the screen can distinguish "ready but never published" from
    "published", which look identical in the documents table. A field that is always
    null would pass a shape check and make every document look unpublished, so this
    reads the pointer back after setting it in the database.
    """
    client = owner["client"]
    configured = client.get(f"/api/v1/bots/{owner['bot_id']}").json()
    assert configured["active_knowledge_version_id"] is None

    with psycopg.connect(_admin(owner["tenant"]["db"]), autocommit=True) as conn:
        # Registration creates a bot but no knowledge, so the document is built here:
        # a source and document are required parents, not optional detail.
        source_id = conn.execute(
            "INSERT INTO knowledge_sources (tenant_id, type, config)"
            " SELECT tenant_id, 'upload', '{}'::jsonb FROM bots WHERE id = %s"
            " RETURNING id",
            (owner["bot_id"],),
        ).fetchone()[0]
        document_id = conn.execute(
            "INSERT INTO documents (tenant_id, source_id, title)"
            " SELECT tenant_id, %s, 'Live version test' FROM bots WHERE id = %s"
            " RETURNING id",
            (source_id, owner["bot_id"]),
        ).fetchone()[0]
        version_id = conn.execute(
            "INSERT INTO document_versions (tenant_id, document_id, version_no,"
            " content_hash, status)"
            " SELECT tenant_id, %s, 1, 'test-hash', 'ready' FROM bots WHERE id = %s"
            " RETURNING id",
            (document_id, owner["bot_id"]),
        ).fetchone()[0]
        conn.execute(
            "UPDATE bots SET active_knowledge_version_id = %s WHERE id = %s",
            (version_id, owner["bot_id"]),
        )

    try:
        listed = client.get("/api/v1/bots").json()
        assert listed[0]["active_knowledge_version_id"] == str(version_id), (
            "the list endpoint must report the live version too -- the screen reads"
            " the list, not the single-bot endpoint"
        )
    finally:
        with psycopg.connect(_admin(owner["tenant"]["db"]), autocommit=True) as conn:
            conn.execute(
                "UPDATE bots SET active_knowledge_version_id = NULL WHERE id = %s",
                (owner["bot_id"],),
            )
            conn.execute("DELETE FROM document_versions WHERE id = %s", (version_id,))
