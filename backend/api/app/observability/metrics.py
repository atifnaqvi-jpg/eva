"""Prometheus metrics without Prometheus, Grafana or a metrics library (OPS-03, §22).

§22 lists seven metrics and one charting requirement. HALT-08 (PLAN §8.25 RULINGS)
ruled that none of it may be delivered by adding the 3rd-party stack, and the
operator's confirmation of 2026-11-18 made that a *binding design constraint* rather
than a preference: ``GET /metrics`` must emit valid Prometheus text exposition format
so that adopting Prometheus later is a scrape config and not an application change.

Four consequences shape this module.

**The event store is the metric store.** Every number below is one SQL aggregation
over rows the application already writes — ``messages``, ``usage_records``,
``provider_outcomes``, ``document_versions``. There is no exporter, no separate TSDB
and no in-memory aggregation of business data, so a metric cannot disagree with the
database it describes.

**Windowed aggregates are gauges, never counters.** A count computed over the last N
minutes *decreases* as samples age out. Prometheus reads ``_total`` as monotonic and
feeds it to ``rate()``/``increase()``, so naming such a number ``_total`` would produce
wrong graphs rather than merely unconventional ones. Only the two genuinely
process-lifetime series here (HTTP requests, SSE streams) are counters, and they
carry no ``_seconds``/window semantics. The window is stated in every affected
``# HELP`` line because a number whose window is invisible is a number a reader will
misinterpret.

**Cardinality is bounded and tenant is the only unbounded label.** Labels are drawn
from closed vocabularies (provider keys, outcome, role, status, route *templates*),
plus ``tenant_id`` on the per-tenant families. §20.2's ceiling is ≤50 tenants. Request
rate is labelled on ``scope["route"].path_format`` — never the raw path — because a
conversation id in a label grows cardinality with traffic. Per-tenant aggregates are
computed on request; nothing is stored as one series per tenant per metric, which is
what keeps the tenant label out of the hot path.

**The tenant dimension is why /metrics loops tenants.** ``eva_app`` is subject to RLS
and a connection with no ``app.tenant_id`` sees zero rows on every tenant table
(measured, not assumed). A single cross-tenant aggregate would need a privileged query
or a ``SECURITY DEFINER`` function; iterating tenants and opening a scoped session each
is slower but reuses the isolation the rest of the application is already bound by.
That is the deliberate trade, and ``docs/PLAN.md`` §8.38 records its cost.

**``/metrics`` is not routed through Caddy, on purpose.** It is scraped
container-to-container by a future Prometheus on the compose network. §22's "access
control on observability" cannot be satisfied by an unauthenticated public scrape
endpoint, and an internal target needs no application-level auth at all. The route is
therefore declared outside the ``/api/v1`` prefix and ``caddy/Caddyfile`` has no block
for it — both asserted by tests, because an accidental Caddy block would publish it.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.auth.dependencies import RequireAdmin, ScopedSession
from app.db import session as db_session

logger = logging.getLogger(__name__)

# How far back the windowed aggregates look. Five minutes matches the window HALT-09
# set for the provider error-rate alert, so an operator comparing this endpoint with
# the alerting poller is not comparing two different definitions of "recently".
WINDOW_SECONDS = int(os.environ.get("METRICS_WINDOW_SECONDS", "300"))

# Content type Prometheus expects. `version=0.0.4` is what makes a scraper parse it
# without guessing, and it is asserted by `tests/test_metrics.py` rather than trusted.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# The arq queue is a Redis *sorted set* (`zadd` in arq/connections.py), not a list, so
# `ZCARD` is the correct cardinality call and `LLEN` would raise WRONGTYPE. Measured in
# the running container; PLAN §8.33 suggested `LLEN` before this row checked.
QUEUE_KEY = "arq:queue"

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_TYPES = frozenset({"counter", "gauge"})


# --------------------------------------------------------------------------- #
# Exposition format
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


@dataclass(frozen=True)
class Family:
    """One metric family: a name, a type, a help string and its samples."""

    name: str
    type: str
    help: str
    samples: tuple[Sample, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid Prometheus metric name: {self.name!r}")
        if self.type not in _TYPES:
            raise ValueError(f"unsupported metric type: {self.type!r}")
        if self.type == "counter" and not self.name.endswith("_total"):
            # The convention exists so `rate()` works on a graph without a second
            # lookup; enforcing it here makes a mis-named counter fail in tests
            # rather than in a dashboard someone built on the wrong assumption.
            raise ValueError(f"counter must end in _total: {self.name!r}")
        if self.type == "gauge" and self.name.endswith("_total"):
            # The other half of the same rule, and the trap this row exists to avoid:
            # a windowed count declines as samples age out, so naming it `_total`
            # tells a reader it is monotonic. Reserved for counters by convention.
            raise ValueError(
                f"gauge must not end in _total (reserved for counters): {self.name!r}"
            )
        for sample in self.samples:
            for key in sample.labels:
                if not _LABEL_NAME.fullmatch(key):
                    raise ValueError(f"invalid label name: {key!r}")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    """Escape a label *value*: backslash, double quote and newline (v0.0.4 §).

    A tenant id or a provider key can never contain these today, which is exactly why
    the escaping is tested rather than argued away — the day a label carries a model
    string from a config file, an unescaped quote would corrupt the scrape.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float) -> str:
    if isinstance(value, bool):  # bool is an int; never emit True/False
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


