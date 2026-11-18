"""OPS-04: the five §22 alerts, at HALT-09's defaults, to the operator's mailbox.

HALT-09 (PLAN §8.25 RULINGS) is the shape of this file:

* **Reading B** — the two signals whose events were never recorded were fixed first
  (OPS-12's ``provider_outcomes`` and real token ledger, OPS-09's backup producer). So the
  provider and budget tests seed those tables and read them back, rather than asserting a
  branch that cannot be reached.
* **Defaults, not policy** — the thresholds are asserted as values an operator changes in
  ``.env`` and a tenant changes in ``tenants.settings -> alerts``. A test that hard-coded
  0.05 everywhere would make the configurability untested.
* **``ALERT_EMAIL`` is the operator's address, never a tenant's** — and its absence is
  reported as ``logged``, because an alert channel that is silently unset is
  indistinguishable from a quiet system.

Three classes of test here exist because the failure they catch is silent:

* **Transitions, not states.** A poller that re-sent an active warning every minute would
  make the channel unreadable; one that never sent could not be told apart from a healthy
  system. Both directions are asserted.
* **The queue must not read "cannot see" as "empty".**
* **No content.** Alert text leaves the box by email, so a seeded visitor question must be
  absent from every subject and body — OPS-01's and OPS-02's rule, in a third place.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn

ROOT = Path(__file__).resolve().parent.parent
ALERTS_SRC = ROOT / "backend" / "api" / "app" / "observability" / "alerts.py"
RUNBOOK = ROOT / "docs" / "ops" / "RUNBOOK.md"
ENV_EXAMPLE = ROOT / ".env.example"

CANARY = "CANARY-should-never-leave-the-box"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _app_url(db: str) -> str:
    return URL.create(
        "postgresql+psycopg", username=APP_USER, password=APP_PASSWORD,
        host=PG_HOST, port=PG_PORT, database=db,
    ).render_as_string(hide_password=False)


@pytest.fixture
def alerts_env(migrated_db: str, monkeypatch):
    """The real session factory pointed at the test database.

    ``make_session_factory`` is kept on purpose: it installs the ``after_begin`` hook that
    sets the tenant GUC, so these tests exercise the same RLS behaviour the worker does.
    """
    import app.db.session as db_session
    from app.observability import alerts

    engine = create_engine(_app_url(migrated_db))
    monkeypatch.setattr(db_session, "SessionLocal", db_session.make_session_factory(engine))
    monkeypatch.setattr(alerts, "_STATE", {})
    monkeypatch.setattr(alerts, "_QUEUE_SINCE", {})
    return alerts


def _tenant(conn, settings: str | None = None) -> str:
    tenant = uuid.uuid4()
    if settings is None:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (tenant, "t"))
    else:
        conn.execute(
            "INSERT INTO tenants (id, name, settings) VALUES (%s, %s, %s::jsonb)",
            (tenant, "t", settings),
        )
    conn.execute(
        "INSERT INTO bots (tenant_id, public_id, name) VALUES (%s, %s, %s)",
        (tenant, f"bot-{tenant}", "Bot"),
    )
    return str(tenant)


def _conversation(conn, tenant: str) -> str:
    bot = conn.execute(
        "SELECT id FROM bots WHERE tenant_id = %s LIMIT 1", (tenant,)
    ).fetchone()[0]
    conv = uuid.uuid4()
    conn.execute(
        "INSERT INTO conversations (id, tenant_id, bot_id, visitor_session_id, status)"
        " VALUES (%s, %s, %s, %s, 'active')",
        (conv, tenant, bot, f"visit-{conv}"),
    )
    conn.execute(
        "INSERT INTO messages (tenant_id, conversation_id, role, content)"
        " VALUES (%s, %s, 'visitor', %s)",
        (tenant, conv, CANARY),
    )
    return str(conv)


def _outcomes(conn, tenant: str, conv: str, *, provider: str, ok: int, failed: int) -> None:
    for outcome, count in (("success", ok), ("failure", failed)):
        for _ in range(count):
            conn.execute(
                "INSERT INTO provider_outcomes"
                " (tenant_id, conversation_id, provider, model, outcome, error_class)"
                " VALUES (%s, %s, %s, 'm', %s, %s)",
                (tenant, conv, provider, outcome,
                 None if outcome == "success" else "TimeoutError"),
            )


def _usage(conn, tenant: str, conv: str, units: int) -> None:
    conn.execute(
        "INSERT INTO usage_records (tenant_id, conversation_id, kind, provider, model,"
        " units) VALUES (%s, %s, 'chat', 'ollama_cloud', 'm', %s)",
        (tenant, conv, units),
    )


class FakeRedis:
    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.calls: list[str] = []

    def zcard(self, key: str) -> int:
        self.calls.append(key)
        return self.depth


class RecordingChannel:
    """A stand-in for ``EmailChannel`` that records what it was handed."""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list = []
        self.ok = ok

    def send(self, notification):
        from app.handover.channels import Delivery

        self.sent.append(notification)
        return Delivery(ok=self.ok, detail="recorded" if self.ok else "smtp refused")


def _usage_reading(total: int, used: int):
    return collections.namedtuple("usage", "total used free")(total, used, total - used)


# --- thresholds ---------------------------------------------------------------


def test_defaults_are_halt_09s_industry_values() -> None:
    """The numbers the ruling fixed, and nowhere else — an assertion on policy, once."""
    from app.observability import alerts

    t = alerts.thresholds()
    assert (t.provider_error_warn, t.provider_error_critical) == (0.05, 0.20)
    assert (t.budget_warn, t.budget_critical) == (0.90, 1.00)
    assert (t.disk_warn, t.disk_critical) == (0.85, 0.95)
    assert (t.queue_warn, t.queue_critical) == (100, 500)
    assert t.queue_sustain_seconds == 300


def test_an_operator_changes_a_default_without_a_code_change(monkeypatch) -> None:
    """HALT-09: "customizable by the user later on". A `.env` edit is the seam."""
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_DISK_WARN", 0.70)
    monkeypatch.setattr(app.config, "ALERT_DISK_CRITICAL", 0.75)
    monkeypatch.setattr(app.config, "ALERT_QUEUE_WARN", 5)
    t = alerts.thresholds()
    assert (t.disk_warn, t.disk_critical, t.queue_warn) == (0.70, 0.75, 5)


def test_a_tenant_overrides_only_the_signals_that_are_its_own(monkeypatch) -> None:
    """Disk, queue and backup describe the deployment, not one customer."""
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_DISK_WARN", 0.60)
    settings = {
        "alerts": {
            "provider_error_rate": {"warn": 0.02, "critical": 0.03, "min_attempts": 3},
            "budget": {"warn": 0.50, "critical": 0.60},
            "disk": {"warn": 0.99},
        }
    }
    t = alerts.thresholds(settings)
    assert (t.provider_error_warn, t.provider_error_critical) == (0.02, 0.03)
    assert t.provider_min_attempts == 3
    assert (t.budget_warn, t.budget_critical) == (0.50, 0.60)
    assert t.disk_warn == 0.60, "a tenant may not move the platform's disk threshold"


@pytest.mark.parametrize(
    "block",
    [
        {"budget": {"warn": 0.95, "critical": 0.5}},          # inverted pair
        {"budget": {"warn": "90%"}},                          # a string is not a ratio
        {"budget": {"warn": True}},                           # bool is an int subclass
        {"budget": {"warn": 0}},                              # zero would never fire
        {"budget": {"warn": 2.0}},                            # above 1.0
        {"provider_error_rate": {"critical": 0.01, "warn": 0.2}},
        "not a dict",
        None,
    ],
)
def test_an_unusable_override_degrades_to_the_operator_defaults(block) -> None:
    """A hand-edited blob must not be able to raise on every sweep, or switch an alert off.

    A merged pair that failed validation would make ``evaluate`` raise for that tenant
    every minute — a way to hide an alert rather than configure one.
    """
    from app.observability import alerts

    t = alerts.thresholds(block)
    base = alerts.thresholds()
    assert (t.budget_warn, t.budget_critical) == (base.budget_warn, base.budget_critical)
    assert (t.provider_error_warn, t.provider_error_critical) == (
        base.provider_error_warn,
        base.provider_error_critical,
    )


def test_an_impossible_threshold_pair_is_refused_at_construction() -> None:
    from app.observability import alerts

    with pytest.raises(ValueError):
        alerts.Thresholds(
            provider_error_warn=0.3, provider_error_critical=0.2,
            provider_min_attempts=10, budget_warn=0.9, budget_critical=1.0,
            disk_warn=0.85, disk_critical=0.95, queue_warn=100, queue_critical=500,
            queue_sustain_seconds=300,
        )


# --- disk ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "used,total,severity",
    [
        (10, 100, "ok"),
        (85, 100, "warn"),
        (94, 100, "warn"),
        (95, 100, "critical"),
        (100, 100, "critical"),
    ],
)
def test_disk_usage_uses_the_ruled_thresholds(monkeypatch, used, total, severity) -> None:
    import shutil

    from app.observability import alerts

    monkeypatch.setattr(shutil, "disk_usage", lambda path: _usage_reading(total, used))
    reading = alerts.disk_reading(thresholds_=alerts.thresholds())
    assert reading.severity == severity
    assert reading.signal == "disk_usage"


def test_disk_usage_reports_the_number_it_judged(monkeypatch) -> None:
    """A severity without a value is an alert nobody can act on."""
    import shutil

    from app.observability import alerts

    monkeypatch.setattr(shutil, "disk_usage", lambda path: _usage_reading(1000, 900))
    reading = alerts.disk_reading(thresholds_=alerts.thresholds())
    assert reading.value == pytest.approx(0.9)
    assert reading.threshold == 0.85
    assert "90.0%" in reading.detail


# --- queue backlog ------------------------------------------------------------


def test_queue_depth_is_a_zset_cardinality_not_llen() -> None:
    """arq pushes with `zadd`, so the queue is a sorted set (measured, §8.33)."""
    from app.observability import alerts, metrics

    assert alerts.QUEUE_KEY == metrics.QUEUE_KEY == "arq:queue"
    client = FakeRedis(3)
    alerts.queue_reading(client, thresholds_=alerts.thresholds(), now=NOW)
    assert client.calls == ["arq:queue"]


def test_a_critical_backlog_alerts_immediately() -> None:
    from app.observability import alerts

    reading = alerts.queue_reading(FakeRedis(501), thresholds_=alerts.thresholds(), now=NOW)
    assert reading.severity == "critical"
    assert reading.value == 501


def test_a_warning_backlog_must_be_sustained() -> None:
    """100 jobs for one tick is a burst; 100 jobs for five minutes is a backlog."""
    from app.observability import alerts

    state: dict[str, datetime] = {}
    client = FakeRedis(150)
    t = alerts.thresholds()

    first = alerts.queue_reading(client, thresholds_=t, now=NOW, state=state)
    assert first.severity == "ok", "inside the sustain window"
    assert "sustain" in first.detail

    later = alerts.queue_reading(
        client, thresholds_=t, now=NOW + timedelta(seconds=301), state=state
    )
    assert later.severity == "warn"

    drained = alerts.queue_reading(FakeRedis(0), thresholds_=t, now=NOW, state=state)
    assert drained.severity == "ok"
    assert "queue_backlog" not in state, "a drained queue restarts the window"


def test_unreachable_redis_is_a_warning_not_a_silence() -> None:
    """"Queue empty" and "cannot see the queue" must not look the same (§8.38)."""
    from app.observability import alerts

    reading = alerts.queue_reading(None, thresholds_=alerts.thresholds(), now=NOW)
    assert reading.severity == "warn"
    assert "not reachable" in reading.detail or "unknown" in reading.detail


def test_a_redis_error_during_the_read_is_a_warning_too() -> None:
    from app.observability import alerts

    class Broken:
        def zcard(self, key):
            raise RuntimeError("WRONGTYPE")

    reading = alerts.queue_reading(Broken(), thresholds_=alerts.thresholds(), now=NOW)
    assert reading.severity == "warn"
    assert "WRONGTYPE" not in reading.detail, "the class name, never the message"


# --- backup freshness ---------------------------------------------------------


def _manifest(directory: Path, *, created_at: datetime, name: str = "eva-1.json") -> Path:
    path = directory / name
    path.write_text(json.dumps({"format_version": 1, "created_at": created_at.isoformat()}))
    return path


def test_a_missing_backup_destination_is_reported(tmp_path) -> None:
    from app.observability import alerts

    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path / "absent"))
    assert reading.severity == "warn"
    assert "does not exist" in reading.detail


def test_no_manifest_means_no_backup(tmp_path) -> None:
    from app.observability import alerts

    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "warn"
    assert "no backup manifest" in reading.detail


def test_a_fresh_backup_is_ok(tmp_path) -> None:
    from app.observability import alerts

    _manifest(tmp_path, created_at=NOW - timedelta(hours=3))
    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "ok"
    assert reading.value == pytest.approx(3 * 3600, rel=1e-3)


def test_a_missed_night_is_critical(tmp_path) -> None:
    """26 h: the 02:45 daily run plus slack, so a missed night reports the same morning."""
    from app.observability import alerts

    _manifest(tmp_path, created_at=NOW - timedelta(hours=27))
    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "critical"
    assert "stale" in reading.subject


def test_the_drill_manifest_is_not_a_backup(tmp_path) -> None:
    """The drill writes `eva-<stamp>-drill.json` beside the manifests; it is a result."""
    from app.observability import alerts

    (tmp_path / "eva-1-drill.json").write_text(json.dumps({"ok": True}))
    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "warn"
    assert "no backup manifest" in reading.detail


def test_the_manifest_timestamp_wins_over_the_file_mtime(tmp_path) -> None:
    """A copy to another box carries a new mtime; the producer's stamp is the age."""
    from app.observability import alerts

    old = _manifest(tmp_path, created_at=NOW - timedelta(hours=40))
    os.utime(old, (NOW.timestamp(), NOW.timestamp()))
    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "critical", "mtime says fresh, created_at says stale"


