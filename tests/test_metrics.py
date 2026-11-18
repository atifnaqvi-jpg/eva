"""OPS-03: Prometheus metrics in exposition format, delivered without Prometheus.

HALT-08 (PLAN §8.25 RULINGS, confirmed 2026-11-18) forbids ``prometheus_client``,
a Prometheus container and a Grafana container. It requires instead that
``GET /metrics`` emit **valid Prometheus text exposition format v0.0.4** with
conventional metric names, so that adopting the real stack later is a scrape config
and not an application change. That requirement is what most of this file defends.

Why each group of tests is here rather than in a smoke test:

* **An independent parser, written from the spec.** ``parse_exposition`` below does
  not call the renderer and does not import a Prometheus library (none is installed,
  by ruling). A test that round-tripped the renderer through its own helpers would
  prove the renderer is self-consistent, not that the output is *readable*. This is
  the SEC-13 lesson — a resolver that trusted its own input would clear any tree.
* **Counters are named and typed correctly, gauges are not called ``_total``.** A
  windowed count that decreases is not a counter: Prometheus feeds ``_total`` to
  ``rate()``, so a mis-named series produces a graph that is wrong rather than
  merely unconventional. The test asserts the type of every emitted family.
* **Label escaping is checked against the format specification, not against
  plausibility.** Backslash, double quote and newline, in label values.
* **No content in labels.** A canary string pushed through a real turn and a real
  provider outcome must be absent from the exposition text, because a scrape leaves
  the box — a stronger rule than OPS-01's logging guard needed.
* **Route templates, never raw paths.** ``/api/v1/conversations/<uuid>`` must fold
  into one series. A conversation id in a label is unbounded cardinality and also a
  visitor correlation, which is why it is refused twice over.
* **``/metrics`` is not published through Caddy.** Read from the real Caddyfile: an
  unauthenticated public scrape endpoint would contradict §22's access control.
"""

import os
import re
import uuid
from datetime import UTC, datetime, timedelta

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import psycopg
import pytest
from fastapi.testclient import TestClient

from test_conversation import client  # noqa: F401  (module-scoped app/db fixture)

PASSWORD = "correct-horse-battery"
METRICS_FIXTURE = "metrics-"
CANARY = "CANARY-should-never-be-a-label"

METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|NaN|[+-]?Inf)")


# --------------------------------------------------------------------------- #
# An independent reader for the exposition format
# --------------------------------------------------------------------------- #


