"""OPS-02: OpenTelemetry spans on the conversation turn (§22, ARCHITECTURE §9.8).

§22 names one trace and three numbers: the turn, retrieval ms, LLM ms
(time-to-first-token + total), and per-provider errors. Each has a test here that
reads the *recorded span tree*, not the code — because the shipped deployment has
no SDK, so a span that is constructed wrongly would otherwise be indistinguishable
from one that is constructed correctly.

The provider is a recording ``TracerProvider`` installed once for the session;
``set_tracer_provider`` is process-global and refuses a second install, so a
per-test install would pass alone and fail in the full suite — the same trap
OPS-01 recorded for a reloaded session module. Each test resets the recorder.

What is deliberately asserted as *absent* is as important as what is present: the
visitor's question must not appear in any attribute or event, because a trace is
exported off-box. The canary runs through the real turn loop, so it covers the
prompt builder and the provider call rather than only the span calls.
"""

import asyncio
import logging
import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest

from opentelemetry.trace import (
    Span,
    SpanContext,
    SpanKind,
    TraceFlags,
    Tracer,
    TracerProvider,
)

from app.observability import tracing
from test_conversation import (  # noqa: F401 - fixtures used by pytest
    FakeProvider,
    _start,
    client,
    published_bot,
    stub_embedder,
    tenant,
)


class RecordedSpan(Span):
    """A span that keeps its attributes, its events and its parent's trace id."""

    def __init__(self, name: str, attributes=None, parent: SpanContext | None = None):
        self.name = name
        self.attributes: dict = dict(attributes or {})
        self.events: list[tuple[str, dict]] = []
        self.exceptions: list[str] = []
        self.ended = False
        self.status = None
        valid_parent = parent is not None and parent.is_valid
        trace_id = parent.trace_id if valid_parent else uuid.uuid4().int
        self.context = SpanContext(
            trace_id=trace_id, span_id=uuid.uuid4().int >> 64 or 1,
            is_remote=False, trace_flags=TraceFlags.SAMPLED,
        )
        self.parent_trace_id = parent.trace_id if valid_parent else None

    def get_span_context(self) -> SpanContext:
        return self.context

    def is_recording(self) -> bool:
        return not self.ended

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value

    def set_attributes(self, attributes) -> None:
        self.attributes.update(attributes)

    def add_event(self, name, attributes=None, timestamp=None) -> None:
        self.events.append((name, dict(attributes or {})))

    def update_name(self, name) -> None:
        self.name = name

    def end(self, end_time=None) -> None:
        self.ended = True

    def record_exception(
        self, exception, attributes=None, timestamp=None, escaped=False
    ) -> None:
        # FastAPI instruments its own request spans and calls this with the HTTP
        # exception it is handling. Only this application's own spans are recorded
        # (see RecordingProvider), and a test asserts separately that this module
        # never calls `record_exception` itself.
        self.exceptions.append(type(exception).__name__)

    def set_status(self, status, description=None) -> None:
        self.status = status


class RecordingTracer(Tracer):
    def __init__(self, spans: list[RecordedSpan]) -> None:
        self.spans = spans

    def start_span(
        self, name, context=None, kind=SpanKind.INTERNAL, attributes=None,
        links=None, start_time=None, record_exception=True, set_status_on_exception=True,
    ) -> RecordedSpan:
        parent = tracing.trace.get_current_span(context) if context is not None else None
        parent_context = parent.get_span_context() if parent is not None else None
        span = RecordedSpan(name, attributes, parent_context)
        self.spans.append(span)
        return span

    def start_as_current_span(self, name, *args, **kwargs):
        """FastAPI instruments its own request handling through this entry point."""
        from opentelemetry.trace import use_span

        span = self.start_span(
            name, kwargs.get("context"), attributes=kwargs.get("attributes")
        )
        span.used_current_span_context = True
        return use_span(span, end_on_exit=kwargs.get("end_on_exit", True))


class RecordingProvider(TracerProvider):
    """Records only this application's own spans.

    ``fastapi==0.163.1`` ships its own OTel instrumentation and calls
    ``get_tracer`` per request, so a provider that answered every scope would
    record a span for all 738 tests in the suite — an unbounded list and a global
    behaviour change caused by a fixture in this file. Filtering on the scope keeps
    the fixture's effect to the spans under test (the same reason OPS-01's
    ``configure()`` adds a handler instead of replacing the root logger's list).
    """

    def __init__(self, spans: list[RecordedSpan]) -> None:
        self.spans = spans

    def get_tracer(self, name, *args, **kwargs) -> RecordingTracer:
        from opentelemetry.trace import NoOpTracer

        if name != tracing.INSTRUMENTATION:
            return NoOpTracer()
        return RecordingTracer(self.spans)


@pytest.fixture(scope="session", autouse=True)
def tracer_provider():
    """Installed once: ``set_tracer_provider`` silently ignores the second call."""
    spans: list[RecordedSpan] = []
    tracing.trace.set_tracer_provider(RecordingProvider(spans))
    return spans


@pytest.fixture
def spans(tracer_provider) -> list[RecordedSpan]:
    tracer_provider.clear()
    yield tracer_provider
    tracer_provider.clear()