def test_an_unreadable_manifest_falls_back_to_the_mtime(tmp_path) -> None:
    from app.observability import alerts

    path = tmp_path / "eva-1.json"
    path.write_text("not json")
    os.utime(path, ((NOW - timedelta(hours=1)).timestamp(),) * 2)
    reading = alerts.backup_reading(now=NOW, directory=str(tmp_path))
    assert reading.severity == "ok"


# --- provider error rate and budget, against the real tables -------------------


def test_provider_error_rate_reads_both_halves_from_provider_outcomes(
    migrated_db: str, alerts_env
) -> None:
    """`usage_records` records answered turns only, so a ratio from it has no numerator."""
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn)
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=16, failed=4)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            readings = alerts.provider_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds(),
                since=NOW - timedelta(hours=1),
            )
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert len(readings) == 1
    assert readings[0].value == pytest.approx(4 / 20)
    assert readings[0].severity == "warn", "20% is above the 5% warn, not above 20% critical"


def test_a_quiet_deployment_does_not_page_on_three_attempts(
    migrated_db: str, alerts_env
) -> None:
    """2/2 failed is 100%, and the attempt floor is what stops it being an alert."""
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn)
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=0, failed=2)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            readings = alerts.provider_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds(),
                since=NOW - timedelta(hours=1),
            )
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert readings[0].severity == "ok"
    assert "floor" in readings[0].detail