def render(families: Iterable[Family]) -> str:
    """Render families as Prometheus text exposition format v0.0.4.

    Families are emitted in name order and samples in label order so two scrapes of
    unchanged data produce identical bytes — a diffable scrape is a scrape an operator
    can eyeball when a graph looks wrong.

    Families that share a name are **merged into one block**. This is not cosmetic:
    a metric name may carry exactly one ``# HELP`` and one ``# TYPE`` in the whole
    exposition, and ``tenant_families`` returns one family per tenant by construction.
    Rendering per tenant as-is emitted ``eva_llm_latency_p50_seconds`` **77 times** on
    the live database — measured — which a real scraper rejects as a duplicate metric
    family. Merging also drops empty families, so a scrape is not padded with 545
    HELP/TYPE blocks for 55 samples.
    """
    merged: dict[str, Family] = {}
    for family in families:
        existing = merged.get(family.name)
        if existing is None:
            merged[family.name] = Family(
                family.name, family.type, family.help, tuple(family.samples)
            )
            continue
        merged[family.name] = Family(
            family.name,
            family.type,
            existing.help,
            (*existing.samples, *family.samples),
        )

    lines: list[str] = []
    for name in sorted(merged):
        family = merged[name]
        # A family with no samples gets no header: a HELP line for a series that does
        # not exist is noise, and some collectors reject a TYPE with no samples.
        if not family.samples:
            continue
        lines.append(f"# HELP {name} {_escape_help(family.help)}")
        lines.append(f"# TYPE {name} {family.type}")
        ordered = sorted(
            family.samples, key=lambda s: sorted(s.labels.items())
        )
        for sample in ordered:
            if sample.labels:
                rendered = ",".join(
                    f'{key}="{_escape_label(value)}"'
                    for key, value in sorted(sample.labels.items())
                )
                lines.append(f"{name}{{{rendered}}} {_format_value(sample.value)}")
            else:
                lines.append(f"{name} {_format_value(sample.value)}")
    # A trailing newline is part of the format: a scraper reading the last line
    # without one still parses, but the spec forbids it and a strict parser says so.
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Process-lifetime counters (the only real counters here)
# --------------------------------------------------------------------------- #


@dataclass
class Counters:
    """Counters that live in this process, guarded by a lock.

    Labelled by closed vocabularies only. The request counter keys on the *route
    template*, so `/api/v1/conversations/<uuid>` contributes to one series however
    much traffic arrives.

    **Bounded gap, stated rather than hidden**: §20.1 specifies two uvicorn workers
    while the container currently runs one. These live per process, so under two
    workers each scrape would report roughly half the traffic. That is why no
    business number is computed here — the store-derived metrics below are correct
    under any worker count. Moving these to Redis is the fix if the worker count
    changes; recorded in PLAN §8.38.
    """

    requests: dict[tuple[str, str, str], int] = field(default_factory=dict, repr=False)
    streams_started: int = 0
    streams_completed: int = 0
    streams_aborted: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(self, route: str, method: str, status: int) -> None:
        with self._lock:
            key = (route, method, str(status))
            self.requests[key] = self.requests.get(key, 0) + 1

    def stream_started(self) -> None:
        with self._lock:
            self.streams_started += 1

    def stream_finished(self, *, completed: bool) -> None:
        """Count a finished stream as completed or aborted.

        ``aborted`` is the number that matters: a stream that opened, emitted a 200
        and then lost its consumer is a visitor who saw a hung panel while every
        status-code metric stayed green. OPS-08's load test found the same failure
        shape from the client side (``sse_streams_without_final``).
        """
        with self._lock:
            if completed:
                self.streams_completed += 1
            else:
                self.streams_aborted += 1

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.streams_started = 0
            self.streams_completed = 0
            self.streams_aborted = 0