def _by_name(spans: list[RecordedSpan], name: str) -> RecordedSpan:
    matches = [span for span in spans if span.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} span, got {len(matches)}"
    return matches[0]


def _turn(client, *, tenant_id, conversation_id, question, provider):
    """Drive one real turn, exactly as the router does."""
    from app.db.session import tenant_session
    from app.retrieval.embedding import HashingEmbedder

    from app.conversation import service as conversation

    events = []
    with tenant_session(uuid.UUID(tenant_id)) as session:

        async def collect():
            async for item in conversation.turn_events(
                session,
                tenant_id=uuid.UUID(tenant_id),
                conversation_id=uuid.UUID(conversation_id),
                question=question,
                embedder=HashingEmbedder(dimension=1024),
                provider=provider,
            ):
                events.append(item)

        asyncio.run(collect())
    return events


# --- the span tree -----------------------------------------------------------


def test_a_turn_emits_one_turn_span_carrying_its_ids_and_duration(
    client, published_bot, tenant, spans
) -> None:
    conversation_id = _start(client, published_bot["bot_id"])
    _turn(
        published_bot, tenant_id=tenant["tenant_id"], conversation_id=conversation_id,
        question="How long does standard shipping take?", provider=FakeProvider(),
    )

    turn = _by_name(spans, "conversation.turn")
    assert turn.attributes["tenant.id"] == tenant["tenant_id"]
    assert turn.attributes["conversation.id"] == conversation_id
    assert turn.attributes["turn.declined"] is False
    assert turn.attributes["duration_ms"] >= 0
    assert turn.ended, "a span that is never ended is invisible to every exporter"


def test_retrieval_is_a_child_span_whose_duration_is_the_retrieval_ms(
    client, published_bot, tenant, spans
) -> None:
    conversation_id = _start(client, published_bot["bot_id"])
    _turn(
        published_bot, tenant_id=tenant["tenant_id"], conversation_id=conversation_id,
        question="How long does standard shipping take?", provider=FakeProvider(),
    )

    turn = _by_name(spans, "conversation.turn")
    retrieval = _by_name(spans, "retrieval.search")
    assert retrieval.parent_trace_id == turn.get_span_context().trace_id, (
        "retrieval must be nested under the turn or the trace cannot be read as a tree"
    )
    assert retrieval.attributes["duration_ms"] >= 0
    assert retrieval.attributes["retrieval.declined"] is False
    assert retrieval.attributes["retrieval.chunks"] >= 1
    assert retrieval.ended


def test_the_llm_span_carries_time_to_first_token_and_total(
    client, published_bot, tenant, spans
) -> None:
    conversation_id = _start(client, published_bot["bot_id"])
    _turn(
        published_bot, tenant_id=tenant["tenant_id"], conversation_id=conversation_id,
        question="How long does standard shipping take?", provider=FakeProvider(),
    )

    turn = _by_name(spans, "conversation.turn")
    llm = _by_name(spans, "llm.stream")
    assert llm.parent_trace_id == turn.get_span_context().trace_id
    assert "llm.time_to_first_token_ms" in llm.attributes
    assert "duration_ms" in llm.attributes
    # Time-to-first-token is a part of the total, never larger than it.
    assert llm.attributes["llm.time_to_first_token_ms"] <= llm.attributes["duration_ms"]
    assert llm.attributes["llm.model"]
    assert llm.ended


def test_a_declined_turn_is_marked_and_asks_no_provider(
    client, published_bot, tenant, spans
) -> None:
    """The floor refused, so no ``llm.stream`` span exists at all."""
    conversation_id = _start(client, published_bot["bot_id"])
    from app.db.session import tenant_session
    from sqlalchemy import text

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text("UPDATE bots SET confidence_threshold = 0.99 WHERE tenant_id = :t"),
            {"t": tenant["tenant_id"]},
        )
        session.commit()

    events = _turn(
        published_bot, tenant_id=tenant["tenant_id"], conversation_id=conversation_id,
        question="What is the airspeed velocity of an unladen swallow?",
        provider=FakeProvider(),
    )

    turn = _by_name(spans, "conversation.turn")
    assert turn.attributes["turn.declined"] is True
    assert [span for span in spans if span.name == "llm.stream"] == []
    assert any(name == "final" for name, _ in events)


# --- per-provider errors -----------------------------------------------------


class _Boom:
    def __init__(self, key: str) -> None:
        self.key = key

    def stream(self, messages, *, model, max_tokens=1024):
        raise RuntimeError("SECRET-UPSTREAM-BODY " + self.key)

    def complete(self, messages, *, model, max_tokens=1024):
        raise RuntimeError("SECRET-UPSTREAM-BODY " + self.key)


class _Ok:
    key = "backup"

    def stream(self, messages, *, model, max_tokens=1024):
        return self._chunks()

    async def _chunks(self):
        from app.conversation.llm import CompletionChunk

        yield CompletionChunk(text="recovered ")

    def complete(self, messages, *, model, max_tokens=1024):
        from app.conversation.llm import Completion

        return Completion(text="recovered")


