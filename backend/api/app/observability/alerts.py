"""The five §22 alerts, evaluated from the store and delivered to ``ALERT_EMAIL`` (OPS-04).

§22 lists the alerts and §27's launch checklist asks for *"Alerts firing to a monitored
address; runbook written"*. HALT-09 (PLAN §8.25 RULINGS) ruled **Reading B**: the two
signals whose events were never recorded became prerequisites (OPS-12's per-turn provider
outcome and real token ledger, OPS-09's backup producer), and only then does a poller
mean anything. Both landed, so this module is the poller.

**Defaults are industry values, not policy.** HALT-09 fixed them — 5%/20% provider error
rate over five minutes, 90%/100% of a tenant's monthly budget, 85%/95% disk, any failed
or missed backup, 100/500 queue depth sustained five minutes — and made them
operator-tunable without a code deploy. They live in ``app.config`` (environment) and the
two tenant-scoped ones can also be overridden per tenant in ``tenants.settings ->
alerts``, the same read-only JSONB path ``conversation.controls.monthly_budget`` already
uses. A threshold is a number an operator changes; a *code* change per customer would be
the wrong seam.

**Two signals are platform-wide and two are per tenant.** Disk, queue and backup describe
the deployment; provider error rate and budget describe a tenant. The per-tenant ones are
read the only way ``eva_app`` can read them — one tenant in a scoped session each —
because RLS makes a cross-tenant aggregate return zero rows rather than raise
(``metrics.py`` records the same measurement).

**Delivery degrades visibly, never silently.** HALT-09 point 3: ``ALERT_EMAIL`` is the
operator's mailbox, never a tenant's handover address. Absent, or with SMTP unconfigured,
an alert is logged at warning with its signal and severity and the report says
``logged`` — the channel being unset must be as visible as an alert being sent. A real
provider is a ``.env`` change (the ``EmailChannel`` is the existing handover channel),
never a code path.

**State is per process.** The worker runs one process (§20.1 allows two; the container
runs one), and arq's ``unique`` cron means one evaluation per tick across workers. The
consequence, stated rather than hidden: a restart re-announces an alert that is still
active and restarts the queue-backlog window. OPS-03's counters make the same trade. Redis
would hold the state; using it would mean an alerting loop that cannot report a Redis
outage because it needs Redis to report it.

**Alert text carries no conversation content.** A signal, a severity, a tenant id, a
number and a threshold — the same rule OPS-01's log guard and OPS-02's spans follow,
because this text leaves the box by email.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import config
from app.conversation import controls
from app.db import session as db_session
from app.handover import channels

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
CRITICAL = "critical"

# Readings are keyed by signal *and* scope so a transition for one provider or one
# tenant does not suppress another's. Never keyed by anything a client supplies.
SEVERITY_RANK = {OK: 0, WARN: 1, CRITICAL: 2}

# A provider key that outlived its configuration is labelled, not dropped: an absent
# series and "no traffic" must not look the same (OPS-03 made the same choice).
UNKNOWN_PROVIDER = "(unknown)"

# Copied from ``metrics.QUEUE_KEY`` rather than imported: ``metrics`` imports FastAPI,
# which the worker image does not have, and this module has to run there. A test asserts
# the two constants stay equal.
QUEUE_KEY = "arq:queue"

# Backup manifests are `eva-<stamp>.json`; the drill writes `eva-<stamp>-drill.json` beside
# them and a drill result is not a backup.
MANIFEST_PREFIX = "eva-"
MANIFEST_SUFFIX = ".json"
DRILL_SUFFIX = "-drill.json"

# `_STATE` is the process's memory of what was last announced, key -> severity. Module
# level on purpose: the cron job runs in the worker process and the store is not shared.
_STATE: dict[str, str] = {}

# When the queue was first seen above the warn depth, for the sustained condition.
_QUEUE_SINCE: dict[str, datetime] = {}


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Thresholds:
    """The numbers one evaluation uses. Built per tenant, so an override is local."""

    provider_error_warn: float
    provider_error_critical: float
    provider_min_attempts: int
    budget_warn: float
    budget_critical: float
    disk_warn: float
    disk_critical: float
    queue_warn: int
    queue_critical: int
    queue_sustain_seconds: int

    def __post_init__(self) -> None:
        for name, low, high in (
            ("provider_error", self.provider_error_warn, self.provider_error_critical),
            ("budget", self.budget_warn, self.budget_critical),
            ("disk", self.disk_warn, self.disk_critical),
        ):
            if not 0.0 < low <= high <= 1.0:
                raise ValueError(f"{name} thresholds must be 0 < warn <= critical <= 1")
        if not 0 <= self.queue_warn <= self.queue_critical:
            raise ValueError("queue thresholds must be 0 <= warn <= critical")
        if self.provider_min_attempts < 1:
            raise ValueError("provider_min_attempts must be at least 1")
        if self.queue_sustain_seconds < 0:
            raise ValueError("queue_sustain_seconds must not be negative")


def _fraction(value: Any, fallback: float) -> float:
    """A stored/env threshold as a fraction, or the fallback.

    Tolerant on read like every policy reader here (``retention.policy``, handover
    ``policy.settings``): ``tenants.settings`` is operator-editable JSON, and a
    hand-edited ``"5%"`` must not decide how a tenant's outage is reported. ``bool`` is
    refused explicitly because ``True`` is an ``int`` subclass in Python.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not 0.0 < float(value) <= 1.0:
        return fallback
    return float(value)