def parse_exposition(body: str) -> dict[str, dict]:
    """Parse Prometheus text exposition format v0.0.4 into families.

    Written from the specification and independent of ``app.observability.metrics``:
    it re-derives the escaping rules rather than importing them, so a bug in the
    renderer cannot be mirrored here. It is strict on purpose — an unescaped quote or
    a missing ``# TYPE`` is an error, not a tolerated variant, because a real scraper
    is strict too.

    Raises ``AssertionError`` with the offending line for anything a scraper would
    reject.
    """
    families: dict[str, dict] = {}

    def unescape(value: str) -> str:
        out, index = [], 0
        while index < len(value):
            char = value[index]
            if char == "\\":
                index += 1
                assert index < len(value), f"dangling backslash in {value!r}"
                escaped = value[index]
                assert escaped in '\\"n', f"invalid escape \\{escaped} in {value!r}"
                out.append({"\\": "\\", '"': '"', "n": "\n"}[escaped])
            elif char == '"':
                raise AssertionError(f"unescaped quote in label value {value!r}")
            else:
                out.append(char)
            index += 1
        return "".join(out)

    for raw in body.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("# HELP "):
            rest = raw[len("# HELP ") :]
            name, _, help_text = rest.partition(" ")
            assert METRIC_NAME.fullmatch(name), f"bad HELP name: {name!r}"
            family = families.setdefault(name, {"help": None, "type": None, "samples": []})
            # The spec allows ONE help line per name in the whole exposition. This
            # assertion is why the renderer merges same-named families: the first
            # version of this parser silently overwrote the duplicate and reported a
            # clean parse of output that repeated `# HELP` 77 times on live data.
            assert family["help"] is None, f"duplicate # HELP for {name}"
            family["help"] = help_text
            continue
        if raw.startswith("# TYPE "):
            rest = raw[len("# TYPE ") :]
            name, _, type_name = rest.partition(" ")
            assert METRIC_NAME.fullmatch(name), f"bad TYPE name: {name!r}"
            assert type_name in {"counter", "gauge", "histogram", "summary"}, (
                f"bad type {type_name!r}"
            )
            family = families.setdefault(name, {"help": None, "type": None, "samples": []})
            assert family["type"] is None, f"duplicate # TYPE for {name}"
            family["type"] = type_name
            continue
        assert not raw.startswith("#"), f"unexpected comment line: {raw!r}"

        name, rest = None, raw
        if "{" in raw:
            name, _, rest = raw.partition("{")
            # Label *values* legitimately contain braces — a route template is
            # `/api/v1/conversations/{conversation_id}` — so the label block ends at
            # the first `}` that is NOT inside a quoted value. Splitting on the first
            # `}` regardless was a parser bug that read a valid line as malformed;
            # it surfaced only in the full suite, once another test had recorded a
            # templated route.
            in_quote, escaped, end = False, False, None
            for index, char in enumerate(rest):
                if escaped:
                    escaped = False
                elif char == "\\" and in_quote:
                    escaped = True
                elif char == '"':
                    in_quote = not in_quote
                elif char == "}" and not in_quote:
                    end = index
                    break
            assert end is not None, f"unterminated label block: {raw!r}"
            labels_part, rest = rest[:end], rest[end + 1 :]
            assert rest.strip() == "" or rest.strip()[0] not in "{}", (
                f"stray brace: {raw!r}"
            )
            labels = {}
            for item in re.finditer(
                r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', labels_part
            ):
                key = item.group(1)
                assert LABEL_NAME.fullmatch(key), f"bad label name: {key!r}"
                labels[key] = unescape(item.group(2))
            assert labels_part.strip(), f"empty label set: {raw!r}"
            # Every non-space run in the label block must be a whole `name="value"`.
            joined = ",".join(
                f'{m.group(1)}="{m.group(2)}"'
                for m in re.finditer(
                    r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', labels_part
                )
            )
            assert joined == labels_part, f"malformed label block: {labels_part!r}"
        else:
            name, _, rest = raw.partition(" ")

        assert METRIC_NAME.fullmatch(name), f"bad sample name: {name!r}"
        value_text = rest.strip()
        assert NUMBER.fullmatch(value_text), f"bad sample value: {value_text!r}"

        sample = {"labels": labels if "{" in raw else {}, "value": float(value_text)}
        families.setdefault(name, {"help": None, "type": None, "samples": []})[
            "samples"
        ].append(sample)

    # A family with samples but no TYPE is an exposition-format error, and the
    # check is here rather than in the renderer because it is what a scraper does.
    for name, family in families.items():
        assert family["samples"], f"{name} declares a type but has no samples"
        assert family["type"] is not None, f"{name} has samples but no # TYPE"
        assert family["help"] is not None, f"{name} has samples but no # HELP"
        # Duplicate (name, labels) pairs are the other way an exposition is invalid:
        # a scraper rejects two samples for one series in one scrape.
        seen = set()
        for sample in family["samples"]:
            key = tuple(sorted(sample["labels"].items()))
            assert key not in seen, f"duplicate series {name}{dict(key)}"
            seen.add(key)
    return families


def series(families: dict[str, dict], name: str, **labels) -> float | None:
    """The value of one series, or ``None`` when it was not emitted."""
    for sample in families.get(name, {}).get("samples", []):
        if all(sample["labels"].get(key) == value for key, value in labels.items()):
            if len(sample["labels"]) != len(labels):
                continue
            return sample["value"]
    return None


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _admin(migrated_db: str) -> str:
    from conftest import _admin_dsn

    return _admin_dsn(migrated_db)


def _register(client: TestClient) -> dict:
    email = f"{METRICS_FIXTURE}{uuid.uuid4().hex[:10]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": "Metrics"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            return header.split("eva_session=", 1)[1].split(";", 1)[0]
    raise AssertionError("no session cookie")


class Seeded:
    def __init__(self, tenant: dict, token: str, ids: dict) -> None:
        self.tenant = tenant
        self.token = token
        self.ids = ids

    @property
    def tenant_id(self) -> str:
        return self.tenant["tenant_id"]


