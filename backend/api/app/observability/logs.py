"""Structured JSON logs with per-request correlation (OPS-01, §22).

§22 asks for four things at once, and they are easy to satisfy in a way that looks
right and is not:

* **structured JSON** — one JSON object per line, so a log shipper needs no regex;
* **per-request correlation id** — so the lines of one request can be put back
  together, including the ones written by a library;
* **tenant id, conversation id** — so an operator can answer "what happened to this
  conversation" without guessing, and so a line can be scoped to a tenant;
* **conversation content never logged at info level** — metadata only.

The design decisions worth stating, because the obvious version of each is wrong:

**The field bag is one mutable dict per request, not a `ContextVar` per field.**
FastAPI runs sync dependencies and sync endpoints in a worker thread, and each call
gets a *copy* of the context (`anyio.to_thread.run_sync`). A `ContextVar.set()` in
`current_user` is therefore invisible to the endpoint it authenticated, and to every
log line that endpoint writes — the ids would appear on exactly the lines that never
needed them. Mutating a dict that was placed in the context once *is* visible in
every copy, because the copies share the object. `set()` is used only by the
middleware, which runs in the request's own task.

**The access line is ours, not uvicorn's.** `uvicorn.access` writes
`INFO: 1.2.3.4:5 - "GET / HTTP/1.1" 200` — not JSON, and emitted after the response
completes, by which point the request context is gone, so it cannot carry a
correlation id. `configure` therefore clears the `uvicorn.access` handlers and leaves
it non-propagating, and this module's middleware writes exactly one line per request,
in JSON, with the ids. No container flag is involved: uvicorn installs that logger
before the app is imported, so the fix has to happen here where it can also be tested.

**Two lines at the very start of a container are still uvicorn's text.** Uvicorn logs
"Started server process" *before* it runs the app's lifespan, where `configure` is
called, so those two lines are text in every deployment. They carry no request and no
tenant, and fixing them would mean configuring logging at import time — which would
also reconfigure pytest on import. Left as a known, bounded gap; the worker does not
have it because arq's CLI offers an early hook (`dict_config` + `--custom-log-dict`).

**The correlation id from a client is validated, not trusted.** `X-Request-ID` is
useful (Caddy, a browser, a support ticket), but it is attacker-controlled text
going into a log line, so only a short `[A-Za-z0-9._-]` value is accepted. JSON
encoding already escapes newlines, which rules out forging a second line; the
charset and length limit stop a caller from writing kilobytes per request.

**No content is logged because nothing here can log content.** The middleware logs
method, path (never the query string) and status. `tests/test_logging.py` proves the
rest two ways: a canary string driven through a real turn must not appear in any
captured record, and an AST guard fails the suite if an info-or-above call is ever
handed a name like `question`, `answer` or `content`.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from datetime import UTC, datetime
from typing import Any

# The closed vocabulary of context fields. An unknown key is ignored rather than
# written: a log line whose shape depends on its caller cannot be indexed.
_FIELDS = ("request_id", "tenant_id", "conversation_id")

_context: contextvars.ContextVar[MutableMapping[str, str | None] | None] = (
    contextvars.ContextVar("eva_log_context", default=None)
)

logger = logging.getLogger("app.http")

_REQUEST_ID_HEADER = b"x-request-id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def bind(**fields: object) -> None:
    """Attach ids to the current log context. No-op for an unknown field.

    Called where the id becomes known: the authenticated tenant in
    `auth.dependencies.current_user`, the conversation in the routes and the turn
    loop, the tenant in the worker's jobs.

    A context is created if there is none. Inside a request the middleware has
    already installed one; in an arq job there is no request middleware, and each
    job body runs as its own task, so the context it creates belongs to that job.
    """
    context = _context.get(None)
    if context is None:
        context = dict.fromkeys(_FIELDS)
        _context.set(context)
    for key, value in fields.items():
        if key in _FIELDS and value is not None:
            context[key] = str(value)


def current() -> dict[str, str | None]:
    """A read-only copy of the current context, for tests and diagnostics."""
    context = _context.get(None) or {}
    return {key: context.get(key) for key in _FIELDS}


@contextlib.contextmanager
def scoped(**fields: object) -> Iterator[None]:
    """Bind ids for the duration of a block, then put the previous values back.

    The purge job loops over tenants inside one job; binding without restoring would
    label the summary line with whichever tenant happened to be last, and a log line
    attributed to the wrong tenant is worse than one with no tenant at all.
    """
    before = current()
    bind(**fields)
    try:
        yield
    finally:
        context = _context.get(None)
        if context is not None:
            for key in _FIELDS:
                context[key] = before[key]


def snapshot() -> contextvars.Context:
    """The current context, to carry into a background thread.

    Work started by a request (handover delivery, webhook emission) runs after the
    response returns, and a bare `threading.Thread` starts with an empty context —
    so without this the one line that says whether the notification was delivered
    would be the one line that cannot be traced back to its conversation.
    """
    return contextvars.copy_context()


class JsonFormatter(logging.Formatter):
    """One JSON object per record: timestamp, level, logger, message, ids, traceback."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the line
            message = f"<unformattable record: {record.msg!r}>"
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
        }
        context = _context.get(None) or {}
        for key in _FIELDS:
            payload[key] = context.get(key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # `default=str` so a UUID or a Path in a message never raises inside the
        # formatter: a logging failure that replaces the message it was formatting
        # is worse than an ugly field.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(level: str | None = None) -> None:
    """Send every logger to stdout as JSON. Safe to call twice.

    Called from the API's lifespan and the worker's `on_startup`, not at import:
    importing `app.main` in a test must not install a process-wide stdout handler.

    The handler is *added* to the root logger, not swapped in. Swapping would remove
    whatever else is attached — in the suite that is pytest's capture handler, and a
    log assertion that cannot see records passes for the wrong reason, which is the
    failure this row's tests exist to avoid.
    """
    root = logging.getLogger()
    if not any(getattr(h, "_eva_json", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._eva_json = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel((level or os.environ.get("LOG_LEVEL", "INFO")).upper())

    # uvicorn installs its own handlers before the app is imported; left in place they
    # would write non-JSON lines next to ours. `uvicorn.access` is silenced rather than
    # redirected because this module's middleware writes the access line — uvicorn
    # emits its own after the response completes, when the ids are already gone.
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False


def dict_config(level: str | None = None) -> dict[str, Any]:
    """A `logging.config.dictConfig` document for processes that configure logging
    before any of our code runs.

    `arq` installs its own plain-text handler from its CLI (`default_log_config`)
    before `on_startup` is called, so its banner — the first lines a worker writes —
    would stay text next to our JSON. `--custom-log-dict` hands us that moment, which
    is why the worker uses this and not `configure()`. `disable_existing_loggers` stays
    False so a library logger created at import still emits; `arq` gets an empty
    handler list and propagation on, so its records reach our handler once instead of
    twice.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"eva": {"()": JsonFormatter}},
        "handlers": {
            "eva": {
                "class": "logging.StreamHandler",
                "formatter": "eva",
                "stream": "ext://sys.stdout",
            }
        },
        "root": {
            "handlers": ["eva"],
            "level": (level or os.environ.get("LOG_LEVEL", "INFO")).upper(),
        },
        "loggers": {
            "arq": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False},
        },
    }


def _request_id(scope: dict) -> str:
    for key, value in scope.get("headers") or ():
        if key == _REQUEST_ID_HEADER:
            candidate = value.decode("latin-1").strip()
            if _VALID_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Per-request context, `X-Request-ID` echo, and the one JSON access line.

    Pure ASGI rather than `BaseHTTPMiddleware`: the latter interposes a task and a
    queue around the response, which changes streaming behaviour, and the SSE turn is
    the response this app cares most about.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        token = _context.set(
            {"request_id": request_id, "tenant_id": None, "conversation_id": None}
        )
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message):  # type: ignore[no-untyped-def]
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers") or [])
                headers.append((_REQUEST_ID_HEADER, request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # One line per request, written before the context is reset so it carries
            # the ids the request bound. `path` only: the query string is client input
            # and a visitor's words could one day be in it, which is the leak this row
            # forbids.
            logger.info(
                "%s %s -> %d (%.1f ms)",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                (time.perf_counter() - started) * 1000,
            )
            _context.reset(token)