_counters = Counters()


def counters() -> Counters:
    return _counters


@contextlib.contextmanager
def sse_stream() -> Iterator[dict[str, bool]]:
    """Track one SSE stream's life. Yields a mutable ``completed`` flag.

    A context manager over a flag rather than a wrapper around the generator, because
    the two call sites are async generators that yield while open: wrapping the
    iteration would close the stream from a different context, the trap OPS-02
    documented for ``start_as_current_span``. Setting a flag inside the existing
    ``finally`` costs nothing and cannot reorder the yields.
    """
    state = {"completed": False}
    counters().stream_started()
    try:
        yield state
    finally:
        # `finally` runs on normal exhaustion and on `GeneratorExit`, which is how a
        # disconnect closes the generator at its suspension point. Completion is
        # defined by one thing only — whether a `final` event was emitted — because
        # that is the signal §22 needs: a stream that opened and never reached `final`
        # is a hung visitor, while a disconnect after `final` cost the visitor nothing.
        counters().stream_finished(completed=state["completed"])


# --------------------------------------------------------------------------- #
# Store-derived families
# --------------------------------------------------------------------------- #

# A provider key that outlived its configuration, or a model the map has never seen,
# is labelled rather than dropped: an absent series is indistinguishable from "no
# traffic", and conflating the two hides the problem this metric exists to show.
UNKNOWN_PROVIDER = "(unknown)"


def _latency_by_provider(session: Any, tenant_id: UUID, since: datetime) -> list[Family]:
    """p50/p95 LLM latency per provider (§22 "LLM latency ... by provider").

    ``messages`` carries ``latency_ms`` and ``model`` but **no provider column** —
    §16.1 fixes that shape and adding one is a schema change beyond the blueprint
    (G3.2). The provider is therefore resolved through the model string, using the
    ``(model -> provider)`` map that ``provider_outcomes`` (OPS-12) records per turn.
    That map is exact rather than approximate: provider routing is by model prefix
    (``providers.PROFILES``), so one model string is served by at most one provider.
    A model with no recorded outcome is labelled ``(unknown)``, not dropped.
    """
    rows = session.execute(
        text(
            """
            WITH provider_by_model AS (
                SELECT DISTINCT ON (model) model, provider
                  FROM provider_outcomes
                 WHERE tenant_id = :tenant_id AND provider IS NOT NULL
                 ORDER BY model, created_at DESC
            )
            SELECT COALESCE(p.provider, :unknown) AS provider,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY m.latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY m.latency_ms) AS p95,
                   count(*) AS samples
              FROM messages m
              LEFT JOIN provider_by_model p ON p.model = m.model
             WHERE m.tenant_id = :tenant_id
               AND m.role = 'assistant'
               AND m.latency_ms IS NOT NULL
               AND m.created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """
        ),
        {"tenant_id": str(tenant_id), "since": since, "unknown": UNKNOWN_PROVIDER},
    ).mappings().all()

    p50, p95, counts = [], [], []
    for row in rows:
        labels = {"tenant_id": str(tenant_id), "provider": row["provider"]}
        if row["p50"] is not None:
            p50.append(Sample(dict(labels), float(row["p50"]) / 1000.0))
            p95.append(Sample(dict(labels), float(row["p95"]) / 1000.0))
        counts.append(Sample(dict(labels), int(row["samples"])))

    window = f"over the last {WINDOW_SECONDS}s"
    return [
        Family(
            "eva_llm_latency_p50_seconds",
            "gauge",
            f"Median assistant-turn latency {window}, by provider (messages.latency_ms / 1000)",
            tuple(p50),
        ),
        Family(
            "eva_llm_latency_p95_seconds",
            "gauge",
            f"95th percentile assistant-turn latency {window}, by provider",
            tuple(p95),
        ),
        Family(
            # Present so a zero percentile cannot be confused with no samples: a
            # percentage computed from three calls and from three thousand is the
            # same number here and a different decision for the reader.
            "eva_llm_latency_samples",
            "gauge",
            f"Assistant turns with a recorded latency {window}, by provider",
            tuple(counts),
        ),
    ]