@pytest.fixture
def seeded(client: TestClient, migrated_db: str) -> Seeded:
    """A tenant with answered turns, a failure and a failed document version.

    Seeded through SQL for the same reason WP-19's fixture is: no API creates these
    rows yet. The *reads* under test still go through the real HTTP stack and real RLS.
    """
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()

    tenant = _register(client)
    token = _login(client, tenant["email"])
    tenant_id = tenant["tenant_id"]
    now = datetime.now(UTC)

    with psycopg.connect(_admin(migrated_db)) as conn:
        bot_id = conn.execute(
            "SELECT id FROM bots WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]

        conversation = uuid.uuid4()
        conn.execute(
            "INSERT INTO conversations"
            " (id, tenant_id, bot_id, visitor_session_id, status, page_url,"
            "  started_at, last_activity_at)"
            " VALUES (%s,%s,%s,%s,'active','https://shop.example.test/',%s,%s)",
            (str(conversation), tenant_id, str(bot_id), f"vs-{conversation}", now, now),
        )

        # Two answered turns with real latencies, so a percentile has something to be
        # a percentile of, and one with no latency so the null filter is exercised.
        for latency, minute in ((400, 1), (600, 2), (None, 3)):
            conn.execute(
                "INSERT INTO messages"
                " (id, tenant_id, conversation_id, role, content, latency_ms, model,"
                "  created_at)"
                " VALUES (%s,%s,%s,'assistant',%s,%s,'metrics-model',%s)",
                (
                    str(uuid.uuid4()), tenant_id, str(conversation),
                    f"Answer {minute}", latency, now - timedelta(minutes=minute),
                ),
            )

        # OPS-12's producer: one success, one failure, so the ratio is 0.5 rather than
        # 0 (which would be indistinguishable from an unread table).
        for outcome in ("success", "failure"):
            conn.execute(
                "INSERT INTO provider_outcomes"
                " (id, tenant_id, conversation_id, model, provider, outcome,"
                "  error_class, created_at)"
                " VALUES (%s,%s,%s,'metrics-model','ollama_cloud',%s,%s,%s)",
                (
                    str(uuid.uuid4()), tenant_id, str(conversation), outcome,
                    None if outcome == "success" else "RuntimeError", now,
                ),
            )

        conn.execute(
            "INSERT INTO usage_records"
            " (id, tenant_id, conversation_id, kind, provider, model, units, created_at)"
            " VALUES (%s,%s,%s,'chat','ollama_cloud','metrics-model',421,%s)",
            (str(uuid.uuid4()), tenant_id, str(conversation), now),
        )

        source = uuid.uuid4()
        conn.execute(
            "INSERT INTO knowledge_sources (id, tenant_id, type, status, created_at)"
            " VALUES (%s,%s,'upload','active',%s)",
            (str(source), tenant_id, now),
        )
        document = uuid.uuid4()
        conn.execute(
            "INSERT INTO documents (id, tenant_id, source_id, title, created_at)"
            " VALUES (%s,%s,%s,'Broken doc',%s)",
            (str(document), tenant_id, str(source), now),
        )
        for version_no, status in ((1, "failed"), (2, "ready")):
            conn.execute(
                "INSERT INTO document_versions"
                " (id, tenant_id, document_id, version_no, content_hash, status,"
                "  created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    str(uuid.uuid4()), tenant_id, str(document), version_no,
                    f"hash-{version_no}", status, now,
                ),
            )
        conn.commit()

    return Seeded(tenant, token, {"conversation": str(conversation)})


# --------------------------------------------------------------------------- #
# The format itself
# --------------------------------------------------------------------------- #


def test_render_merges_same_named_families_into_one_block() -> None:
    """One metric name gets exactly one HELP/TYPE, however many families feed it.

    Found against the live database, not in a unit test: ``tenant_families`` returns
    one family per tenant, so rendering them straight through emitted
    ``eva_llm_latency_p50_seconds`` **77 times** — the number of active tenants — and
    a real Prometheus rejects a duplicate metric family. The first version of the
    parser above silently overwrote the duplicate, so this went unnoticed until the
    parser was made strict.
    """
    from app.observability import metrics

    body = metrics.render(
        [
            metrics.Family("eva_m", "gauge", "h", (metrics.Sample({"t": "a"}, 1),)),
            metrics.Family("eva_m", "gauge", "h", (metrics.Sample({"t": "b"}, 2),)),
            metrics.Family("eva_m", "gauge", "h", ()),
        ]
    )
    assert body.count("# HELP eva_m ") == 1
    assert body.count("# TYPE eva_m ") == 1

    parsed = parse_exposition(body)  # strict: raises on a duplicate HELP
    assert series(parsed, "eva_m", t="a") == 1.0
    assert series(parsed, "eva_m", t="b") == 2.0


def test_a_live_scrape_has_no_duplicate_family_or_series(client: TestClient) -> None:
    """The whole real scrape parses strictly — the check the live defect escaped.

    Parsing with the strict reader is the assertion: it raises on a repeated HELP/TYPE
    or a repeated (name, labels) pair. Run against the real database, which is where
    the 77-fold duplication appeared.
    """
    families = parse_exposition(client.get("/metrics").text)
    assert families, "the scrape was empty"