def test_the_window_is_applied_so_a_fixed_outage_stops_alerting(
    migrated_db: str, alerts_env
) -> None:
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn)
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=0, failed=20)
        conn.execute(
            "UPDATE provider_outcomes SET created_at = %s WHERE tenant_id = %s",
            (NOW - timedelta(hours=2), tenant),
        )
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            readings = alerts.provider_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds(),
                since=NOW - timedelta(seconds=300),
            )
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert readings == [], "outside the window there are no attempts, so no series"


def test_budget_readings_read_the_same_ledger_the_hard_stop_reads(
    migrated_db: str, alerts_env
) -> None:
    """The alert and CONV-14 must not disagree about how much of the month is spent."""
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn, '{"monthly_token_budget": 1000}')
        conv = _conversation(conn, tenant)
        _usage(conn, tenant, conv, 950)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            reading = alerts.budget_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds()
            )[0]
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert reading.severity == "warn"
    assert reading.value == pytest.approx(0.95)
    assert reading.threshold == 0.90


def test_budget_at_the_limit_is_critical_because_turns_are_refused(
    migrated_db: str, alerts_env
) -> None:
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn, '{"monthly_token_budget": 1000}')
        conv = _conversation(conn, tenant)
        _usage(conn, tenant, conv, 1000)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            reading = alerts.budget_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds()
            )[0]
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert reading.severity == "critical"
    assert "CONV-14" in reading.detail