def _count(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value if value >= 0 else fallback


def thresholds(tenant_settings: Any = None) -> Thresholds:
    """Operator defaults from the environment, with a tenant's overrides applied.

    Only the two tenant-scoped signals can be overridden per tenant. Disk, queue and
    backup describe the deployment, and letting one tenant change the platform's disk
    threshold would let one customer silence an alert about everyone's disk.

    An override that inverts its own pair falls back to the operator's pair rather than
    raising: ``tenants.settings`` is operator-editable JSON, and a merged pair that
    failed validation would make every evaluation of that tenant raise — a way to hide an
    alert rather than configure one.
    """
    base = Thresholds(
        provider_error_warn=config.ALERT_PROVIDER_ERROR_WARN,
        provider_error_critical=config.ALERT_PROVIDER_ERROR_CRITICAL,
        provider_min_attempts=config.ALERT_PROVIDER_MIN_ATTEMPTS,
        budget_warn=config.ALERT_BUDGET_WARN,
        budget_critical=config.ALERT_BUDGET_CRITICAL,
        disk_warn=config.ALERT_DISK_WARN,
        disk_critical=config.ALERT_DISK_CRITICAL,
        queue_warn=config.ALERT_QUEUE_WARN,
        queue_critical=config.ALERT_QUEUE_CRITICAL,
        queue_sustain_seconds=config.ALERT_QUEUE_SUSTAIN_SECONDS,
    )
    raw = tenant_settings if isinstance(tenant_settings, dict) else {}
    block = raw.get("alerts")
    if not isinstance(block, dict):
        return base

    provider = block.get("provider_error_rate")
    provider = provider if isinstance(provider, dict) else {}
    budget = block.get("budget")
    budget = budget if isinstance(budget, dict) else {}

    provider_warn = _fraction(provider.get("warn"), base.provider_error_warn)
    provider_critical = _fraction(
        provider.get("critical"), base.provider_error_critical
    )
    if provider_warn > provider_critical:
        provider_warn, provider_critical = (
            base.provider_error_warn,
            base.provider_error_critical,
        )

    budget_warn = _fraction(budget.get("warn"), base.budget_warn)
    budget_critical = _fraction(budget.get("critical"), base.budget_critical)
    if budget_warn > budget_critical:
        budget_warn, budget_critical = base.budget_warn, base.budget_critical

    return replace(
        base,
        provider_error_warn=provider_warn,
        provider_error_critical=provider_critical,
        provider_min_attempts=_count(
            provider.get("min_attempts"), base.provider_min_attempts
        ),
        budget_warn=budget_warn,
        budget_critical=budget_critical,
    )


# --------------------------------------------------------------------------- #
# A reading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reading:
    """One signal's verdict for one scope.

    ``key`` identifies the scope for transition tracking. ``detail`` is the sentence a
    human reads in the email: it may name a tenant id, a provider key, a number and a
    threshold, and nothing else.
    """

    signal: str
    key: str
    severity: str
    subject: str
    detail: str
    value: float | None = None
    threshold: float | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity!r}")


# --------------------------------------------------------------------------- #
# Per-tenant signals: provider error rate, budget
# --------------------------------------------------------------------------- #