def test_render_output_is_valid_prometheus_text() -> None:
    """The renderer's output parses with a reader written from the spec."""
    from app.observability import metrics

    body = metrics.render(
        [
            metrics.Family(
                "eva_example_total", "counter", "a counter", (
                    metrics.Sample({"route": "/api/v1/bots", "method": "GET"}, 3),
                ),
            ),
            metrics.Family(
                "eva_example_gauge", "gauge", "a gauge\nspanning two lines",
                (metrics.Sample({"tenant_id": str(uuid.uuid4())}, 1.5),),
            ),
            metrics.Family(
                "eva_example_bare", "gauge", "no labels", (metrics.Sample({}, 0),)
            ),
        ]
    )

    parsed = parse_exposition(body)
    assert parsed["eva_example_total"]["type"] == "counter"
    assert parsed["eva_example_gauge"]["type"] == "gauge"
    assert series(parsed, "eva_example_total", route="/api/v1/bots", method="GET") == 3.0
    assert series(parsed, "eva_example_bare") == 0.0
    assert body.endswith("\n")


def test_label_values_are_escaped_per_specification() -> None:
    """Backslash, quote and newline are the three escapes the format defines."""
    from app.observability import metrics

    nasty = 'back\\slash "quoted" new\nline'
    body = metrics.render(
        [metrics.Family("eva_escaped", "gauge", "e", (metrics.Sample({"v": nasty}, 1),))]
    )

    # The rendered line must contain the escapes, not the raw characters.
    assert '\\\\' in body
    assert '\\"' in body
    assert "\\n" in body
    assert "new\nline" not in body

    parsed = parse_exposition(body)
    assert series(parsed, "eva_escaped", v=nasty) == 1.0


def test_help_text_is_escaped_too() -> None:
    from app.observability import metrics

    body = metrics.render(
        [metrics.Family("eva_help", "gauge", "line one\nline two", (metrics.Sample({}, 1),))]
    )
    lines = [line for line in body.splitlines() if line.startswith("# HELP")]
    assert len(lines) == 1
    assert "\n" not in lines[0].replace("# HELP ", "")
    assert lines[0].endswith("line one\\nline two")


def test_a_family_with_no_samples_emits_no_header() -> None:
    """An empty family is omitted, not rendered as a bare TYPE line.

    `tenant_families` returns every family for every tenant whether or not that tenant
    has rows, so a scrape of 77 quiet tenants would otherwise be 500-odd HELP/TYPE
    blocks describing series that do not exist.
    """
    from app.observability import metrics

    body = metrics.render([metrics.Family("eva_empty", "gauge", "nothing", ())])
    assert body == "\n", f"empty family rendered as {body!r}"


def test_counter_must_be_named_total_and_only_counters_may_be() -> None:
    """The naming rule is enforced at construction, not left to review."""
    from app.observability import metrics

    with pytest.raises(ValueError, match="must end in _total"):
        metrics.Family("eva_requests", "counter", "mis-named")

    # A gauge ending in _total is also wrong: it would tell a reader a windowed
    # number is monotonic, which is exactly the trap this row avoids.
    with pytest.raises(ValueError, match="must not end in _total"):
        metrics.Family("eva_windowed_total", "gauge", "decreases as samples age out")


def test_invalid_metric_and_label_names_are_rejected() -> None:
    from app.observability import metrics

    with pytest.raises(ValueError, match="invalid Prometheus metric name"):
        metrics.Family("eva-bad-name", "gauge", "hyphens are not allowed")
    with pytest.raises(ValueError, match="invalid label name"):
        metrics.Family(
            "eva_ok", "gauge", "bad label", (metrics.Sample({"conversation-id": "x"}, 1),)
        )
    with pytest.raises(ValueError, match="unsupported metric type"):
        metrics.Family("eva_ok", "histogram", "no histogram support needed yet")


def test_special_float_values_render_as_prometheus_tokens() -> None:
    from app.observability import metrics

    body = metrics.render(
        [
            metrics.Family("eva_nan", "gauge", "n", (metrics.Sample({}, float("nan")),)),
            metrics.Family("eva_inf", "gauge", "i", (metrics.Sample({}, float("inf")),)),
            metrics.Family(
                "eva_ninf", "gauge", "n", (metrics.Sample({}, float("-inf")),)
            ),
        ]
    )
    parsed = parse_exposition(body)
    assert series(parsed, "eva_nan") is not None
    assert series(parsed, "eva_inf") == float("inf")
    assert series(parsed, "eva_ninf") == float("-inf")


def test_render_is_deterministic() -> None:
    """Two renders of the same families produce identical bytes.

    Ordering by name and label makes a scrape diffable. Without it, a dict iteration
    order that changes between processes would make every scrape look changed to a
    textual comparison.
    """
    from app.observability import metrics

    families = [
        metrics.Family(
            "eva_b", "gauge", "b",
            (metrics.Sample({"z": "1", "a": "2"}, 1), metrics.Sample({"z": "2"}, 2)),
        ),
        metrics.Family("eva_a", "gauge", "a", (metrics.Sample({}, 0),)),
    ]
    assert metrics.render(families) == metrics.render(list(reversed(families)))


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #


def test_metrics_endpoint_serves_valid_exposition_with_content_type(
    client: TestClient,
) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    # The version parameter is what lets a scraper parse without guessing.
    assert response.headers["content-type"] == (
        "text/plain; version=0.0.4; charset=utf-8"
    )

    parsed = parse_exposition(response.text)
    # The platform families must always be present, even with no tenant data.
    for name in (
        "eva_http_requests_total",
        "eva_sse_streams_started_total",
        "eva_sse_streams_completed_total",
        "eva_sse_streams_aborted_total",
        "eva_db_pool_connections",
    ):
        assert name in parsed, f"{name} missing from the scrape"
        assert parsed[name]["type"] in {"counter", "gauge"}


def test_metrics_endpoint_counts_its_own_request_on_a_route_template(
    client: TestClient,
) -> None:
    """Request counting is on the template, and the scrape counts itself."""
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()
    client.get("/metrics")
    families = obs_metrics.counter_families()

    http = next(f for f in families if f.name == "eva_http_requests_total")
    routes = {sample.labels["route"] for sample in http.samples}
    assert "/metrics" in routes


def test_request_labels_use_the_route_template_not_the_raw_path(
    client: TestClient, seeded: Seeded
) -> None:
    """A conversation id must never become a label value.

    Two requests to two different conversations must fold into ONE series. Labelling
    the raw path would grow cardinality with traffic and would also put a visitor
    correlation into a scrape — refused for two independent reasons.
    """
    from app.observability import metrics as obs_metrics

    client.cookies.set("eva_session", seeded.token)
    obs_metrics.counters().reset()

    other = uuid.uuid4()
    client.get(f"/api/v1/conversations/{seeded.ids['conversation']}")
    client.get(f"/api/v1/conversations/{other}")

    requests, _ = obs_metrics.snapshot()
    routes = {route for route, _, _ in requests}
    assert "/api/v1/conversations/{conversation_id}" in routes
    # Neither raw id may appear anywhere in the counter keys: not as a route, not as
    # a method, not as a status.
    for key in requests:
        assert seeded.ids["conversation"] not in "".join(str(part) for part in key)
        assert str(other) not in "".join(str(part) for part in key)


def test_unmatched_paths_are_bucketed_not_labelled_with_client_input(
    client: TestClient,
) -> None:
    """A 404's path is client input; it must not become a label."""
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()
    client.get("/no-such-route/should-not-be-a-label")

    requests, _ = obs_metrics.snapshot()
    assert requests, "the middleware did not count the request"
    routes = {route for route, _, _ in requests}
    assert routes == {"(unmatched)"}
    statuses = {status for _, _, status in requests}
    assert statuses == {"404"}


# --------------------------------------------------------------------------- #
# Store-derived families: the numbers must be the database's numbers
# --------------------------------------------------------------------------- #