def test_a_tenant_below_its_budget_is_ok(migrated_db: str, alerts_env) -> None:
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn, '{"monthly_token_budget": 1000}')
        conv = _conversation(conn, tenant)
        _usage(conn, tenant, conv, 10)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            reading = alerts.budget_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds()
            )[0]
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert reading.severity == "ok"


# --- the sweep -----------------------------------------------------------------


def test_the_sweep_covers_all_five_signals(migrated_db: str, alerts_env, tmp_path) -> None:
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn)
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=19, failed=1)
        conn.commit()

    readings = alerts.evaluate(
        now=NOW, client=FakeRedis(0), disk_path=str(tmp_path), backup_dir=str(tmp_path)
    )
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    signals = {reading.signal for reading in readings}
    assert signals == {
        "provider_error_rate",
        "budget_exhaustion",
        "disk_usage",
        "backup_failure",
        "queue_backlog",
    }


def test_a_tenant_override_applies_to_that_tenant_only(
    migrated_db: str, alerts_env, tmp_path
) -> None:
    """Two tenants, one tight threshold pair; the sweep must use each tenant's own."""
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        strict = _tenant(
            conn,
            '{"alerts": {"provider_error_rate": {"warn": 0.01, "critical": 0.06,'
            ' "min_attempts": 2}}}',
        )
        loose = _tenant(conn)
        for tenant in (strict, loose):
            conv = _conversation(conn, tenant)
            _outcomes(conn, tenant, conv, provider="p", ok=19, failed=1)
        conn.commit()

    readings = alerts.evaluate(
        now=NOW, client=FakeRedis(0), disk_path=str(tmp_path), backup_dir=str(tmp_path)
    )
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", ([strict, loose],))
        conn.commit()

    provider = {
        reading.tenant_id: reading.severity
        for reading in readings
        if reading.signal == "provider_error_rate"
    }
    assert provider[strict] == "warn", "5% is above this tenant's 1% warn, under its 6% critical"
    assert provider[loose] == "ok", "5% is inside the operator default"


