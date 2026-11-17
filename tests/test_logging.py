"""OPS-01: structured logs, per-request correlation, and no content at info.

§22 asks for four things and this file tests each one where it can actually break:

* **JSON** — the formatter is exercised directly, because "we set a formatter" is not
  a claim about the bytes that leave the process.
* **correlation id** — a client-supplied `X-Request-ID` is echoed, an unusable one is
  replaced rather than trusted, and two requests never share an id.
* **tenant id / conversation id** — asserted on the *access line of a real request*,
  not on `current()` after a `bind`: the ids are worthless if they do not reach the
  line that ships.
* **no conversation content at info** — a canary string is driven through a real turn
  and must not appear in any emitted line, and an AST guard fails the suite if an
  info-or-above call is ever handed a content-like name.

The two traps this file is built around, both of which produce a green suite that
proves nothing:

1. **`caplog` can be blind.** `configure()` adds a handler rather than replacing the
   root logger's, so the capture handler is still attached when the lifespan runs.
2. **A guard can scan nothing.** The AST test asserts a floor on how many calls it
   read, and proves the detector flags a violation in a synthetic source — a scanner
   that silently matches nothing is the same failure as the loose SQL rule in §8.30.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import ast
import json
import logging
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability import logs

from test_conversation import client, published_bot, stub_embedder, tenant  # noqa: F401


class Capture(logging.Handler):
    """Collects records so a test can inspect both the record and the shipped line.

    The JSON line is rendered at *emit* time, not when the test reads it. Formatting
    lazily would read the request context after the middleware had reset it, and every
    id assertion below would fail against code that works — or, worse, pass against
    code that does not.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []
        self.rendered: list[str] = []
        self._formatter = logs.JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.rendered.append(self._formatter.format(record))

    def lines(self) -> list[str]:
        return list(self.rendered)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records) + "\n" + "\n".join(
            self.rendered
        )


@pytest.fixture
def captured():
    handler = Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    # The root level filters below the handler, and `configure()` set it from LOG_LEVEL
    # in the app's lifespan. Info is the level §22 names, so pin it rather than assume.
    level = root.level
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.setLevel(level)
        root.removeHandler(handler)


# --- the formatter ---------------------------------------------------------


def test_one_record_is_one_json_line() -> None:
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    line = logs.JsonFormatter().format(record)
    assert "\n" not in line, "a multi-line record breaks one-line-per-event"
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["msg"] == "hello world"
    assert payload["ts"].endswith("+00:00")


def test_ids_are_null_rather_than_absent_when_unset() -> None:
    """A fixed schema: an absent key and a null key are different to an indexer."""
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="no context", args=(), exc_info=None,
    )
    payload = json.loads(logs.JsonFormatter().format(record))
    for field in ("request_id", "tenant_id", "conversation_id"):
        assert field in payload
        assert payload[field] is None


def test_ids_from_the_context_reach_the_line() -> None:
    with logs.scoped(tenant_id="tenant-1", conversation_id="conv-1"):
        line = logs.JsonFormatter().format(
            logging.LogRecord(
                name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
                msg="inside", args=(), exc_info=None,
            )
        )
    payload = json.loads(line)
    assert payload["tenant_id"] == "tenant-1"
    assert payload["conversation_id"] == "conv-1"


def test_an_exception_keeps_the_traceback_in_the_same_object() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(logs.JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]


def test_a_value_json_cannot_encode_does_not_raise() -> None:
    """A formatter that raises replaces the message it was formatting with a traceback."""
    payload = json.loads(
        logs.JsonFormatter().format(
            logging.LogRecord(
                name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
                msg="%s", args=(object(),), exc_info=None,
            )
        )
    )
    assert payload["msg"].startswith("<object object")


def test_an_unknown_context_key_is_ignored_rather_than_written() -> None:
    with logs.scoped(tenant_id="t", nonsense="x"):
        payload = json.loads(
            logs.JsonFormatter().format(
                logging.LogRecord(
                    name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
                    msg="m", args=(), exc_info=None,
                )
            )
        )
    assert "nonsense" not in payload


