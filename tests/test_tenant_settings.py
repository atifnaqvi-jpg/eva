"""ADMN-10: profile, password change and the handover settings the screen owns.

Two of these are security surfaces rather than forms, so they get tests that would fail if
the check were merely present:

* A password change re-proves the current password and revokes *other* sessions. Without
  the second half, an owner who changes their password because they think a colleague has
  their old one is left with that colleague still logged in.
* The handover email is validated through the same normaliser the handover path reads
  with. A screen that accepts `not-an-email` and stores it is worse than no screen: the
  owner believes escalations are being emailed.
"""

import os

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg

from conftest import _admin_dsn
from test_conversation import client, tenant  # noqa: F401

PASSWORD = "correct-horse-battery"


def test_profile_names_the_tenant_it_belongs_to(client, tenant) -> None:
    """The screen's heading combines two tables; a 404 here would blank the page."""
    response = client.get("/api/v1/settings/profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == tenant["email"]
    assert body["tenant_name"] == "C"
    assert body["tenant_id"] == tenant["tenant_id"]


def test_changing_a_password_requires_the_current_one(client, tenant) -> None:
    """A live session is not proof of identity for an irreversible change."""
    response = client.post(
        "/api/v1/settings/password",
        json={"current_password": "not-my-password", "new_password": "a-much-longer-pass"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_credentials"

    # And the old password still works, i.e. the rejected request changed nothing.
    client.cookies.clear()
    again = client.post(
        "/api/v1/auth/login", json={"email": tenant["email"], "password": PASSWORD}
    )
    assert again.status_code == 200, again.text


def test_a_short_new_password_is_rejected_by_the_same_rule_as_registration(
    client, tenant
) -> None:
    response = client.post(
        "/api/v1/settings/password",
        json={"current_password": PASSWORD, "new_password": "short"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"]["code"] == "weak_password"


def test_changing_a_password_revokes_the_other_sessions(client, tenant, migrated_db) -> None:
    """The point of a password change is that the other person is logged out.

    The caller's own session must survive, or the screen logs the owner out of the page
    they just used to change it.
    """
    # A second session for the same user, as if from another browser.
    other = client.post(
        "/api/v1/auth/login", json={"email": tenant["email"], "password": PASSWORD}
    )
    other_token = next(
        header.split(";")[0].split("=", 1)[1]
        for header in other.headers.get_list("set-cookie")
        if header.startswith("eva_session=")
    )

    response = client.post(
        "/api/v1/settings/password",
        json={"current_password": PASSWORD, "new_password": "a-much-longer-pass"},
    )
    assert response.status_code == 204, response.text

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        live = conn.execute(
            "SELECT count(*) FROM sessions WHERE user_id = %s AND revoked_at IS NULL",
            (tenant["user_id"],),
        ).fetchone()[0]
    assert live == 1, f"expected only the caller's session to survive, {live} did"

    # The surviving session is the caller's: the page still works.
    assert client.get("/api/v1/settings/profile").status_code == 200

    # And the revoked one is really rejected, not just marked.
    client.cookies.clear()
    client.cookies.set("eva_session", other_token)
    assert client.get("/api/v1/settings/profile").status_code == 401


def test_handover_email_is_stored_where_the_handover_path_reads_it(
    client, tenant, migrated_db
) -> None:
    """Writing settings and reading them back through the *reader* is the only proof.

    A write to the wrong JSONB path would pass a round-trip test against the same endpoint
    while escalations still went nowhere.
    """
    response = client.patch(
        "/api/v1/settings/tenant",
        json={"handover": {"email": "support@example.test", "keywords": ["Human"]}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["handover"]["email"] == "support@example.test"
    # Keywords are normalised to lowercase by the reader; the screen is shown that value.
    assert response.json()["handover"]["keywords"] == ["human"]

    from app.handover import policy

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        settings = conn.execute(
            "SELECT settings FROM tenants WHERE id = %s", (tenant["tenant_id"],)
        ).fetchone()[0]
    assert policy.settings(settings)["email"] == "support@example.test"


def test_an_unusable_handover_email_is_refused_rather_than_dropped(client, tenant) -> None:
    """The reader drops a value it cannot use; accepting it would silence escalations."""
    response = client.patch(
        "/api/v1/settings/tenant", json={"handover": {"email": "not-an-email"}}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_email"


def test_a_settings_patch_leaves_other_keys_alone(client, tenant, migrated_db) -> None:
    """The blob is shared with retrieval config; a merge that replaced it would be silent."""
    with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
        conn.execute(
            "UPDATE tenants SET settings = %s::jsonb WHERE id = %s",
            ('{"retrieval": {"rerank": false}}', tenant["tenant_id"]),
        )

    response = client.patch(
        "/api/v1/settings/tenant", json={"handover": {"email": "ops@example.test"}}
    )
    assert response.status_code == 200, response.text

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        settings = conn.execute(
            "SELECT settings FROM tenants WHERE id = %s", (tenant["tenant_id"],)
        ).fetchone()[0]
    assert settings["retrieval"] == {"rerank": False}, "unrelated key was clobbered"
    assert settings["handover"]["email"] == "ops@example.test"


# --- retention (SEC-09) -------------------------------------------------------
#
# The window shown on the settings screen and the window the purge enforces have to be
# the same number, and both go through ``app.retention.policy``. Asserting the round trip
# alone would not prove that: a write to the wrong JSONB path would satisfy it while the
# purge kept applying the default, so these check where the value actually landed.


def test_retention_defaults_to_ninety_days_for_a_new_tenant(client, tenant) -> None:
    """The default is what protects a tenant who never opens this screen."""
    response = client.get("/api/v1/settings/tenant")
    assert response.status_code == 200, response.text
    assert response.json()["retention"]["conversation_days"] == 90


def test_a_retention_window_is_stored_where_the_purge_reads_it(
    client, tenant, migrated_db
) -> None:
    response = client.patch("/api/v1/settings/tenant", json={"retention": {"conversation_days": 30}})
    assert response.status_code == 200, response.text
    assert response.json()["retention"]["conversation_days"] == 30

    from app.retention import policy

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        settings = conn.execute(
            "SELECT settings FROM tenants WHERE id = %s", (tenant["tenant_id"],)
        ).fetchone()[0]
    assert policy.settings(settings)["conversation_days"] == 30


def test_a_window_shorter_than_a_day_is_refused(client, tenant) -> None:
    """Zero would mean "delete immediately", which is not a retention choice: it would
    empty a conversation someone is still having."""
    response = client.patch("/api/v1/settings/tenant", json={"retention": {"conversation_days": 0}})
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "invalid_retention"


def test_too_long_a_window_is_refused_rather_than_silently_clamped(client, tenant) -> None:
    response = client.patch(
        "/api/v1/settings/tenant", json={"retention": {"conversation_days": 9999}}
    )
    assert response.status_code == 400, response.text


def test_a_retention_change_leaves_the_handover_block_alone(client, tenant, migrated_db) -> None:
    client.patch("/api/v1/settings/tenant", json={"handover": {"email": "ops@example.test"}})
    response = client.patch(
        "/api/v1/settings/tenant", json={"retention": {"conversation_days": 14}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["handover"]["email"] == "ops@example.test"
    # Handover and retention share one blob, so a merge that replaced it would silently
    # disable escalations.
    assert response.json()["retention"]["conversation_days"] == 14