def _tenants() -> list[dict[str, Any]]:
    """Every active tenant with the settings the overrides live in.

    ``tenants`` carries no RLS policy, so this cross-tenant read is deliberate and the
    only one here — the same shape ``worker.purge_job._tenants`` uses.
    """
    with db_session.SessionLocal() as session:
        session.begin()
        rows = session.execute(
            text(
                "SELECT id::text AS id, name, settings FROM tenants"
                " WHERE status = 'active' ORDER BY id"
            )
        ).mappings().all()
        session.commit()
    return [dict(row) for row in rows]


def provider_readings(
    session: Any,
    *,
    tenant_id: str,
    thresholds_: Thresholds,
    since: datetime,
) -> list[Reading]:
    """Error rate per provider over the window (§22 "provider error-rate spike").

    Both halves of the ratio come from ``provider_outcomes``, which OPS-12 writes on the
    degraded path as well as the answered one. Reading ``usage_records`` instead would
    make the numerator structurally zero, because that table records answered turns.

    ``provider_min_attempts`` is a noise floor, not a hidden threshold: three failures
    out of three turns is 100% and would page on the first bad minute of a quiet
    deployment. HALT-09 fixed the percentages and not this, so it is a stated default
    (10) that an operator or tenant changes like the rest.
    """
    rows = session.execute(
        text(
            """
            SELECT COALESCE(provider, :unknown) AS provider,
                   count(*) FILTER (WHERE outcome = 'success') AS ok,
                   count(*) FILTER (WHERE outcome = 'failure') AS failed,
                   count(*) AS attempts
              FROM provider_outcomes
             WHERE tenant_id = :tenant_id AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """
        ),
        {"tenant_id": tenant_id, "since": since, "unknown": UNKNOWN_PROVIDER},
    ).mappings().all()

    readings: list[Reading] = []
    for row in rows:
        attempts = int(row["attempts"])
        failed = int(row["failed"])
        rate = failed / attempts if attempts else 0.0
        subject_base = f"provider {row['provider']} error rate"
        if attempts < thresholds_.provider_min_attempts:
            severity = OK
            detail = (
                f"{failed}/{attempts} provider attempts failed in the last"
                f" {config.ALERT_WINDOW_SECONDS}s; below the"
                f" {thresholds_.provider_min_attempts} attempt floor, so no rate is"
                " claimed"
            )
        elif rate > thresholds_.provider_error_critical:
            severity = CRITICAL
            detail = (
                f"{failed}/{attempts} ({rate:.1%}) provider attempts failed in the last"
                f" {config.ALERT_WINDOW_SECONDS}s, above the critical"
                f" {thresholds_.provider_error_critical:.0%} threshold"
            )
        elif rate > thresholds_.provider_error_warn:
            severity = WARN
            detail = (
                f"{failed}/{attempts} ({rate:.1%}) provider attempts failed in the last"
                f" {config.ALERT_WINDOW_SECONDS}s, above the warn"
                f" {thresholds_.provider_error_warn:.0%} threshold"
            )
        else:
            severity = OK
            detail = (
                f"{failed}/{attempts} ({rate:.1%}) provider attempts failed in the last"
                f" {config.ALERT_WINDOW_SECONDS}s"
            )
        readings.append(
            Reading(
                signal="provider_error_rate",
                key=f"provider_error_rate:{tenant_id}:{row['provider']}",
                severity=severity,
                subject=f"{subject_base} at {rate:.1%} for tenant {tenant_id}",
                detail=detail,
                value=rate,
                threshold=(
                    thresholds_.provider_error_critical
                    if severity == CRITICAL
                    else thresholds_.provider_error_warn
                    if severity == WARN
                    else None
                ),
                tenant_id=tenant_id,
            )
        )
    return readings