def test_tenant_families_reconcile_with_direct_sql(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """Assert against independent SQL, not against the fixture's own constants."""
    from app.observability import metrics as obs_metrics

    client.cookies.set("eva_session", seeded.token)
    response = client.get("/api/v1/admin/metrics/summary")
    assert response.status_code == 200, response.text

    with psycopg.connect(_admin(migrated_db)) as conn:
        p50, p95, samples = conn.execute(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms),
                   count(*)
              FROM messages
             WHERE tenant_id = %s AND role = 'assistant' AND latency_ms IS NOT NULL
            """,
            (seeded.tenant_id,),
        ).fetchone()
        units = conn.execute(
            "SELECT sum(units) FROM usage_records WHERE tenant_id = %s",
            (seeded.tenant_id,),
        ).fetchone()[0]
        failed_versions = conn.execute(
            "SELECT count(*) FROM document_versions"
            " WHERE tenant_id = %s AND status = 'failed'",
            (seeded.tenant_id,),
        ).fetchone()[0]

    assert samples == 2, "fixture should have two latencies and one null"

    families = {f["name"]: f for f in response.json()["families"]}
    by_labels = {
        name: [
            (sample["labels"], sample["value"]) for sample in family["samples"]
        ]
        for name, family in families.items()
    }

    assert by_labels["eva_llm_latency_samples"] == [
        ({"provider": "ollama_cloud", "tenant_id": seeded.tenant_id}, samples)
    ]
    # Seconds, not milliseconds: the base unit is seconds by convention and a
    # millisecond value under a _seconds name is a silent factor-of-1000 error.
    assert by_labels["eva_llm_latency_p50_seconds"] == [
        ({"provider": "ollama_cloud", "tenant_id": seeded.tenant_id}, p50 / 1000.0)
    ]
    assert by_labels["eva_tokens_used"] == [
        ({"kind": "chat", "tenant_id": seeded.tenant_id}, units)
    ]
    assert (
        {"tenant_id": seeded.tenant_id, "status": "failed"},
        failed_versions,
    ) in by_labels["eva_ingestion_documents"]


def test_provider_error_ratio_comes_from_both_outcomes(
    client: TestClient, seeded: Seeded
) -> None:
    """One success and one failure is 0.5, not 0 and not 1.

    A ratio computed from ``usage_records`` alone (answered turns only) would report
    0 with the same confidence as a healthy provider — the reading OPS-04 halted on.
    """
    client.cookies.set("eva_session", seeded.token)
    response = client.get("/api/v1/admin/metrics/summary")
    families = {f["name"]: f for f in response.json()["families"]}

    turns = {
        sample["labels"]["outcome"]: sample["value"]
        for sample in families["eva_llm_turns"]["samples"]
    }
    assert turns == {"success": 1, "failure": 1}

    ratio = families["eva_llm_error_ratio"]["samples"]
    assert len(ratio) == 1
    assert ratio[0]["value"] == pytest.approx(0.5)


def test_unknown_provider_is_labelled_not_dropped(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """A model with no recorded outcome reports ``(unknown)`` rather than vanishing.

    An absent series and "no traffic" are the same graph otherwise, which would hide
    exactly the misconfiguration this metric exists to surface.
    """
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "UPDATE messages SET model = 'unmapped-model'"
            " WHERE tenant_id = %s AND role = 'assistant'",
            (seeded.tenant_id,),
        )
        conn.commit()

    client.cookies.set("eva_session", seeded.token)
    response = client.get("/api/v1/admin/metrics/summary")
    families = {f["name"]: f for f in response.json()["families"]}

    providers = {
        sample["labels"]["provider"]
        for sample in families["eva_llm_latency_samples"]["samples"]
    }
    assert providers == {"(unknown)"}


def test_window_excludes_old_rows(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """The window is real: a row older than it must not be counted.

    Without this the "window" could be a comment rather than a predicate, and the
    metric would silently be a lifetime total wearing a rolling-window name.
    """
    old = datetime.now(UTC) - timedelta(days=2)
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "INSERT INTO messages"
            " (id, tenant_id, conversation_id, role, content, latency_ms, model,"
            "  created_at)"
            " VALUES (%s,%s,%s,'assistant','Ancient answer',90000,'metrics-model',%s)",
            (
                str(uuid.uuid4()), seeded.tenant_id,
                seeded.ids["conversation"], old,
            ),
        )
        conn.commit()

    client.cookies.set("eva_session", seeded.token)
    families = {
        f["name"]: f for f in client.get("/api/v1/admin/metrics/summary").json()["families"]
    }
    samples = families["eva_llm_latency_samples"]["samples"]
    assert samples[0]["value"] == 2, "the two-day-old turn must be outside the window"
    # And the p95 must not be dragged to the ancient row's latency.
    assert families["eva_llm_latency_p95_seconds"]["samples"][0]["value"] < 1.0


def test_ingestion_failures_are_not_windowed(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """A document failed last week is still failed.

    A windowed ingestion metric would report zero failures on a system with a
    permanently stuck document, which is the state §20.4's error surfacing exists to
    make visible.
    """
    old = datetime.now(UTC) - timedelta(days=7)
    with psycopg.connect(_admin(migrated_db)) as conn:
        document = conn.execute(
            "SELECT id FROM documents WHERE tenant_id = %s", (seeded.tenant_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO document_versions"
            " (id, tenant_id, document_id, version_no, content_hash, status, created_at)"
            " VALUES (%s,%s,%s,3,'hash-old','failed',%s)",
            (str(uuid.uuid4()), seeded.tenant_id, str(document), old),
        )
        conn.commit()

    client.cookies.set("eva_session", seeded.token)
    families = {
        f["name"]: f for f in client.get("/api/v1/admin/metrics/summary").json()["families"]
    }
    failed = [
        sample
        for sample in families["eva_ingestion_documents"]["samples"]
        if sample["labels"]["status"] == "failed"
    ]
    assert failed and failed[0]["value"] == 2


# --------------------------------------------------------------------------- #
# Isolation, privacy and exposure
# --------------------------------------------------------------------------- #


def test_admin_summary_is_tenant_scoped(
    client: TestClient, seeded: Seeded
) -> None:
    """A second tenant sees its own zeros, never another tenant's series."""
    other = _register(client)
    token = _login(client, other["email"])
    client.cookies.set("eva_session", token)

    response = client.get("/api/v1/admin/metrics/summary")
    assert response.status_code == 200, response.text
    body = response.json()

    tenant_ids = {
        sample["labels"]["tenant_id"]
        for family in body["families"]
        for sample in family["samples"]
        if "tenant_id" in sample["labels"]
    }
    assert tenant_ids <= {other["tenant_id"]}
    assert seeded.tenant_id not in tenant_ids


def test_admin_summary_requires_auth(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/api/v1/admin/metrics/summary").status_code == 401


def test_no_content_reaches_a_label(
    client: TestClient, seeded: Seeded, migrated_db: str
) -> None:
    """Visitor content, email and conversation id must be absent from the scrape.

    A scrape leaves the box, so this is a stricter rule than OPS-01's log guard. The
    seeded fixture carries a synthetic visitor name, email and question text; all
    three are pushed through the real render path and must not appear.
    """
    with psycopg.connect(_admin(migrated_db)) as conn:
        conn.execute(
            "UPDATE messages SET content = %s WHERE tenant_id = %s",
            (CANARY, seeded.tenant_id),
        )
        conn.execute(
            "UPDATE conversations SET visitor_name = %s, visitor_email = %s"
            " WHERE tenant_id = %s",
            (CANARY, f"{CANARY}@example.test", seeded.tenant_id),
        )
        conn.commit()

    body = client.get("/metrics").text
    parsed = parse_exposition(body)

    assert CANARY not in body
    for family in parsed.values():
        for sample in family["samples"]:
            for key, value in sample["labels"].items():
                assert CANARY not in value, f"content leaked into label {key}"
                assert "@example.test" not in value
    # And no label may be named for something that carries visitor identity.
    label_names = {
        key for family in parsed.values() for sample in family["samples"]
        for key in sample["labels"]
    }
    assert not label_names & {
        "content", "email", "visitor_email", "visitor_name", "visitor_session_id",
        "conversation_id", "page_url", "ip", "client_ip",
    }


def test_tenant_label_is_permitted_and_is_the_only_unbounded_one(
    client: TestClient, seeded: Seeded
) -> None:
    """§20.2 caps tenants at 50, which is what makes this label acceptable."""
    parsed = parse_exposition(client.get("/metrics").text)
    label_names = {
        key for family in parsed.values() for sample in family["samples"]
        for key in sample["labels"]
    }
    assert label_names <= {
        "tenant_id", "provider", "outcome", "kind", "status", "route", "method",
        "state", "queue",
    }


def test_metrics_is_not_routed_through_caddy() -> None:
    """`/metrics` must not be published on the public origin.

    §22 requires access control on observability. An unauthenticated scrape endpoint
    reachable from the internet would be the opposite, so the Caddyfile is read as
    evidence rather than trusted to stay correct.
    """
    with open("caddy/Caddyfile", encoding="utf-8") as handle:
        caddyfile = handle.read()

    assert "/metrics" not in caddyfile
    # The API routes it does publish are all under /api/*; /metrics is outside that
    # prefix, so it is not covered by the existing handle block either.
    assert "handle /api/*" in caddyfile


def test_metrics_is_mounted_outside_the_api_prefix(client: TestClient) -> None:
    """A scrape target under `/api/v1` would be swallowed by the Caddy API block.

    Asserted over HTTP rather than by walking ``app.routes``: this FastAPI version
    wraps included routers, so the route table does not name the sub-paths. Behaviour
    is the better evidence anyway — it also proves the route actually resolves.
    """
    assert client.get("/metrics").status_code == 200
    assert client.get("/api/v1/metrics").status_code == 404
    # Not in the public schema: an internal scrape target is not part of the API
    # contract a tenant integrates against.
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


# --------------------------------------------------------------------------- #
# SSE stream health
# --------------------------------------------------------------------------- #


def test_sse_stream_counters_distinguish_completed_from_aborted() -> None:
    """A stream that never emits `final` is an aborted stream, not a completed one.

    This is the counter that catches a hung visitor while every status-code metric
    stays green (OPS-08's `sse_streams_without_final`).
    """
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()

    with obs_metrics.sse_stream() as state:
        state["completed"] = True
    with obs_metrics.sse_stream():
        pass

    _, streams = obs_metrics.snapshot()
    assert dict(streams) == {"started": 2, "completed": 1, "aborted": 1}


def test_sse_stream_counts_a_failed_stream_as_aborted() -> None:
    """A stream that raises before `final` is an abort, not a completion."""
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()
    with pytest.raises(RuntimeError):
        with obs_metrics.sse_stream():
            raise RuntimeError("consumer vanished")

    _, streams = obs_metrics.snapshot()
    assert dict(streams) == {"started": 1, "completed": 0, "aborted": 1}


def test_sse_stream_counts_a_disconnect_after_final_as_completed() -> None:
    """Completion is `final`-emitted, not "the generator drained normally".

    A visitor who reads the whole answer and then closes the tab produces a
    `GeneratorExit`, and counting that as an abort would understate the completion
    rate — the one number that says the stream surface is healthy.
    """
    from app.observability import metrics as obs_metrics

    obs_metrics.counters().reset()

    def consume() -> None:
        with obs_metrics.sse_stream() as state:
            state["completed"] = True
            raise GeneratorExit

    with pytest.raises(GeneratorExit):
        consume()

    _, streams = obs_metrics.snapshot()
    assert dict(streams) == {"started": 1, "completed": 1, "aborted": 0}


def test_a_real_turn_increments_the_stream_counters(
    client: TestClient, seeded: Seeded
) -> None:
    """Driven through the real owner SSE route, not a synthetic call."""
    from app.observability import metrics as obs_metrics

    client.cookies.set("eva_session", seeded.token)
    obs_metrics.counters().reset()

    with client.stream(
        "POST", f"/api/v1/conversations/{seeded.ids['conversation']}/messages",
        json={"text": "How long does shipping take?"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    # The stream reached a terminal event; which one depends on whether the
    # environment has a configured provider, and both are legitimate here.
    assert "event:" in body
    _, streams = obs_metrics.snapshot()
    assert dict(streams)["started"] == 1
    assert dict(streams)["completed"] + dict(streams)["aborted"] == 1


def test_queue_depth_reports_absence_rather_than_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable Redis omits the series; it must not render as an empty queue.

    "Queue empty" and "cannot see the queue" must not look the same on a graph. Redis
    is not running in this environment, which is why the failing path is exercised
    explicitly rather than skipped — a skip here would leave the distinction untested.
    """
    from app.observability import metrics as obs_metrics

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    family = obs_metrics._queue_depth_family()

    assert family.name == "eva_queue_depth"
    assert family.samples == ()


def test_queue_depth_is_read_as_a_sorted_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The arq queue is a sorted set, so LLEN would raise WRONGTYPE and ZCARD is right.

    PLAN §8.33 suggested ``LLEN arq:queue`` before this row checked the type. ``zadd``
    in ``arq/connections.py`` makes it a zset, confirmed against the running Redis
    container. Asserted by calling the collector against a real zset rather than by
    grepping the source: the first version of this test matched the word "LLEN" inside
    its own explanatory comment, which is a green test that proves nothing.
    """
    from app.observability import metrics as obs_metrics

    redis = pytest.importorskip("redis")
    client = redis.Redis(
        host="127.0.0.1", port=6379, socket_connect_timeout=0.25, socket_timeout=0.25
    )
    try:
        client.ping()
    except Exception:
        pytest.skip("no Redis server reachable to hold a real zset")

    key = f"probe:metrics:{uuid.uuid4().hex[:8]}"
    try:
        client.zadd(key, {"a": 1, "b": 2})
        # LLEN against a zset is a WRONGTYPE error, which is the failure this asserts
        # the collector does not invite.
        with pytest.raises(redis.exceptions.ResponseError):
            client.llen(key)

        monkeypatch.setattr(obs_metrics, "QUEUE_KEY", key)
        family = obs_metrics._queue_depth_family()
        assert family.samples[0].value == 2
        assert family.samples[0].labels["queue"] == key
    finally:
        client.delete(key)


def test_db_pool_family_reports_the_engines_own_counters() -> None:
    from app.observability import metrics as obs_metrics

    family = obs_metrics._pool_family()
    by_state = {sample.labels["state"]: sample.value for sample in family.samples}

    assert set(by_state) == {"checked_out", "idle", "size"}
    # A pool that is never asked cannot be saturated; the gauge must be readable.
    assert by_state["size"] > 0
    assert by_state["checked_out"] >= 0
    assert by_state["idle"] >= 0
    # `overflow` is deliberately absent: SQLAlchemy reports the *remaining* overflow
    # budget, which is negative on an idle pool (measured -5). A gauge that reads as
    # alarming while the pool is healthy would be worse than not publishing it.
    assert "overflow" not in by_state


def test_prometheus_and_admin_endpoints_agree(
    client: TestClient, seeded: Seeded
) -> None:
    """The text scrape and the JSON summary must be one definition, not two.

    Both call ``tenant_families``; this asserts the outcome rather than the structure,
    because the whole point of the ruling's "adopt Prometheus later without renaming"
    is that the dashboard is already reading the same numbers.
    """
    parsed = parse_exposition(client.get("/metrics").text)
    client.cookies.set("eva_session", seeded.token)
    body = client.get("/api/v1/admin/metrics/summary").json()

    for family in body["families"]:
        for sample in family["samples"]:
            text_value = series(parsed, family["name"], **sample["labels"])
            assert text_value == pytest.approx(sample["value"]), (
                f"{family['name']} disagrees between /metrics and /summary"
            )