def _llm_outcomes(session: Any, tenant_id: UUID, since: datetime) -> list[Family]:
    """Turn counts and error ratio per provider (§22 "LLM ... error by provider").

    ``provider_outcomes`` is the OPS-12 producer: one row per turn that attempted a
    completion, written on the degraded path as well as the answered one. Both halves
    of the ratio come from it, which is why the error rate does not need to union
    ``usage_records`` — that table records answered turns only, so reading it alone
    would make the failure count structurally zero.
    """
    rows = session.execute(
        text(
            """
            SELECT COALESCE(provider, :unknown) AS provider,
                   count(*) FILTER (WHERE outcome = 'success') AS ok,
                   count(*) FILTER (WHERE outcome = 'failure') AS failed
              FROM provider_outcomes
             WHERE tenant_id = :tenant_id AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """
        ),
        {"tenant_id": str(tenant_id), "since": since, "unknown": UNKNOWN_PROVIDER},
    ).mappings().all()

    turns, ratios = [], []
    for row in rows:
        labels = {"tenant_id": str(tenant_id), "provider": row["provider"]}
        for outcome, count in (("success", row["ok"]), ("failure", row["failed"])):
            turns.append(Sample({**labels, "outcome": outcome}, int(count)))
        total = int(row["ok"]) + int(row["failed"])
        if total:
            ratios.append(Sample(dict(labels), int(row["failed"]) / total))

    window = f"over the last {WINDOW_SECONDS}s"
    return [
        Family(
            "eva_llm_turns",
            "gauge",
            f"Provider completion attempts {window}, by provider and outcome",
            tuple(turns),
        ),
        Family(
            "eva_llm_error_ratio",
            "gauge",
            f"Fraction of provider attempts that failed {window} (0-1), by provider",
            tuple(ratios),
        ),
    ]


def _tokens_by_tenant(session: Any, tenant_id: UUID, since: datetime) -> list[Family]:
    """Token units per tenant and kind (§22 "token usage by tenant", R31).

    ``usage_records.units`` is the ledger OPS-12 made non-zero. No tenant id goes into
    a label here beyond the per-tenant loop's own — §20.2 caps tenants at 50 and the
    ruling permits that label specifically.
    """
    rows = session.execute(
        text(
            """
            SELECT kind, COALESCE(sum(units), 0) AS units
              FROM usage_records
             WHERE tenant_id = :tenant_id AND created_at >= :since
             GROUP BY 1
             ORDER BY 1
            """
        ),
        {"tenant_id": str(tenant_id), "since": since},
    ).mappings().all()

    return [
        Family(
            "eva_tokens_used",
            "gauge",
            f"Token units recorded in the ledger over the last {WINDOW_SECONDS}s,"
            " by tenant and kind (R31 metering, not a billing guarantee)",
            tuple(
                Sample(
                    {"tenant_id": str(tenant_id), "kind": row["kind"]},
                    int(row["units"]),
                )
                for row in rows
            ),
        )
    ]


def _ingestion(session: Any, tenant_id: UUID) -> list[Family]:
    """Document versions by status (§22 "ingestion failures").

    A *state*, not a window: a document that failed yesterday is still failed, and a
    five-minute window would report zero ingestion failures on a system that has a
    permanently stuck document. ``failed`` is the number §20.4's "failed documents
    surface errors" depends on being visible.
    """
    rows = session.execute(
        text(
            """
            SELECT status, count(*) AS versions
              FROM document_versions
             WHERE tenant_id = :tenant_id
             GROUP BY 1
             ORDER BY 1
            """
        ),
        {"tenant_id": str(tenant_id)},
    ).mappings().all()

    return [
        Family(
            "eva_ingestion_documents",
            "gauge",
            "Document versions by ingestion status, current state (not windowed)",
            tuple(
                Sample(
                    {"tenant_id": str(tenant_id), "status": row["status"]},
                    int(row["versions"]),
                )
                for row in rows
            ),
        )
    ]