def budget_readings(
    session: Any,
    *,
    tenant_id: str,
    thresholds_: Thresholds,
) -> list[Reading]:
    """Monthly budget exhaustion (§22 "budget exhaustion", R31, CONV-14).

    Read through ``conversation.controls`` rather than re-querying ``usage_records``, so
    the alert and the hard stop that refuses the turn cannot disagree about how much of
    the month is spent. The ledger was structurally zero until OPS-12; the threshold is
    only meaningful now that the numerator exists.
    """
    used = controls.monthly_token_usage(session, tenant_id=UUID(tenant_id))
    budget = controls.monthly_budget(session, tenant_id=UUID(tenant_id))
    ratio = used / budget if budget > 0 else 0.0

    if ratio >= thresholds_.budget_critical:
        severity = CRITICAL
        detail = (
            f"{used}/{budget} tokens used this month ({ratio:.0%}): at or above the"
            f" {thresholds_.budget_critical:.0%} critical threshold, so turns are being"
            " refused with the offline message (CONV-14)"
        )
    elif ratio >= thresholds_.budget_warn:
        severity = WARN
        detail = (
            f"{used}/{budget} tokens used this month ({ratio:.0%}), above the warn"
            f" {thresholds_.budget_warn:.0%} threshold"
        )
    else:
        severity = OK
        detail = f"{used}/{budget} tokens used this month ({ratio:.0%})"

    return [
        Reading(
            signal="budget_exhaustion",
            key=f"budget_exhaustion:{tenant_id}",
            severity=severity,
            subject=f"tenant {tenant_id} at {ratio:.0%} of its monthly token budget",
            detail=detail,
            value=ratio,
            threshold=(
                thresholds_.budget_critical
                if severity == CRITICAL
                else thresholds_.budget_warn
                if severity == WARN
                else None
            ),
            tenant_id=tenant_id,
        )
    ]


# --------------------------------------------------------------------------- #
# Platform signals: disk, queue backlog, backup
# --------------------------------------------------------------------------- #


def disk_reading(
    *, thresholds_: Thresholds, path: str | None = None
) -> Reading:
    """Filesystem usage (§22 "disk usage").

    ``shutil.disk_usage`` is stdlib and, inside the worker, reports the host filesystem
    the container's overlay and the ``pgdata`` volume both sit on — measured as
    ``total=336746889216`` in both the api and worker containers (§8.33). The API image
    is capped at 512 MiB but its disk is the same disk, and a full ``pgdata`` is the
    failure this alert exists for.
    """
    target = path or config.ALERT_DISK_PATH
    usage = shutil.disk_usage(target)
    ratio = usage.used / usage.total if usage.total else 0.0
    human = f"{usage.used / 2**30:.1f} GiB of {usage.total / 2**30:.1f} GiB"

    if ratio >= thresholds_.disk_critical:
        severity = CRITICAL
    elif ratio >= thresholds_.disk_warn:
        severity = WARN
    else:
        severity = OK
    return Reading(
        signal="disk_usage",
        key="disk_usage",
        severity=severity,
        subject=f"disk usage at {ratio:.0%} on {target}",
        detail=(
            f"{human} used on {target} ({ratio:.1%}); thresholds are"
            f" {thresholds_.disk_warn:.0%} warn / {thresholds_.disk_critical:.0%} critical"
        ),
        value=ratio,
        threshold=(
            thresholds_.disk_critical
            if severity == CRITICAL
            else thresholds_.disk_warn
            if severity == WARN
            else None
        ),
    )