def test_alert_text_carries_no_conversation_content(migrated_db: str, alerts_env) -> None:
    """The text leaves the box by email; a canary in `messages` must not ride along."""
    alerts = alerts_env
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn, '{"monthly_token_budget": 10}')
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=0, failed=50)
        _usage(conn, tenant, conv, 100)
        conn.commit()

        with alerts.db_session.tenant_session(uuid.UUID(tenant)) as session:
            readings = alerts.provider_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds(),
                since=NOW - timedelta(hours=1),
            ) + alerts.budget_readings(
                session, tenant_id=tenant, thresholds_=alerts.thresholds()
            )
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()

    assert readings, "the sweep must have produced something to inspect"
    for reading in readings:
        subject, body = alerts._message(reading, NOW)
        assert CANARY not in subject and CANARY not in body
        assert "visit-" not in body, "no visitor session id either"
        assert tenant in body, "the tenant id is what makes the alert actionable"


# --- transitions and delivery --------------------------------------------------


def test_a_warning_is_announced_once_and_cleared_on_recovery(
    migrated_db: str, alerts_env, monkeypatch
) -> None:
    import app.config

    alerts = alerts_env
    monkeypatch.setattr(app.config, "ALERT_EMAIL", "ops@example.test")
    channel = RecordingChannel()

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn)
        conv = _conversation(conn, tenant)
        _outcomes(conn, tenant, conv, provider="ollama_cloud", ok=18, failed=2)
        conn.commit()

    kwargs = dict(now=NOW, client=FakeRedis(0), channel=channel, disk_path="/")
    alerts.run_once(backup_dir="/nonexistent-backup-dir", **kwargs)
    first = len(channel.sent)
    assert first >= 1, "a new warning is announced"

    alerts.run_once(backup_dir="/nonexistent-backup-dir", **kwargs)
    assert len(channel.sent) == first, "a still-active warning is not re-sent"

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM provider_outcomes WHERE tenant_id = %s", (tenant,))
        conn.commit()
    report = alerts.run_once(backup_dir="/nonexistent-backup-dir", **kwargs)
    assert any(key.startswith("provider_error_rate") for key in report["recovered"])
    assert not [
        key for key in alerts._STATE if key.startswith("provider_error_rate")
    ], "a cleared scope is forgotten, so it can alert again"
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()