def test_scoped_restores_the_previous_value() -> None:
    """The purge loops over tenants; a line attributed to the wrong tenant is worse
    than one with none."""
    with logs.scoped(tenant_id="outer"):
        with logs.scoped(tenant_id="inner"):
            assert logs.current()["tenant_id"] == "inner"
        assert logs.current()["tenant_id"] == "outer"


def test_snapshot_carries_the_context_into_another_thread() -> None:
    seen: list[dict] = []

    def work() -> None:
        seen.append(logs.current())

    with logs.scoped(tenant_id="t-9", conversation_id="c-9"):
        context = logs.snapshot()
        thread = threading.Thread(target=lambda: context.run(work))
        thread.start()
        thread.join()
        # Without the snapshot, the thread would report no ids at all.
        bare = threading.Thread(target=work)
        bare.start()
        bare.join()

    assert seen[0]["tenant_id"] == "t-9"
    assert seen[0]["conversation_id"] == "c-9"
    assert seen[1]["tenant_id"] is None


# --- the middleware --------------------------------------------------------


def _echo_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(logs.RequestContextMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        logs.bind(tenant_id="tenant-from-route")
        return {"ok": "yes"}

    return app


def test_request_id_is_echoed_back_to_the_caller() -> None:
    with TestClient(_echo_app()) as test_client:
        response = test_client.get("/ping", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_an_absent_request_id_is_generated_and_unique() -> None:
    with TestClient(_echo_app()) as test_client:
        first = test_client.get("/ping").headers["x-request-id"]
        second = test_client.get("/ping").headers["x-request-id"]
    assert first and second and first != second


def test_an_unusable_request_id_is_replaced_not_trusted() -> None:
    """The header is attacker-controlled text on its way into a log line."""
    hostile = "x" * 200
    with TestClient(_echo_app()) as test_client:
        response = test_client.get("/ping", headers={"X-Request-ID": hostile})
    echoed = response.headers["x-request-id"]
    assert echoed != hostile
    assert len(echoed) == 32, "a rejected id is replaced by a fresh uuid4 hex"


def test_the_access_line_is_json_and_carries_the_route_bound_ids(captured) -> None:
    with TestClient(_echo_app()) as test_client:
        test_client.get("/ping", headers={"X-Request-ID": "corr-1"})
    access = [
        json.loads(line)
        for line in captured.lines()
        if json.loads(line)["logger"] == "app.http"
    ]
    assert len(access) == 1, "exactly one access line per request"
    line = access[0]
    assert line["request_id"] == "corr-1"
    assert line["tenant_id"] == "tenant-from-route"
    assert "/ping" in line["msg"]
    assert "200" in line["msg"]


def test_the_access_line_omits_the_query_string(captured) -> None:
    """The query string is client input, and one route could put visitor words in it.

    `widget.upload_file` takes the file name from `?filename=`, which is
    visitor-supplied. Asserted on `app.http` only: httpx logs its own request URL at
    INFO, which is the test client describing itself, not the server logging a request.
    """
    with TestClient(_echo_app()) as test_client:
        test_client.get("/ping?secret=visitor-words")
    access = [
        json.loads(line) for line in captured.lines()
        if json.loads(line)["logger"] == "app.http"
    ]
    assert access, "no access line was emitted"
    assert all("visitor-words" not in line["msg"] for line in access)


def test_the_access_line_reports_the_real_status(captured) -> None:
    app = FastAPI()
    app.add_middleware(logs.RequestContextMiddleware)

    @app.get("/boom")
    def boom() -> None:
        raise ValueError("no")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        assert test_client.get("/boom").status_code == 500
    access = [
        json.loads(line) for line in captured.lines()
        if json.loads(line)["logger"] == "app.http"
    ]
    assert "500" in access[-1]["msg"]


# --- through the real app --------------------------------------------------


def _login(client: TestClient, tenant_body: dict) -> None:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": tenant_body["email"], "password": "correct-horse-battery"},
    )
    assert response.status_code == 200, response.text
    for header in response.headers.get_list("set-cookie"):
        if header.startswith("eva_session="):
            client.cookies.set(
                "eva_session", header.split("eva_session=", 1)[1].split(";", 1)[0]
            )
            return
    raise AssertionError("no session cookie")


def _start(client: TestClient, bot_id: str) -> str:
    response = client.post(
        "/api/v1/conversations",
        json={"bot_id": bot_id, "visitor_session_id": uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()["conversation_id"]


def test_a_real_request_line_carries_tenant_and_conversation(
    client: TestClient, tenant, published_bot, captured
) -> None:
    """The end-to-end claim: `bind` in a dependency reaches the line that ships.

    This is the assertion that would have caught the `ContextVar.set()` mistake —
    `current_user` and the endpoint run in a worker thread with a *copy* of the
    context, so a `set()` in the dependency is invisible to the line below it.
    """
    _login(client, tenant)
    conversation_id = _start(client, published_bot["bot_id"])
    captured.records.clear()

    response = client.get(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 200, response.text

    access = [
        json.loads(line) for line in captured.lines()
        if json.loads(line)["logger"] == "app.http"
    ]
    assert access, "the conversation read produced no access line"
    line = access[-1]
    assert line["tenant_id"] == tenant["tenant_id"]
    assert line["conversation_id"] == conversation_id
    assert line["request_id"]


def test_an_anonymous_request_has_a_correlation_id_and_no_tenant(
    client: TestClient, captured
) -> None:
    client.cookies.clear()
    captured.records.clear()
    response = client.get("/health", headers={"X-Request-ID": "anon-1"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "anon-1"
    access = [
        json.loads(line) for line in captured.lines()
        if json.loads(line)["logger"] == "app.http"
    ]
    assert access[-1]["request_id"] == "anon-1"
    assert access[-1]["tenant_id"] is None


# --- the worker ------------------------------------------------------------


def test_the_worker_log_config_is_installed_as_json() -> None:
    """arq's `--custom-log-dict` is handed a dict, applied before `on_startup`.

    Applying it for real — rather than asserting on the dict's shape — is what proves
    the `formatter` reference resolves; a wrong `()` entry raises inside `dictConfig`
    at worker boot, which is a crash, not a log line.
    """
    import logging.config

    from worker.settings import LOG_CONFIG

    previous = logging.getLogger().handlers[:]
    previous_level = logging.getLogger().level
    try:
        logging.config.dictConfig(LOG_CONFIG)
        root = logging.getLogger()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, logs.JsonFormatter)]
        assert len(json_handlers) == 1, "exactly one JSON handler, or lines double up"
        # arq's own logger must not carry a second plain-text handler.
        arq_logger = logging.getLogger("arq")
        assert arq_logger.handlers == []
        assert arq_logger.propagate is True
    finally:
        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "handlers": {},
                "root": {"handlers": [], "level": previous_level},
                "loggers": {
                    name: {"handlers": [], "level": "INFO", "propagate": True}
                    for name in ("arq", "uvicorn", "uvicorn.access")
                },
            }
        )
        root = logging.getLogger()
        root.handlers[:] = previous
        root.setLevel(previous_level)


# --- conversation content stays out of info --------------------------------


CANARY = "CANARY-7Q4z-visitor-words-must-not-be-logged"


def test_a_visitors_words_never_reach_an_info_line(
    client: TestClient, tenant, published_bot, captured
) -> None:
    """The row's core prohibition, driven through a real turn.

    The question is sent over the wire, so it passes through the router, the turn
    loop, retrieval, the prompt builder and the provider — every place a future
    `logger.info("asking: %s", question)` might be added.
    """
    _login(client, tenant)
    conversation_id = _start(client, published_bot["bot_id"])
    captured.records.clear()

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"text": f"{CANARY} how long is shipping?"},
    )
    assert response.status_code == 200, response.text
    list(response.iter_lines())  # drain the stream: the turn runs while it is read

    assert captured.records, "no lines were emitted; the assertion would be vacuous"
    emitted = captured.text()
    assert CANARY not in emitted
    # The conversation is still traceable, which is the point of logging ids instead
    # of words.
    assert any(
        json.loads(line)["conversation_id"] == conversation_id
        for line in captured.lines()
    )