def queue_reading(
    client: Any,
    *,
    thresholds_: Thresholds,
    now: datetime,
    state: dict[str, datetime] | None = None,
) -> Reading:
    """arq backlog (§22 "queue backlog"), sustained.

    ``ZCARD`` of ``arq:queue``, not ``LLEN``: arq pushes with ``zadd`` so the queue is a
    sorted set and ``LLEN`` answers ``WRONGTYPE`` (measured in the live Redis, and
    ``metrics.QUEUE_KEY`` records the same). Unreachable Redis is a **warn**, not an
    ``ok``: "the queue is empty" and "I cannot see the queue" must not look the same,
    which is OPS-03's rule applied to an alert that would otherwise stay silent through
    the outage it exists for.
    """
    if client is None:
        return Reading(
            signal="queue_backlog",
            key="queue_backlog",
            severity=WARN,
            subject="arq queue depth is not readable",
            detail=(
                "Redis is not reachable, so the queue depth is unknown. This is an alert"
                " rather than a silence: an unreadable queue cannot be an empty one."
            ),
        )
    try:
        depth = int(client.zcard(QUEUE_KEY))
    except Exception as exc:  # noqa: BLE001 - an alert sweep must not die on a side-car
        logger.warning("queue depth unavailable (%s)", type(exc).__name__)
        return Reading(
            signal="queue_backlog",
            key="queue_backlog",
            severity=WARN,
            subject="arq queue depth is not readable",
            detail=(
                f"Redis answered with {type(exc).__name__}, so the queue depth is"
                " unknown; treat this as a backlog alert, not as an empty queue"
            ),
        )

    memory = _QUEUE_SINCE if state is None else state
    if depth > thresholds_.queue_critical:
        memory.pop("queue_backlog", None)
        return Reading(
            signal="queue_backlog",
            key="queue_backlog",
            severity=CRITICAL,
            subject=f"arq queue depth at {depth}",
            detail=(
                f"{depth} jobs queued, above the critical {thresholds_.queue_critical}"
                f" threshold (§20.3's trigger is to scale the worker count)"
            ),
            value=float(depth),
            threshold=float(thresholds_.queue_critical),
        )

    if depth > thresholds_.queue_warn:
        first_seen = memory.setdefault("queue_backlog", now)
        elapsed = (now - first_seen).total_seconds()
        if elapsed >= thresholds_.queue_sustain_seconds:
            return Reading(
                signal="queue_backlog",
                key="queue_backlog",
                severity=WARN,
                subject=f"arq queue depth at {depth}",
                detail=(
                    f"{depth} jobs queued for {elapsed:.0f}s, above the warn"
                    f" {thresholds_.queue_warn} threshold"
                ),
                value=float(depth),
                threshold=float(thresholds_.queue_warn),
            )
        return Reading(
            signal="queue_backlog",
            key="queue_backlog",
            severity=OK,
            subject=f"arq queue depth at {depth}",
            detail=(
                f"{depth} jobs queued for {elapsed:.0f}s: above the warn"
                f" {thresholds_.queue_warn} threshold but inside the"
                f" {thresholds_.queue_sustain_seconds}s sustain window"
            ),
            value=float(depth),
        )

    memory.pop("queue_backlog", None)
    return Reading(
        signal="queue_backlog",
        key="queue_backlog",
        severity=OK,
        subject=f"arq queue depth at {depth}",
        detail=f"{depth} jobs queued, at or below the warn {thresholds_.queue_warn} threshold",
        value=float(depth),
    )


def backup_reading(
    *, now: datetime, directory: str | None = None, max_age_seconds: int | None = None
) -> Reading:
    """Backup freshness (§22 "backup failure").

    OPS-09 writes ``eva-<stamp>.json`` (a manifest with row counts, the alembic revision
    and the dump's checksum) beside each backup set, and the daily systemd timer exits
    non-zero when a run fails. This alert reads the manifests, so it fires on **no run**
    (``missing``/``empty``), on a run that never completed (**stale**) and on an
    unreadable destination — the three ways "the backup did not happen" presents.

    **Stated limit: a run that fails mid-dump leaves no manifest, so it is reported as
    stale once the window passes rather than as a distinct failure.** The producer writes
    nothing on failure, and adding a failure marker is OPS-09's file, not this row's. The
    window (26 h) is the 02:45 UTC daily schedule plus two hours of slack, so a missed
    night is reported the same morning rather than the next.
    """
    target = Path(directory or config.ALERT_BACKUP_DIR)
    window = int(
        max_age_seconds
        if max_age_seconds is not None
        else config.ALERT_BACKUP_MAX_AGE_SECONDS
    )
    hours = window / 3600

    if not target.is_dir():
        return Reading(
            signal="backup_failure",
            key="backup_failure",
            severity=WARN,
            subject="backup destination is not visible",
            detail=(
                f"{target} does not exist inside this container, so no backup can be"
                " verified. Set ALERT_BACKUP_DIR to the backup destination, which the"
                " worker mounts read-only (see docs/ops/RUNBOOK.md)."
            ),
        )

    try:
        manifests = sorted(
            path
            for path in target.glob(f"{MANIFEST_PREFIX}*{MANIFEST_SUFFIX}")
            if not path.name.endswith(DRILL_SUFFIX)
        )
    except OSError as exc:
        return Reading(
            signal="backup_failure",
            key="backup_failure",
            severity=WARN,
            subject="backup destination is unreadable",
            detail=f"{target} could not be listed ({type(exc).__name__}), so no backup is verified",
        )

    if not manifests:
        return Reading(
            signal="backup_failure",
            key="backup_failure",
            severity=WARN,
            subject="no backup found",
            detail=(
                f"{target} holds no backup manifest, so no backup has completed. Run"
                " `python scripts/ops09_backup.py backup` or check the eva-backup timer."
            ),
        )

    newest = max(manifests, key=lambda path: path.stat().st_mtime)
    created = _manifest_time(newest) or datetime.fromtimestamp(
        newest.stat().st_mtime, UTC
    )
    age = (now - created).total_seconds()
    if age > window:
        return Reading(
            signal="backup_failure",
            key="backup_failure",
            severity=CRITICAL,
            subject="backup is stale",
            detail=(
                f"the newest backup ({newest.name}) is {age / 3600:.1f} h old, beyond the"
                f" {hours:.0f} h window (a failed run leaves no manifest, so a failure"
                " appears here as staleness)"
            ),
            value=age,
            threshold=float(window),
        )
    return Reading(
        signal="backup_failure",
        key="backup_failure",
        severity=OK,
        subject=f"backup is current ({newest.name})",
        detail=f"the newest backup is {age / 3600:.1f} h old, inside the {hours:.0f} h window",
        value=age,
        threshold=float(window),
    )


