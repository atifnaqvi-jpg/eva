"""OpenTelemetry traces for the conversation turn (OPS-02, ARCHITECTURE §9.8, §22).

§22 asks for one trace shape: "the conversation turn — retrieval ms, LLM ms
(time-to-first-token + total), per-provider errors". So this module instruments
exactly three things and refuses to grow:

* ``conversation.turn`` — the whole turn, with the ids and the outcome;
* ``retrieval.search`` — embed + retrieve, whose ``duration_ms`` *is* "retrieval ms";
* ``llm.stream`` — the completion, carrying ``llm.time_to_first_token_ms`` and
  ``duration_ms`` (the total).

Per-provider errors are **events on the enclosing span**, not a fourth span tree:
a fallback chain can try three providers in one stream, so the attempt count is
data about the LLM call, not a new nesting level.

**Instrumentation only, because the API is already here and the SDK is not.**
``opentelemetry-api`` is a hard dependency of the pinned ``fastapi==0.163.1`` (it is
in ``Requires-Dist``, not an extra), so the API's no-op implementation ships in the
image whether or not anyone uses it. The **SDK and an exporter are not installed**,
and §20.1's topology has no collector, so in this deployment every span here is
created and immediately discarded. That is a real, bounded gap rather than a
complete feature — see ``docs/PLAN.md`` §8.32. What is bought by writing the
instrumentation now is that it is *tested*: a TracerProvider installed by a test
(or later by the operator, via ``opentelemetry-instrument``) sees a correct span
tree with correct durations, and no call site needs revisiting to gain one.

**Nothing content-shaped is ever an attribute.** The same rule as OPS-01's logs
(§22: "conversation content is not logged"), for the same reason: a trace is
exported off-box, so the visitor's question and the model's answer must not be in
it. Provider error *messages* are excluded too — ``providers.py`` builds them from
up to 300 characters of the provider's response body, which can echo the prompt.
Only the exception's *type* is recorded, plus the provider key.

**The helper must be free when nothing is recording.** Every span is started
through the OTel API, whose default provider returns a ``NonRecordingSpan``; the
attribute calls below are then no-ops and the only cost is a ``perf_counter``
reading per span. No provider is installed at import or in the lifespan: doing so
would (a) be pointless without an SDK and (b) fight ``pytest``, which is the trap
OPS-01 hit when it configured logging at import time.

**Nesting is explicit, not the current-span context.** The obvious implementation is
``tracer.start_as_current_span``, and it is wrong here: these spans are opened
inside an **async generator that yields while they are open**, so the attach/detach
pair straddles a suspension point. When an SSE client disconnects the generator is
cancelled at a ``yield`` and ``use_span`` tries to ``detach`` a token created in a
different context — measured, not guessed: it logs
``ERROR opentelemetry.context: Failed to detach context`` and leaves the context
skewed, once per visitor disconnect. So ``span()`` calls ``start_span`` and passes
the parent down explicitly; the active span is tracked in one ``ContextVar`` that
``event()`` reads, with a guarded reset because a cancelled task never returns.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

# The instrumentation scope. One name for every span in the turn, so a collector
# query can select this application's own spans without matching on span names.
INSTRUMENTATION = "eva.conversation"

# The span an `event()` belongs to. Set by `span()` and read by `event()`, so the
# fallback chain can attach facts to `llm.stream` without the `LlmProvider`
# protocol growing a span parameter — the protocol is the seam WP-13 built, and a
# provider that had to know about tracing would be a provider that cannot be
# faked in a test.
_active: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "eva_trace_span", default=None
)


def tracer() -> trace.Tracer:
    """Resolved per call, not cached at import.

    A tracer obtained before a provider is installed is a ``ProxyTracer`` and
    delegates once one appears, so caching would also work — but resolving per
    span keeps the module correct under the one ordering a library cannot control:
    an operator installing a provider in a startup hook that runs after this module
    was imported.
    """
    return trace.get_tracer(INSTRUMENTATION)


def _attributes(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop unset values: a null attribute is noise in every exporter's output."""
    return {key: value for key, value in fields.items() if value is not None}


def set_attributes(target: Span, /, **fields: Any) -> None:
    """Set several attributes, skipping the unset ones.

    ``Span.set_attribute(key, None)`` is not a valid ``AttributeValue``; the SDK
    warns on it once one is installed. Most of the values here are optional (a
    decline has no model, an answered turn has no error code), so dropping them at
    the edge keeps both the no-op API and a future SDK quiet.
    """
    for key, value in _attributes(fields).items():
        target.set_attribute(key, value)


@contextmanager
def span(name: str, /, *, parent: Span | None = None, **fields: Any) -> Iterator[Span]:
    """Open a span, end it with a ``duration_ms`` attribute, and scope ``event()``.

    ``duration_ms`` is set for every span by this helper rather than by each call
    site, so "retrieval ms" and "the LLM's total" cannot drift apart or be
    forgotten on one branch. The clock is ``perf_counter``: monotonic, so a system
    clock adjustment mid-turn cannot produce a negative duration.
    """
    current_tracer = tracer()
    started = time.perf_counter()
    ancestor = parent if parent is not None else _active.get()
    current = current_tracer.start_span(
        name,
        context=trace.set_span_in_context(ancestor) if ancestor is not None else None,
        attributes=_attributes(fields),
    )
    token = _active.set(current)
    try:
        yield current
    except BaseException:
        current.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        current.set_attribute(
            "duration_ms", round((time.perf_counter() - started) * 1000, 3)
        )
        current.end()
        try:
            _active.reset(token)
        except ValueError:
            # The generator was cancelled or closed from another context, so the
            # context holding this token is already gone. OPS-02 measured that
            # letting this raise logs an ERROR per SSE disconnect.
            pass


def event(name: str, /, **fields: Any) -> None:
    """Record an event on the active span, or nowhere if there is none.

    Used by the fallback chain, which is nested inside ``llm.stream`` and must not
    open a span tree of its own: a chain that tried, got refused and tried again is
    one LLM call with three facts attached, not three spans.
    """
    current = _active.get()
    if current is not None:
        current.add_event(name, _attributes(fields))


def error_event(exc: BaseException, /, **fields: Any) -> None:
    """Record a failure by *type* only.

    ``exc`` is deliberately not passed to ``record_exception``: that would put the
    exception message into the trace, and a provider error message is built from
    the provider's own response body. The type is the part an operator can group
    by, and it is the part that carries no third-party text.
    """
    event("provider.error", error_type=type(exc).__name__, **fields)