def test_each_failing_provider_contributes_an_event_with_its_key(spans) -> None:
    from app.conversation.resilience import FallbackChain

    chain = FallbackChain(providers=[_Boom("primary"), _Boom("secondary"), _Ok()])

    async def run():
        with tracing.span("llm.stream") as span:
            async for _ in chain.stream([], model="m"):
                pass
            return span

    span = asyncio.run(run())
    errors = [attrs for name, attrs in span.events if name == "provider.error"]
    assert [attrs["provider_key"] for attrs in errors] == ["primary", "secondary"]
    assert {attrs["error_type"] for attrs in errors} == {"RuntimeError"}
    # The upstream body is provider text and can echo the prompt, so it must not
    # reach the trace — only the type.
    assert "SECRET-UPSTREAM-BODY" not in repr(span.events)


def test_a_tripped_provider_is_recorded_as_skipped_rather_than_failed(spans) -> None:
    from app.conversation.resilience import FallbackChain

    chain = FallbackChain(providers=[_Boom("primary"), _Ok()])
    chain.breaker("primary").record_failure()
    chain.breaker("primary").failure_threshold = 1
    chain.breaker("primary").record_failure()

    async def run():
        with tracing.span("llm.stream") as span:
            async for _ in chain.stream([], model="m"):
                pass
            return span

    span = asyncio.run(run())
    names = [name for name, _ in span.events]
    assert "provider.skipped" in names
    assert (
        "provider.skipped", {"provider_key": "primary", "reason": "circuit_open"}
    ) in span.events


# --- content never reaches a trace -------------------------------------------


def test_the_visitors_question_never_appears_in_a_span(
    client, published_bot, tenant, spans
) -> None:
    canary = "CANARYVISITORWORDS"
    conversation_id = _start(client, published_bot["bot_id"])
    _turn(
        published_bot, tenant_id=tenant["tenant_id"], conversation_id=conversation_id,
        question=f"How long does shipping take {canary}?", provider=FakeProvider(),
    )

    assert spans, "no spans recorded: a guard over zero spans proves nothing"
    dump = repr([(span.name, span.attributes, span.events) for span in spans])
    assert canary not in dump
    # The conversation id *must* be there, or the absence above proves nothing.
    assert conversation_id in dump


def test_the_module_never_records_an_exception_object() -> None:
    """``record_exception`` is the one API that would embed a provider's body."""
    import pathlib

    source = pathlib.Path(tracing.__file__).read_text()
    assert "record_exception(" not in source.replace("def record_exception", "")


# --- it must be safe on the paths that are not a clean turn ------------------


def test_cancelling_a_stream_does_not_corrupt_the_trace_context(
    caplog: pytest.LogCaptureFixture, spans
) -> None:
    """An SSE client disconnect cancels the generator at a ``yield``.

    ``start_as_current_span`` cannot survive that: it attaches the span to the
    *consuming task's* context and detaches on exit, and when that task is
    cancelled the generator is closed from a **different** context, so the detach
    raises ``ValueError: Token was created in a different Context`` and OTel logs
    ``Failed to detach context`` once per disconnect. The cancellation must happen
    in a separate task to reproduce that; cancelling the consuming task itself
    leaves the generator to be closed in the original context and hides the bug —
    which is how the first version of this test passed against the broken code.
    """
    async def stream():
        with tracing.span("llm.stream") as span:
            for index in range(200):
                yield span, index
                await asyncio.sleep(0)

    async def consume(holder: dict) -> None:
        generator = stream()
        holder["generator"] = generator
        async for _span, _index in generator:
            await asyncio.sleep(0.01)

    async def scenario(holder: dict) -> None:
        task = asyncio.create_task(consume(holder))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Closed from this task, which is not the one the span was attached in.
        await holder["generator"].aclose()

    holder: dict = {}
    with caplog.at_level(logging.WARNING, logger="opentelemetry"):
        asyncio.run(scenario(holder))

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "the trace context was corrupted: " + caplog.text[:400]
    )
    span = _by_name(spans, "llm.stream")
    assert span.ended, "a cancelled stream must still end its span"


def test_no_provider_installed_is_a_no_op(caplog: pytest.LogCaptureFixture) -> None:
    """The shipped deployment has the API but no SDK; nothing may raise or log."""
    from opentelemetry import trace as otel_trace

    # The default provider in a process where a test already installed one is still
    # this module's behaviour to prove: call through the no-op path explicitly.
    non_recording = otel_trace.get_tracer("probe").start_span("noop")

    class _NoopProvider(TracerProvider):
        def get_tracer(self, *args, **kwargs):
            return _NoopTracer()

    class _NoopTracer(Tracer):
        def start_span(self, name, *args, **kwargs):
            return non_recording

        def start_as_current_span(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("unused")

    original = tracing.tracer
    tracing.tracer = lambda: _NoopTracer()
    try:
        with caplog.at_level(logging.DEBUG):
            with tracing.span("conversation.turn", **{"tenant.id": "t"}) as span:
                tracing.event("provider.error", provider_key="p")
                tracing.set_attributes(span, **{"turn.model": None})
    finally:
        tracing.tracer = original

    assert not span.is_recording()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