# --- the guard -------------------------------------------------------------

_API = Path(__file__).resolve().parents[1] / "backend" / "api" / "app"
_WORKER = Path(__file__).resolve().parents[1] / "backend" / "worker" / "worker"

# Names that carry a visitor's words or a tenant's private text. Deliberately a list
# of identifiers rather than a substring match on the message: a message may quote a
# field name without being about content.
_CONTENT_NAMES = frozenset(
    {
        "question", "answer", "text", "content", "message", "messages", "body",
        "prompt", "transcript", "chunks", "reply", "visitor_name", "visitor_email",
        "page_url",
    }
)
_LEVELS = frozenset({"info", "warning", "error", "exception", "critical", "log", "warn"})


def _content_names(node: ast.AST) -> list[str]:
    """Names in one log argument that carry text rather than an id or a count.

    `len(chunks)` is a count and must not count as logging the chunks: a guard that
    flags it is a guard someone deletes. The call is skipped rather than the name
    subtracted, so `logger.info(len(chunks), chunks)` still flags the second one.
    """
    found: list[str] = []

    def visit(current: ast.AST) -> None:
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name) and func.id == "len":
                return
        name = None
        if isinstance(current, ast.Name):
            name = current.id
        elif isinstance(current, ast.Attribute):
            name = current.attr
        if name in _CONTENT_NAMES:
            found.append(name)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return found