def _manifest_time(path: Path) -> datetime | None:
    """``created_at`` from the manifest, when it is readable.

    Preferred to the file's mtime because a backup copied to another box carries a new
    mtime; the timestamp the producer wrote is what "how old is the copy" means.
    """
    try:
        payload = json.loads(path.read_text())
        stamp = str(payload.get("created_at") or "")
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def redis_client() -> Any:
    """A Redis client for the queue reading, or ``None`` when Redis is unreachable.

    ``redis`` is a hard ``Requires-Dist`` of the pinned ``arq==0.28.0`` and is in both
    images, so this is not a new dependency — the same measurement HALT-09 recorded.
    """
    try:
        import redis

        client = redis.Redis.from_url(
            os.environ.get("REDIS_URL", config.REDIS_URL),
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - reported as a reading, not raised
        logger.warning("queue backlog: Redis unavailable (%s)", type(exc).__name__)
        return None
    return client


def evaluate(
    *,
    now: datetime | None = None,
    client: Any = None,
    queue_state: dict[str, datetime] | None = None,
    disk_path: str | None = None,
    backup_dir: str | None = None,
) -> list[Reading]:
    """Every signal's verdict, tenants iterated one scoped session each.

    Returns ``ok`` readings too: the caller needs them to clear a previously announced
    alert, and a report that only lists problems cannot show that the sweep ran.
    """
    moment = now or datetime.now(UTC)
    readings: list[Reading] = []

    platform = thresholds()
    readings.append(disk_reading(thresholds_=platform, path=disk_path))
    readings.append(backup_reading(now=moment, directory=backup_dir))
    readings.append(
        queue_reading(client, thresholds_=platform, now=moment, state=queue_state)
    )

    since = moment - timedelta(seconds=config.ALERT_WINDOW_SECONDS)
    for tenant in _tenants():
        own = thresholds(tenant["settings"])
        with db_session.tenant_session(UUID(tenant["id"])) as session:
            readings.extend(
                provider_readings(
                    session, tenant_id=tenant["id"], thresholds_=own, since=since
                )
            )
            readings.append(
                budget_readings(session, tenant_id=tenant["id"], thresholds_=own)[0]
            )
    return readings


def _message(reading: Reading, moment: datetime) -> tuple[str, str]:
    """Subject and body for one alert. Ids, numbers and thresholds only."""
    subject = f"[EVA {reading.severity.upper()}] {reading.subject}"
    lines = [
        f"Signal:   {reading.signal}",
        f"Severity: {reading.severity}",
        f"At:       {moment.isoformat(timespec='seconds')}",
    ]
    if reading.tenant_id:
        lines.append(f"Tenant:   {reading.tenant_id}")
    if reading.value is not None:
        lines.append(f"Value:    {reading.value}")
    if reading.threshold is not None:
        lines.append(f"Threshold: {reading.threshold}")
    lines += ["", reading.detail, "", "Response procedure: docs/ops/RUNBOOK.md (§ Alerts)."]
    return subject, "\n".join(lines)


def send(reading: Reading, moment: datetime, *, channel: Any = None) -> str:
    """Deliver one alert: ``sent``, ``logged`` (no recipient/transport) or ``failed``.

    ``ALERT_EMAIL`` is the operator's mailbox and never a tenant's handover address
    (HALT-09 point 3): a platform alert carries tenant ids and provider names, which is
    deployment topology a customer did not ask for. Absent, the alert is logged with its
    severity and the report says so — silence would be indistinguishable from a quiet
    system.
    """
    recipient = config.ALERT_EMAIL
    subject, body = _message(reading, moment)
    if not recipient:
        logger.warning(
            "alert not delivered: ALERT_EMAIL is not set (signal=%s severity=%s)",
            reading.signal,
            reading.severity,
        )
        return "logged"

    sender = channel if channel is not None else channels.build_channel()
    if sender is None:
        logger.warning(
            "alert not delivered: SMTP is not configured (signal=%s severity=%s)",
            reading.signal,
            reading.severity,
        )
        return "logged"

    delivery = sender.send(
        channels.Notification(
            channel="email", to=recipient, subject=subject, body=body
        )
    )
    if not delivery.ok:
        logger.warning("alert delivery failed (%s)", delivery.detail[:200])
        return "failed"
    return "sent"


def dispatch(
    readings: list[Reading],
    moment: datetime,
    *,
    state: dict[str, str] | None = None,
    channel: Any = None,
) -> dict[str, Any]:
    """Announce transitions, not states.

    A poller every minute that re-sent the same warning sixty times an hour is an alert
    channel an operator filters into a folder, which is how alerts get ignored (§13's
    noise-control reasoning for handover email, applied to the platform's own mail). So
    the rule is: send when a scope's severity **changes** to warn or critical, stay
    silent while it stays there, and clear the memory when it returns to ok.
    """
    memory = _STATE if state is None else state
    sent: list[dict[str, str]] = []
    recovered: list[str] = []
    seen: set[str] = set()

    for reading in readings:
        seen.add(reading.key)
        previous = memory.get(reading.key)
        if reading.severity == OK:
            if previous in (WARN, CRITICAL):
                recovered.append(reading.key)
            memory.pop(reading.key, None)
            continue
        if previous == reading.severity:
            continue
        outcome = send(reading, moment, channel=channel)
        sent.append(
            {
                "key": reading.key,
                "signal": reading.signal,
                "severity": reading.severity,
                "delivery": outcome,
                "from": previous or "ok",
            }
        )
        # Remembered even when delivery was only logged: re-logging every minute is the
        # flood the transition rule exists to prevent, and the report says "logged" so
        # the operator can see the channel is the thing missing.
        memory[reading.key] = reading.severity

    # A scope that produced **no** reading this sweep is cleared, not frozen. The provider
    # rate has no series once the window empties — a fixed outage stops producing rows —
    # and leaving the key in memory would keep a recovered provider "alerting" until the
    # process restarted, so the next real outage would be suppressed as a repeat.
    for key in [key for key in memory if key not in seen]:
        recovered.append(key)
        memory.pop(key, None)

    return {"sent": sent, "recovered": recovered}


def run_once(
    *,
    now: datetime | None = None,
    client: Any = None,
    channel: Any = None,
    state: dict[str, str] | None = None,
    queue_state: dict[str, datetime] | None = None,
    disk_path: str | None = None,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """One sweep: evaluate every signal, announce transitions, return the report.

    The report is the job's return value, arq's stored result and the CLI's output, so
    "the poller ran and saw nothing" is checkable rather than inferred from an absence of
    email.
    """
    moment = now or datetime.now(UTC)
    readings = evaluate(
        now=moment,
        client=client if client is not None else redis_client(),
        queue_state=queue_state,
        disk_path=disk_path,
        backup_dir=backup_dir,
    )
    outcome = dispatch(readings, moment, state=state, channel=channel)

    worst = max((reading.severity for reading in readings), key=SEVERITY_RANK.get)
    report = {
        "at": moment.isoformat(timespec="seconds"),
        "signals": sorted({reading.signal for reading in readings}),
        "readings": [
            {
                "key": reading.key,
                "severity": reading.severity,
                "value": reading.value,
                "threshold": reading.threshold,
            }
            for reading in readings
        ],
        "worst": worst,
        "alert_email": bool(config.ALERT_EMAIL),
        **outcome,
    }
    logger.info(
        "alert sweep: %d readings, worst=%s, sent=%d, recovered=%d",
        len(readings),
        worst,
        len(outcome["sent"]),
        len(outcome["recovered"]),
    )
    return report


if __name__ == "__main__":
    # One sweep from inside a container, for the runbook and for evidence:
    #   sudo docker compose exec -T worker python -m app.observability.alerts
    print(json.dumps(run_once(), indent=2, default=str))