def tenant_families(
    session: Any, tenant_id: UUID, *, window_seconds: int | None = None
) -> list[Family]:
    """Every store-derived family for one tenant.

    Shared by both endpoints on purpose: ``GET /metrics`` calls it per tenant and
    ``GET /api/v1/admin/metrics/summary`` calls it for the caller's own tenant. One
    implementation means the JSON the dashboard charts and the text a Prometheus would
    scrape cannot drift into two different definitions — which is the ruling's "no
    local-only shortcuts" made structural rather than promised.
    """
    since = datetime.now(UTC) - timedelta(seconds=window_seconds or WINDOW_SECONDS)
    return [
        *_latency_by_provider(session, tenant_id, since),
        *_llm_outcomes(session, tenant_id, since),
        *_tokens_by_tenant(session, tenant_id, since),
        *_ingestion(session, tenant_id),
    ]


# --------------------------------------------------------------------------- #
# Platform families: queue depth, DB pool saturation, counters
# --------------------------------------------------------------------------- #


def _active_tenant_ids() -> list[str]:
    """Every active tenant id, in a stable order.

    ``tenants`` carries no RLS policy, so this is a deliberately cross-tenant read and
    the only one in this module — the same shape ``purge_job._tenants`` uses.
    """
    with db_session.SessionLocal() as session:
        session.begin()
        rows = session.execute(
            text("SELECT id::text FROM tenants WHERE status = 'active' ORDER BY id")
        ).scalars().all()
        session.commit()
    return list(rows)


def _queue_depth_family() -> Family:
    """arq queue depth from Redis (§22 "queue depth").

    The worker owns the queue and serves no HTTP, so the API reads it — Reading A of
    the original halt suggested exactly this and it is unchanged by the local-first
    ruling. Unreachable Redis omits the family rather than reporting 0: "queue empty"
    and "cannot see the queue" must not render as the same graph, and a metrics
    endpoint that 500s because a side-car dependency is down would take the scrape
    down with it.
    """
    try:
        import redis

        client = redis.Redis.from_url(
            __import__("os").environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_connect_timeout=0.25,
            socket_timeout=0.25,
        )
        depth = int(client.zcard(QUEUE_KEY))
    except Exception as exc:  # noqa: BLE001 - a scrape must not fail on a side-car
        logger.warning("queue depth unavailable (%s)", type(exc).__name__)
        return Family(
            "eva_queue_depth", "gauge", "arq pending jobs (Redis zset cardinality)", ()
        )
    return Family(
        "eva_queue_depth",
        "gauge",
        "arq pending jobs (ZCARD of the arq queue zset, not LLEN: it is a sorted set)",
        (Sample({"queue": QUEUE_KEY}, depth),),
    )


def _pool_family() -> Family:
    """DB pool saturation (§22).

    Read from the engine's own pool rather than counted in application code, so it
    reports what the driver is doing. Per process, like every pool statistic.
    """
    pool = getattr(db_session.engine, "pool", None)
    samples: list[Sample] = []
    if pool is not None and hasattr(pool, "checkedout"):
        # `checked_out` against `size` is the saturation signal: a pool reporting
        # checked_out == size with a growing wait is the condition §20.3 cares about.
        # SQLAlchemy's `overflow()` is deliberately NOT emitted — it returns the
        # *remaining* overflow budget, which reads as a negative number on a pool that
        # has borrowed nothing (measured -5 here). A gauge that needs a paragraph of
        # explanation to not be alarming is worse than its absence.
        samples = [
            Sample({"state": "checked_out"}, int(pool.checkedout())),
            Sample({"state": "idle"}, int(pool.checkedin())),
            Sample({"state": "size"}, int(pool.size())),
        ]
    return Family(
        "eva_db_pool_connections",
        "gauge",
        "SQLAlchemy pool connections by state, per API process",
        tuple(samples),
    )


def snapshot() -> tuple[dict[tuple[str, str, str], int], tuple[tuple[str, int], ...]]:
    """A consistent read of the counters, taken under the counter's own lock."""
    with counters()._lock:  # noqa: SLF001 - the lock is this object's own invariant
        return (
            dict(counters().requests),
            (
                ("started", counters().streams_started),
                ("completed", counters().streams_completed),
                ("aborted", counters().streams_aborted),
            ),
        )


def counter_families() -> list[Family]:
    requests, streams = snapshot()
    families = [
        Family(
            "eva_http_requests_total",
            "counter",
            "HTTP responses served in this API process, by route template, method"
            " and status",
            tuple(
                Sample({"route": route, "method": method, "status": status}, count)
                for (route, method, status), count in requests.items()
            ),
        )
    ]
    for state, value in streams:
        families.append(
            Family(
                f"eva_sse_streams_{state}_total",
                "counter",
                f"SSE streams {state} in this API process",
                (Sample({}, value),),
            )
        )
    return families


