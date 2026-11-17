"""SEC-09: retention is configurable per tenant, defaults to 90 days, and the purge
deletes what it claims to delete.

The claims worth testing are the ones that fail silently in production:

* the purge runs as the **RLS-bound app role**, so a naive cross-tenant
  ``DELETE`` matches zero rows without raising. A test that only checked "the job
  returned 0" would pass against a purge that never deleted anything, so these
  assert on the rows themselves.
* the default is what protects tenants who never open settings, so an absent or
  malformed blob must resolve to 90 rather than to "keep forever".
* an ``active`` conversation is still happening, however old, and must survive.
* deleting a conversation must take its messages with it (the cascade), or the
  purge would leave the transcript it claimed to remove.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn
from app.retention import policy
from worker.purge_job import purge_expired


def _app_url(db: str) -> str:
    return URL.create(
        "postgresql+psycopg", username=APP_USER, password=APP_PASSWORD,
        host=PG_HOST, port=PG_PORT, database=db,
    ).render_as_string(hide_password=False)


@pytest.fixture
def app_sessions(migrated_db: str, monkeypatch):
    """Point the job's session factory at the test DB, keeping the real factory.

    Keeping ``make_session_factory`` is the point: it installs the ``after_begin`` hook
    that sets the tenant GUC, so these tests exercise the same RLS behaviour production
    does rather than a session that bypasses it.
    """
    import app.db.session as db_session

    engine = create_engine(_app_url(migrated_db))
    monkeypatch.setattr(db_session, "SessionLocal", db_session.make_session_factory(engine))

# --- the policy ---------------------------------------------------------------


def test_default_retention_is_ninety_days() -> None:
    """A tenant with no settings gets the number we promised their visitors."""
    assert policy.settings({})["conversation_days"] == 90
    assert policy.settings(None)["conversation_days"] == 90
    assert policy.settings("not a dict")["conversation_days"] == 90


def test_a_stored_value_is_honoured() -> None:
    assert policy.settings({"retention": {"conversation_days": 30}})["conversation_days"] == 30


@pytest.mark.parametrize("bad", [0, -5, 5000, "30", 3.5, True, None])
def test_an_unusable_stored_value_falls_back_to_the_default(bad) -> None:
    """The blob is operator-editable JSON, so reading must never raise or mean something
    unintended — a hand-edited 0 would otherwise look like "delete everything now"."""
    assert policy.settings({"retention": {"conversation_days": bad}})["conversation_days"] == 90


@pytest.mark.parametrize("bad", [0, -1, 10_000, "30", True])
def test_writing_an_invalid_window_is_refused(bad) -> None:
    """The reader is forgiving, so the writer must not be: saving a value the reader
    would silently replace would leave the screen showing a different number."""
    with pytest.raises(ValueError):
        policy.validate({"conversation_days": bad})


def test_writing_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown retention setting"):
        policy.validate({"conversation_days": 90, "forever": True})


# --- the purge ----------------------------------------------------------------


def _seed(conn, tenant_id, *, status, age_days, messages=1) -> uuid.UUID:
    conv = uuid.uuid4()
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    conn.execute(
        "INSERT INTO bots (tenant_id, public_id, name) VALUES (%s, %s, %s)"
        " ON CONFLICT DO NOTHING",
        (tenant_id, f"bot-{tenant_id}", f"Bot {tenant_id}"),
    )
    bot_id = conn.execute(
        "SELECT id FROM bots WHERE tenant_id = %s LIMIT 1", (tenant_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO conversations (id, tenant_id, bot_id, visitor_session_id, status,"
        " started_at, last_activity_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (conv, tenant_id, bot_id, f"visit-{conv}", status, when, when),
    )
    for i in range(messages):
        conn.execute(
            "INSERT INTO messages (tenant_id, conversation_id, role, content)"
            " VALUES (%s, %s, %s, %s)",
            (tenant_id, conv, "visitor", f"message {i}"),
        )
    conn.commit()
    return conv


def _exists(conn, conv_id) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM conversations WHERE id = %s", (conv_id,)).fetchone()
    )


@pytest.mark.usefixtures("migrated_db")
def test_the_purge_deletes_only_what_is_past_its_tenant_window(migrated_db: str, app_sessions) -> None:
    """Two tenants, two windows, one purge.

    This is the test that would catch a purge running outside RLS: it would delete
    neither row and still return success.
    """
    short, long_ = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (short, "short-retention"))
        conn.execute(
            "INSERT INTO tenants (id, name, settings) VALUES (%s, %s, %s::jsonb)",
            (long_, "long-retention", '{"retention": {"conversation_days": 365}}'),
        )
        conn.execute(
            "UPDATE tenants SET settings = %s::jsonb WHERE id = %s",
            ('{"retention": {"conversation_days": 7}}', str(short)),
        )
        stale_short = _seed(conn, short, status="closed", age_days=10)
        fresh_short = _seed(conn, short, status="closed", age_days=2)
        stale_long = _seed(conn, long_, status="closed", age_days=10)

        report = asyncio.run(purge_expired({}))

        assert not _exists(conn, stale_short), "10 days old with a 7 day window must go"
        assert _exists(conn, fresh_short), "2 days old is inside the window"
        assert _exists(conn, stale_long), "the other tenant's 365 day window protects it"
        assert report["per_tenant"][str(short)] == 1
        assert report["per_tenant"][str(long_)] == 0

        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", ([short, long_],))


@pytest.mark.usefixtures("migrated_db")
def test_an_active_conversation_is_never_purged(migrated_db: str, app_sessions) -> None:
    """Somebody may still be in it; the clock only decides when a *finished* one goes."""
    tenant = uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, "busy-tenant"))
        conn.execute(
            "UPDATE tenants SET settings = %s::jsonb WHERE id = %s",
            ('{"retention": {"conversation_days": 1}}', str(tenant)),
        )
        old_active = _seed(conn, tenant, status="active", age_days=400)

        asyncio.run(purge_expired({}))

        assert _exists(conn, old_active), "an active conversation outlives its window"
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))


@pytest.mark.usefixtures("migrated_db")
def test_purging_a_conversation_takes_its_messages(migrated_db: str, app_sessions) -> None:
    """Otherwise the purge removes the row and leaves the transcript behind."""
    tenant = uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, "cascade-tenant"))
        conn.execute(
            "UPDATE tenants SET settings = %s::jsonb WHERE id = %s",
            ('{"retention": {"conversation_days": 1}}', str(tenant)),
        )
        conv = _seed(conn, tenant, status="closed", age_days=30, messages=3)

        before = conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = %s", (conv,)
        ).fetchone()[0]
        assert before == 3

        asyncio.run(purge_expired({}))

        after = conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = %s", (conv,)
        ).fetchone()[0]
        assert after == 0, "messages must go with the conversation"
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