def test_an_escalation_is_announced_even_though_the_scope_is_remembered() -> None:
    from app.observability import alerts

    channel = RecordingChannel()
    state = {"disk_usage": "warn"}
    warn = alerts.Reading(
        signal="disk_usage", key="disk_usage", severity="warn", subject="disk", detail="d"
    )
    critical = alerts.Reading(
        signal="disk_usage", key="disk_usage", severity="critical", subject="disk", detail="d"
    )
    assert alerts.dispatch([warn], NOW, state=state, channel=channel)["sent"] == []
    report = alerts.dispatch([critical], NOW, state=state, channel=channel)
    assert report["sent"][0]["severity"] == "critical"
    assert report["sent"][0]["from"] == "warn"


def test_an_absent_alert_email_logs_instead_of_silence(monkeypatch, caplog) -> None:
    """HALT-09 point 3 — an unset channel must be as visible as a sent alert."""
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_EMAIL", "")
    channel = RecordingChannel()
    reading = alerts.Reading(
        signal="disk_usage", key="disk_usage", severity="warn", subject="disk", detail="d"
    )
    assert alerts.send(reading, NOW, channel=channel) == "logged"
    assert channel.sent == []
    assert "ALERT_EMAIL is not set" in caplog.text


def test_an_unconfigured_smtp_is_reported_as_logged(monkeypatch) -> None:
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_EMAIL", "ops@example.test")
    monkeypatch.setattr(alerts.channels, "build_channel", lambda: None)
    reading = alerts.Reading(
        signal="disk_usage", key="disk_usage", severity="warn", subject="disk", detail="d"
    )
    assert alerts.send(reading, NOW) == "logged"


def test_a_delivery_failure_is_a_value_not_an_exception(monkeypatch) -> None:
    """An SMTP outage must not abort the sweep and lose the other signals' alerts."""
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_EMAIL", "ops@example.test")
    reading = alerts.Reading(
        signal="disk_usage", key="disk_usage", severity="warn", subject="disk", detail="d"
    )
    assert alerts.send(reading, NOW, channel=RecordingChannel(ok=False)) == "failed"


def test_the_recipient_is_the_operator_and_never_a_tenant(monkeypatch) -> None:
    """A platform alert names tenant ids; a tenant's handover address must never get one."""
    import app.config
    from app.observability import alerts

    monkeypatch.setattr(app.config, "ALERT_EMAIL", "ops@example.test")
    channel = RecordingChannel()
    reading = alerts.Reading(
        signal="budget_exhaustion", key="k", severity="warn", subject="s", detail="d",
        tenant_id="00000000-0000-0000-0000-000000000001",
    )
    alerts.send(reading, NOW, channel=channel)
    assert [n.to for n in channel.sent] == ["ops@example.test"]