def collect() -> list[Family]:
    """The whole scrape: platform families plus every tenant's store-derived ones."""
    families = [*counter_families(), _queue_depth_family(), _pool_family()]
    for tenant_id in _active_tenant_ids():
        with db_session.tenant_session(tenant_id) as session:
            families.extend(tenant_families(session, UUID(tenant_id)))
    return families


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

# Scraped container-to-container. Deliberately NOT under `/api/v1` and deliberately
# absent from `caddy/Caddyfile`: an unauthenticated public scrape endpoint would
# contradict §22's access-control requirement, while an internal target needs no
# application-level auth at all.
prometheus_router = APIRouter()


@prometheus_router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        render(collect()),
        media_type=CONTENT_TYPE.split(";")[0],
        headers={"Content-Type": CONTENT_TYPE},
    )


# The dashboard's read path. Same numbers, tenant-scoped: `ScopedSession` means a
# caller can only ever see their own tenant's series, which is why this family list
# is not simply the one `/metrics` renders.
admin_router = APIRouter(prefix="/admin/metrics", tags=["metrics"])


@admin_router.get("/summary")
def metrics_summary(user: RequireAdmin, session: ScopedSession) -> dict[str, Any]:
    """The scrape as JSON for the dashboard's analytics screen (ADMN-08/09).

    Families and samples are the metric contract itself rather than a bespoke shape,
    so a field renamed here cannot silently mean something different from the same
    name in the text endpoint. `tests/test_metrics.py` asserts the two agree value for
    value for the caller's own tenant.
    """
    families = tenant_families(session, user.tenant_id)
    return {
        "window_seconds": WINDOW_SECONDS,
        "window": {
            "to": datetime.now(UTC).isoformat(),
            "timezone": "UTC",
            "basis": (
                "rolling window on messages.created_at, provider_outcomes.created_at"
                " and usage_records.created_at; eva_ingestion_documents is current"
                " state and not windowed"
            ),
        },
        "families": [
            {
                "name": family.name,
                "type": family.type,
                "help": family.help,
                "samples": [
                    {"labels": dict(sorted(sample.labels.items())), "value": sample.value}
                    for sample in family.samples
                ],
            }
            for family in sorted(families, key=lambda f: f.name)
        ],
    }


def record_request(route: str, method: str, status: int) -> None:
    counters().record_request(route, method, status)


def route_template(scope: Any) -> str:
    """The matched route's template, including the prefix `include_router` added.

    Starlette's ``path_format`` keeps the ``APIRouter`` prefix but **drops the
    ``include_router`` one** (measured on fastapi 0.163.1: a request to
    ``/api/v1/conversations/<id>`` reports ``/conversations/{conversation_id}``). Using
    it raw would label two routers mounted at different prefixes with the same internal
    path — ``/api/v1/widget/x`` and ``/api/v1/x`` — as one series, silently merging
    unrelated traffic and producing a graph that looks plausible and is wrong.

    The prefix is therefore recovered from the request path by aligning the template's
    trailing segments, which is exact because ``include_router`` only ever prepends.
    A template whose segment count cannot be aligned (a ``{path:path}`` converter
    swallowing slashes) falls back to the template alone rather than guessing.
    """
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not template:
        return "(unmatched)"

    actual = scope.get("path", "")
    template_segments = template.strip("/").split("/")
    actual_segments = actual.strip("/").split("/") if actual.strip("/") else []
    extra = len(actual_segments) - len(template_segments)
    if extra < 0:
        return template
    prefix = actual_segments[:extra]
    return "/" + "/".join([*prefix, *template_segments])


class MetricsMiddleware:
    """Counts every HTTP response on its route *template*.

    Pure ASGI for the same reason OPS-01's middleware is: ``BaseHTTPMiddleware``
    interposes a task and a queue around the response, which changes streaming
    behaviour, and the SSE turn is the response this application cares most about.

    The count happens after the inner app returns, when Starlette has written
    ``scope["route"]``. An unmatched path has no route, so it is bucketed as
    ``(unmatched)`` rather than labelled with the raw path — the raw path is client
    input and would grow cardinality without bound.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = 500

        async def capture(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            record_request(route_template(scope), scope.get("method", "-"), status)