def _suspicious(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_logger = (
            isinstance(func, ast.Attribute)
            and func.attr in _LEVELS
            and isinstance(func.value, ast.Name)
            and func.value.id in ("logger", "logging", "_log")
        )
        if not is_logger:
            continue
        for value in list(node.args) + [keyword.value for keyword in node.keywords]:
            found.extend((node.lineno, name) for name in _content_names(value))
    return found


def _scan(roots: tuple[Path, ...]) -> tuple[list[str], int]:
    violations: list[str] = []
    calls = 0
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LEVELS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("logger", "logging", "_log")
                ):
                    calls += 1
            violations.extend(
                f"{path.relative_to(root.parent.parent.parent)}:{line}: {name}"
                for line, name in _suspicious(tree)
            )
    return violations, calls


def test_the_guard_flags_a_content_field_being_logged() -> None:
    """A guard that cannot fail is documentation. Mutation-check it in the test."""
    tree = ast.parse('logger.info("asking: %s", question)')
    assert _suspicious(tree) == [(1, "question")]
    assert _suspicious(ast.parse('logger.warning("x=%s", payload.content)')) == [
        (1, "content")
    ]


def test_the_guard_still_flags_a_nested_content_name_next_to_a_count() -> None:
    """The `len()` exemption must not launder the rest of the same call."""
    assert _suspicious(ast.parse('logger.info("%d %s", len(chunks), chunks)')) == [
        (1, "chunks")
    ]
    assert _suspicious(ast.parse('logger.info("%d", len(chunks))')) == []


def test_the_guard_accepts_ids_and_counts() -> None:
    assert _suspicious(ast.parse('logger.info("turn %s", conversation_id)')) == []
    assert _suspicious(ast.parse('logger.info("deleted %d", deleted)')) == []


def test_no_info_line_is_handed_conversation_content() -> None:
    violations, calls = _scan((_API, _WORKER))
    # A floor, not a count: zero findings from zero files is indistinguishable from a
    # clean tree (§8.25's lesson, restated by SEC-13).
    assert calls >= 20, f"the scan read only {calls} logger calls; it is not looking"
    assert violations == [], (
        "conversation content must not be logged at info or above; "
        f"log an id or a count instead: {violations}"
    )