def test_an_unset_alert_email_does_not_fall_back_to_a_tenant(
    migrated_db: str, alerts_env, monkeypatch
) -> None:
    """HALT-09 point 3, in the direction the previous test cannot see.

    With ``ALERT_EMAIL`` set, *any* fallback is invisible: the first address wins and the
    tenant's is never consulted. So the fallback is tested where it would fire — a tenant
    that has a handover address configured, and no operator address. The alert must be
    logged, not mailed to the customer.
    """
    import app.config

    alerts = alerts_env
    monkeypatch.setattr(app.config, "ALERT_EMAIL", "")
    # The platform's own sender address is not a recipient either: it exists so mail has a
    # From, and using it as a To would leave a deployment looking configured while every
    # alert went to a mailbox nobody reads.
    monkeypatch.setattr(app.config, "SMTP_FROM", "no-reply@eva.test")
    channel = RecordingChannel()

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        tenant = _tenant(conn, '{"handover": {"email": "owner@customer.example"}}')
        conn.commit()

    reading = alerts.Reading(
        signal="budget_exhaustion", key="k", severity="warn", subject="s", detail="d",
        tenant_id=tenant,
    )
    assert alerts.send(reading, NOW, channel=channel) == "logged"
    assert channel.sent == [], "a platform alert must not reach the tenant's mailbox"

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
        conn.commit()


def test_the_report_is_checkable_when_nothing_fires() -> None:
    """A quiet system is only distinguishable from a dead poller by a stored report."""
    from app.observability import alerts

    report = alerts.dispatch([], NOW, state={}, channel=RecordingChannel())
    assert report == {"sent": [], "recovered": []}


# --- the worker wiring --------------------------------------------------------


def test_the_poller_is_a_minute_cron_beside_the_purge() -> None:
    """The schedule is the row's, so it is asserted rather than assumed."""
    from worker.settings import WorkerSettings

    crons = {job.name: job for job in WorkerSettings.cron_jobs}
    assert "cron:purge_expired" in crons
    alert = crons["cron:check_alerts"]
    assert (alert.minute, alert.hour) == (None, None), "every minute"
    assert alert.unique is True, "one evaluation per tick across workers"
    assert "check_alerts" in [f.__name__ for f in WorkerSettings.functions]


def test_the_job_returns_the_sweep_report(monkeypatch) -> None:
    from worker import alert_job

    expected = {"worst": "ok", "sent": [], "recovered": []}
    monkeypatch.setattr(alert_job.alerts, "run_once", lambda: expected)
    assert asyncio.run(alert_job.check_alerts({})) == expected


def test_the_module_runs_in_the_worker_image() -> None:
    """The cron imports it there, and that image has no FastAPI (§8.33's measurement).

    A `from app.observability import metrics` would look harmless and break the worker at
    startup, which is why `QUEUE_KEY` is copied and a test pins the two together.
    """
    tree = ast.parse(ALERTS_SRC.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    root = {name.split(".")[0] for name in imported}
    assert "fastapi" not in root
    assert not {"app.observability.metrics", "app.auth.dependencies"} & imported
    assert "app.conversation" in imported, (
        "budget_readings must reuse the hard stop's ledger reader"
    )


# --- configuration and the runbook -------------------------------------------


def test_the_alert_channel_is_declared_in_the_env_example() -> None:
    """An operator cannot configure a key the example file does not name."""
    text = ENV_EXAMPLE.read_text()
    assert "ALERT_EMAIL=" in text
    assert "ALERT_BACKUP_DIR=" in text


def test_no_real_address_is_committed() -> None:
    """`.env.example` carries a reserved-domain placeholder, never a real mailbox.

    The check is on the domain, not on the presence of `@`: a placeholder has to look like
    an address to be usable as one, and RFC 2606 reserves `.invalid` precisely so an
    example cannot be delivered to by accident.
    """
    line = next(
        ln for ln in ENV_EXAMPLE.read_text().splitlines() if ln.startswith("ALERT_EMAIL=")
    )
    domain = line.split("=", 1)[1].rsplit("@", 1)[-1]
    assert domain.endswith((".invalid", ".example", ".test")) or not domain, (
        f"a real address would be committed: {line}"
    )


def test_the_runbook_documents_a_response_for_every_alert() -> None:
    """§27 asks for the runbook in the same breath as the alerts."""
    text = RUNBOOK.read_text()
    assert "## Alerts" in text
    for signal in (
        "provider error rate",
        "budget exhaustion",
        "disk usage",
        "backup",
        "queue backlog",
    ):
        assert signal in text.lower(), f"{signal} has no response procedure"
    assert "ALERT_EMAIL" in text
    assert "python -m app.observability.alerts" in text, "one command to see the readings"
