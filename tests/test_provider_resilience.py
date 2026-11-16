"""WP-13: resilience, budgets, rate limits, cache, routing (CONV-11..15).

M2's gate requires "provider fallback survives a simulated outage". The outage is
simulated at the provider boundary — two providers, the first raising on every call
— and driven through the *real* turn loop against the real database, so the proof
covers the whole path a visitor exercises: turn loop → fallback chain → breaker →
SSE event.

The rest of this file tests the policy layers directly, because each is a small
state machine with edge cases that a happy-path integration test would miss:
half-open probes, fixed windows, cache scoping, budget defaulting.

Live provider calls are NOT tested, because there is no provider: `litellm` is a
blocked dependency (HALT-05) and no platform key exists in this deployment. What is
tested is the machinery that decides *which* provider to call and *whether* a turn
may spend money — which is the part that has to be right before a provider is
plugged in.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import asyncio

import pytest

from app.conversation import controls
from app.conversation.llm import Completion, CompletionChunk
from app.conversation.resilience import (
    CircuitBreaker,
    CircuitOpen,
    CircuitState,
    FallbackChain,
)

from test_conversation import (  # noqa: F401 - fixtures used by pytest
    FakeProvider,
    _bot_id,
    _run_turn,
    _start,
    client,
    published_bot,
    stub_embedder,
    tenant,
)


class BrokenProvider(FakeProvider):
    """Raises before the first token — the shape a real outage takes."""

    def __init__(self, key: str = "broken") -> None:
        super().__init__()
        self.key = key

    def stream(self, messages, *, model, max_tokens=1024):
        self.requests.append(list(messages))

        async def gen():
            raise RuntimeError("provider is down")
            yield  # pragma: no cover - unreachable, makes this a generator

        return gen()

    def complete(self, messages, *, model, max_tokens=1024) -> Completion:
        self.requests.append(list(messages))
        return Completion(error="provider is down", model=model)


# --- circuit breaker -------------------------------------------------------


def test_breaker_opens_after_consecutive_failures() -> None:
    breaker = CircuitBreaker(name="p", failure_threshold=3, cooldown_seconds=60)
    assert breaker.state is CircuitState.CLOSED
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED, "two failures under a threshold of 3"
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpen):
        breaker.check()


def test_breaker_half_opens_after_cooldown_and_closes_on_success() -> None:
    breaker = CircuitBreaker(name="p", failure_threshold=1, cooldown_seconds=0.01)
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    import time

    time.sleep(0.02)
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.check()  # a probe is allowed through
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker._failures == 0


def test_failed_half_open_probe_reopens_immediately() -> None:
    """Half-open allows exactly one trial; a failed trial must not leak a second."""
    breaker = CircuitBreaker(name="p", failure_threshold=5, cooldown_seconds=0.01)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    import time

    time.sleep(0.02)
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpen):
        breaker.check()


def test_success_resets_the_failure_count() -> None:
    """Consecutive, not cumulative: an intermittent provider must not trip."""
    breaker = CircuitBreaker(name="p", failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


# --- fallback chain --------------------------------------------------------


def test_chain_falls_back_and_then_skips_the_tripped_provider() -> None:
    dead = BrokenProvider("dead")
    alive = FakeProvider(reply="hello")
    chain = FallbackChain(providers=[dead, alive], failure_threshold=2)

    for _ in range(3):
        completion = chain.complete([], model="m")
        assert completion.text == "hello"
        assert not completion.failed

    assert chain.breaker("dead").state is CircuitState.OPEN
    calls_when_tripped = len(dead.requests)
    chain.complete([], model="m")
    assert len(dead.requests) == calls_when_tripped, (
        "a tripped provider must not receive traffic at all"
    )


def test_chain_reports_when_every_provider_fails() -> None:
    a, b = BrokenProvider("a"), BrokenProvider("b")
    chain = FallbackChain(providers=[a, b])
    completion = chain.complete([], model="m")
    assert completion.failed
    assert "all_providers_failed" in completion.error
    assert "a:" in completion.error and "b:" in completion.error


def test_chain_with_no_providers_fails_cleanly() -> None:
    completion = FallbackChain(providers=[]).complete([], model="m")
    assert completion.failed
    assert completion.error == "no_providers"


def test_stream_falls_back_when_the_first_provider_dies_before_tokens() -> None:
    chain = FallbackChain(providers=[BrokenProvider("dead"), FakeProvider("hi there")])

    async def collect():
        return [chunk.text async for chunk in chain.stream([], model="m")]

    assert asyncio.run(collect()) == ["hi ", "there "]


def test_stream_does_not_splice_two_answers_after_the_first_token() -> None:
    """Once bytes are on the wire, switching providers would corrupt the answer."""

    class DiesMidstream(FakeProvider):
        key = "half"

        def stream(self, messages, *, model, max_tokens=1024):
            async def gen():
                yield CompletionChunk(text="Standard shipping ")
                raise RuntimeError("died mid-answer")

            return gen()

    chain = FallbackChain(providers=[DiesMidstream(), FakeProvider("different answer")])

    async def collect():
        out = []
        with pytest.raises(RuntimeError):
            async for chunk in chain.stream([], model="m"):
                out.append(chunk.text)
        return out

    assert asyncio.run(collect()) == ["Standard shipping "]


# --- the gate claim: fallback survives a simulated outage ------------------


def test_turn_survives_a_provider_outage(client, tenant, published_bot) -> None:
    """M2 gate: a grounded answer still reaches the visitor during an outage."""
    conversation_id = _start(client, published_bot["bot_id"])
    chain = FallbackChain(
        providers=[BrokenProvider("primary"), FakeProvider("Standard shipping is 3-5 days.")]
    )

    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=chain,
    )

    final = events[-1][1]
    assert final["degraded"] is False, "fallback should have produced a real answer"
    answer = "".join(p["text"] for e, p in events if e == "token")
    assert "3-5 days" in answer
    assert final["citations"], "the answer is still grounded after failover"


def test_turn_degrades_to_handover_when_every_provider_is_down(
    client, tenant, published_bot
) -> None:
    """The §11.5 all-fail path: offline message + handover, never an error page."""
    conversation_id = _start(client, published_bot["bot_id"])
    chain = FallbackChain(providers=[BrokenProvider("a"), BrokenProvider("b")])

    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=chain,
    )

    assert events[-1][0] == "final", "the stream must still terminate properly"
    final = events[-1][1]
    assert final["degraded"] is True
    answer = "".join(p["text"] for e, p in events if e == "token")
    assert "having trouble answering" in answer
    assert "team" in answer


def test_outage_breaker_stops_calling_the_dead_provider(
    client, tenant, published_bot
) -> None:
    """Three visits into an outage, the dead provider gets zero further traffic."""
    primary = BrokenProvider("primary")
    chain = FallbackChain(providers=[primary, FakeProvider("ok")], failure_threshold=2)

    for _ in range(3):
        conversation_id = _start(client, published_bot["bot_id"])
        _run_turn(
            client,
            tenant_id=tenant["tenant_id"],
            conversation_id=conversation_id,
            question="How long does standard shipping take?",
            provider=chain,
        )

    assert chain.breaker("primary").state is CircuitState.OPEN
    assert len(primary.requests) == 2, (
        "the breaker must stop traffic after the threshold, not on every turn"
    )


# --- rate limits -----------------------------------------------------------


def test_visitor_limit_trips_per_visitor() -> None:
    limiter = controls.RateLimiter(visitor_limit=2, tenant_limit=100)
    tenant_id = uuid.uuid4()

    async def go():
        for _ in range(2):
            await limiter.check(tenant_id=tenant_id, visitor_session_id="v1")
        with pytest.raises(controls.RateLimited) as exc:
            await limiter.check(tenant_id=tenant_id, visitor_session_id="v1")
        assert exc.value.code == "visitor_rate_limited"
        # A different visitor is unaffected.
        await limiter.check(tenant_id=tenant_id, visitor_session_id="v2")

    asyncio.run(go())


def test_tenant_limit_trips_across_visitors() -> None:
    limiter = controls.RateLimiter(visitor_limit=10, tenant_limit=3)
    tenant_id = uuid.uuid4()

    async def go():
        for i in range(3):
            await limiter.check(tenant_id=tenant_id, visitor_session_id=f"v{i}")
        with pytest.raises(controls.RateLimited) as exc:
            await limiter.check(tenant_id=tenant_id, visitor_session_id="v9")
        assert exc.value.code == "tenant_rate_limited"
        # Another tenant is unaffected.
        await limiter.check(tenant_id=uuid.uuid4(), visitor_session_id="v9")

    asyncio.run(go())


def test_limits_are_independent_of_tenant_id_reuse() -> None:
    """Same visitor string under two tenants must not share a counter."""
    limiter = controls.RateLimiter(visitor_limit=1, tenant_limit=100)
    a, b = uuid.uuid4(), uuid.uuid4()

    async def go():
        await limiter.check(tenant_id=a, visitor_session_id="same")
        await limiter.check(tenant_id=b, visitor_session_id="same")
        with pytest.raises(controls.RateLimited):
            await limiter.check(tenant_id=a, visitor_session_id="same")

    asyncio.run(go())


# --- budgets ---------------------------------------------------------------


def test_budget_defaults_when_the_tenant_sets_none(client, tenant) -> None:
    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        assert (
            controls.monthly_budget(session, tenant_id=uuid.UUID(tenant["tenant_id"]))
            == controls.DEFAULT_MONTHLY_TOKEN_BUDGET
        )


def test_budget_reads_the_tenant_setting(client, tenant) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text(
                "UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"
            ),
            {"s": '{"monthly_token_budget": 5000}', "t": tenant["tenant_id"]},
        )
        session.commit()
        assert (
            controls.monthly_budget(session, tenant_id=uuid.UUID(tenant["tenant_id"]))
            == 5000
        )


def test_budget_exhaustion_blocks_the_turn_before_it_spends(
    client, tenant, published_bot
) -> None:
    """CONV-14 hard stop: the model is never called once the budget is spent."""
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text("UPDATE tenants SET settings = CAST(:s AS jsonb) WHERE id = :t"),
            {"s": '{"monthly_token_budget": 100}', "t": tenant["tenant_id"]},
        )
        session.execute(
            text(
                "INSERT INTO usage_records (tenant_id, kind, units)"
                " VALUES (:t, 'chat', 100)"
            ),
            {"t": tenant["tenant_id"]},
        )
        session.commit()

    conversation_id = _start(client, published_bot["bot_id"])
    provider = FakeProvider()
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )

    assert events[0][0] == "error"
    assert events[0][1]["code"] == "budget_exceeded"
    assert provider.requests == [], "a budget stop must not spend a token"


def test_usage_is_recorded_after_a_grounded_answer(
    client, tenant, published_bot
) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=FakeProvider(reply="3-5 days."),
    )

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        count = session.execute(
            text(
                "SELECT count(*) FROM usage_records"
                " WHERE tenant_id = :t AND kind = 'chat'"
            ),
            {"t": tenant["tenant_id"]},
        ).scalar_one()
    assert count >= 1


# --- response cache --------------------------------------------------------


def test_cache_normalises_case_punctuation_and_whitespace() -> None:
    assert controls.normalise("How long is shipping?!") == "how long is shipping"
    assert controls.normalise("  how   LONG is shipping ") == "how long is shipping"


def test_cache_does_not_merge_questions_that_differ_in_a_word() -> None:
    """Serving one answer for two questions is a correctness bug traded for a hit."""
    cache = controls.ResponseCache()
    tenant_id = uuid.uuid4()
    cache.put(tenant_id, "how long is shipping", {"answer": "3-5 days"})
    assert cache.get(tenant_id, "how much is shipping") is None


def test_cache_is_tenant_scoped() -> None:
    cache = controls.ResponseCache()
    a, b = uuid.uuid4(), uuid.uuid4()
    cache.put(a, "how long is shipping", {"answer": "3-5 days"})
    assert cache.get(b, "how long is shipping") is None


def test_cache_expires_and_evicts_oldest() -> None:
    import time

    cache = controls.ResponseCache(ttl_seconds=0.01, max_entries=2)
    tenant_id = uuid.uuid4()
    cache.put(tenant_id, "q1", {"answer": "a"})
    time.sleep(0.02)
    assert cache.get(tenant_id, "q1") is None

    cache = controls.ResponseCache(max_entries=2)
    cache.put(tenant_id, "q1", {"answer": "a"})
    cache.put(tenant_id, "q2", {"answer": "b"})
    cache.put(tenant_id, "q3", {"answer": "c"})
    assert cache.get(tenant_id, "q1") is None, "oldest entry should be evicted"
    assert cache.get(tenant_id, "q3") is not None


def test_publish_invalidates_the_tenant_cache(client, tenant, published_bot) -> None:
    """A cached answer predates the new knowledge; serving it is a freshness bug."""
    cache = controls.cache()
    tenant_id = uuid.UUID(tenant["tenant_id"])
    cache.put(tenant_id, "how long is shipping", {"answer": "2 days"})

    response = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": published_bot["bot_id"], "version_id": published_bot["version_id"]},
    )
    assert response.status_code == 200, response.text
    assert cache.get(tenant_id, "how long is shipping") is None


def test_cached_answer_is_reused_on_a_fresh_conversation(
    client, tenant, published_bot
) -> None:
    controls.cache().clear()
    provider = FakeProvider(reply="Standard shipping takes 3-5 business days.")

    first = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=first,
        question="How long does standard shipping take?",
        provider=provider,
    )
    assert not events[-1][1].get("cached")
    calls_after_first = len(provider.requests)

    second = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=second,
        question="how long does standard shipping take",
        provider=provider,
    )
    assert events[-1][1]["cached"] is True
    assert len(provider.requests) == calls_after_first, (
        "a cache hit must not spend a completion"
    )
    assert events[-1][1]["citations"], "a cached answer keeps its citations"


def test_cache_is_not_used_when_history_exists(client, tenant, published_bot) -> None:
    """With history the same question may legitimately need a different answer."""
    controls.cache().clear()
    provider = FakeProvider(reply="Standard shipping takes 3-5 business days.")
    conversation_id = _start(client, published_bot["bot_id"])

    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    assert not events[-1][1].get("cached")
    assert len(provider.requests) == 2


def test_degraded_answer_is_not_cached(client, tenant, published_bot) -> None:
    """Caching an outage message would keep serving it after recovery."""
    controls.cache().clear()
    conversation_id = _start(client, published_bot["bot_id"])
    down = FakeProvider()
    down.fail = True
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=down,
    )
    assert controls.cache().get(
        uuid.UUID(tenant["tenant_id"]), "How long does standard shipping take?"
    ) is None


# --- session timeout -------------------------------------------------------


def test_idle_conversation_is_closed_and_refuses_to_answer(
    client, tenant, published_bot
) -> None:
    from sqlalchemy import text

    from app.db.session import tenant_session

    conversation_id = _start(client, published_bot["bot_id"])
    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        session.execute(
            text(
                "UPDATE conversations SET last_activity_at = now() - interval '2 hours'"
                " WHERE id = :id"
            ),
            {"id": conversation_id},
        )
        session.commit()

    provider = FakeProvider()
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )
    assert events[0][0] == "error"
    assert events[0][1]["code"] == "conversation_expired"
    assert provider.requests == []

    with tenant_session(uuid.UUID(tenant["tenant_id"])) as session:
        row = session.execute(
            text("SELECT status, ended_at FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        ).one()
    assert row.status == "closed"
    assert row.ended_at is not None


# --- task routing ----------------------------------------------------------


def test_task_routing_separates_quality_from_speed() -> None:
    assert controls.model_for("answer") != controls.model_for("suggestions")
    assert controls.model_for("answer", override="custom/model") == "custom/model"


def test_unknown_task_is_a_configuration_error() -> None:
    with pytest.raises(ValueError):
        controls.model_for("interpretive-dance")


def test_rate_limit_returns_429_over_http(client, tenant, published_bot) -> None:
    """R36 at the edge: the visitor gets 429 + Retry-After, not a queued turn."""
    conversation_id = _start(client, published_bot["bot_id"])
    limiter = controls.limiter()
    limiter.reset()
    saved = limiter.visitor_limit
    limiter.visitor_limit = 1
    try:
        first = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"text": "How long does standard shipping take?"},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"text": "How long does standard shipping take?"},
        )
        assert second.status_code == 429
        assert second.json()["detail"]["error"]["code"] == "visitor_rate_limited"
        assert second.headers["retry-after"]
    finally:
        limiter.visitor_limit = saved
        limiter.reset()


# --- CONV-11: the OpenAI-compatible provider, over a real socket -------------
#
# A real HTTP server rather than a mock transport: the parts most likely to break
# are SSE framing, chunk splitting and the [DONE] terminator, and a mock returns
# whole strings that never split. This server writes the stream in awkward pieces.


class _Handler:
    def __init__(self, status=200, frames=None, body=None, chunk_split=7):
        self.status = status
        self.frames = frames or []
        self.body = body
        self.chunk_split = chunk_split
        self.requests: list[dict] = []

    def __call__(self, handler):
        import json as _json

        length = int(handler.headers.get("Content-Length") or 0)
        payload = handler.rfile.read(length) if length else b""
        try:
            self.requests.append(_json.loads(payload) if payload else {})
        except ValueError:
            self.requests.append({})
        if self.body is not None:
            raw = _json.dumps(self.body).encode()
            handler.send_response(self.status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        handler.send_response(self.status)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        for frame in self.frames:
            data = frame.encode()
            for i in range(0, len(data), self.chunk_split):
                handler.wfile.write(data[i : i + self.chunk_split])
                handler.wfile.flush()
        handler.wfile.flush()


@pytest.fixture
def server():
    import http.server
    import threading

    servers: list[http.server.HTTPServer] = []

    def start(handler_fn):
        # A per-server subclass, not an attribute set on BaseHTTPRequestHandler:
        # that class is shared, so patching it leaks one test's handler into the
        # next server.
        handler_class = type(
            "Handler",
            (http.server.BaseHTTPRequestHandler,),
            {
                "log_message": lambda *a, **k: None,
                # Wrapped in a function: a plain callable stored on the class is not
                # a descriptor, so it would be called without `self`.
                "do_POST": lambda this: handler_fn(this),
            },
        )
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_class)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()


def _provider(base_url, key="deepseek"):
    from app.conversation.providers import OpenAiCompatibleProvider, ProviderProfile

    profile = ProviderProfile(
        key=key,
        base_url=base_url,
        model_prefix=f"{key}/",
        api_key_env="TEST_KEY",
    )
    return OpenAiCompatibleProvider(profile=profile, api_key="test-key")


def _frame(text: str) -> str:
    import json as _json

    return (
        "data: "
        + _json.dumps({"choices": [{"delta": {"content": text}}]})
        + "\n\n"
    )


def test_provider_streams_tokens_split_across_reads(server) -> None:
    """The stream arrives in 7-byte pieces, so frames span TCP reads."""
    handler = _Handler(frames=[_frame("Standard "), _frame("shipping "), _frame("is 3-5 days."), "data: [DONE]\n\n"])
    base = server(handler)
    provider = _provider(base)

    async def collect():
        return [c.text async for c in provider.stream([], model="deepseek/deepseek-v4-flash")]

    assert asyncio.run(collect()) == ["Standard ", "shipping ", "is 3-5 days."]


def test_provider_sends_the_unprefixed_model_and_temperature(server) -> None:
    handler = _Handler(frames=["data: [DONE]\n\n"])
    base = server(handler)
    provider = _provider(base)

    async def drain():
        async for _ in provider.stream([], model="deepseek/deepseek-v4-flash"):
            pass

    asyncio.run(drain())
    sent = handler.requests[0]
    assert sent["model"] == "deepseek-v4-flash", "the provider must not see our prefix"
    assert sent["stream"] is True
    assert sent["temperature"] <= 0.3, "grounded answers should not be creative"


def test_provider_ignores_comments_and_blank_lines(server) -> None:
    frames = [": keep-alive\n\n", "\n", _frame("hi"), "data: [DONE]\n\n"]
    base = server(_Handler(frames=frames))
    provider = _provider(base)

    async def collect():
        return [c.text async for c in provider.stream([], model="m")]

    assert asyncio.run(collect()) == ["hi"]


def test_provider_surfaces_an_error_delivered_inside_the_stream(server) -> None:
    """A 200 with an error frame is still a failure, not an empty answer."""
    frames = [
        _frame("partial "),
        'data: {"error": {"message": "context length exceeded"}}\n\n',
    ]
    base = server(_Handler(frames=frames))
    provider = _provider(base)

    async def collect():
        out = []
        with pytest.raises(RuntimeError) as exc:
            async for chunk in provider.stream([], model="m"):
                out.append(chunk.text)
        return out, str(exc.value)

    out, message = asyncio.run(collect())
    assert out == ["partial "]
    assert "context length exceeded" in message


def test_provider_raises_on_http_error_status(server) -> None:
    base = server(_Handler(status=429, body={"error": {"message": "rate limited"}}))
    provider = _provider(base)

    async def collect():
        with pytest.raises(RuntimeError) as exc:
            async for _ in provider.stream([], model="m"):
                pass
        return str(exc.value)

    assert "429" in asyncio.run(collect())


def test_provider_complete_returns_text_and_usage(server) -> None:
    body = {
        "model": "deepseek-v4-flash",
        "choices": [{"message": {"content": "3-5 days"}}],
        "usage": {"prompt_tokens": 812, "completion_tokens": 120},
    }
    base = server(_Handler(body=body))
    completion = _provider(base).complete([], model="deepseek/deepseek-v4-flash")
    assert completion.text == "3-5 days"
    assert completion.prompt_tokens == 812
    assert completion.completion_tokens == 120
    assert not completion.failed


def test_provider_complete_reports_an_http_error_as_a_value(server) -> None:
    """A failure must be a value, so the turn loop can degrade mid-stream."""
    base = server(_Handler(status=500, body={"error": {"message": "boom"}}))
    completion = _provider(base).complete([], model="m")
    assert completion.failed
    assert "500" in completion.error


def test_provider_translates_a_connection_failure_into_an_error_value() -> None:
    """An unreachable provider must not raise out of complete()."""
    provider = _provider("http://127.0.0.1:1")
    completion = provider.complete([], model="m")
    assert completion.failed
    assert completion.error


def test_sse_parser_ignores_unparseable_and_irrelevant_frames() -> None:
    from app.conversation.providers import parse_sse_line

    assert parse_sse_line("") is None
    assert parse_sse_line(": comment") is None
    assert parse_sse_line("event: token") is None
    assert parse_sse_line("data: not json") is None
    assert parse_sse_line('data: {"choices": []}') is None
    assert parse_sse_line("data: [DONE]") == ("done", None)
    assert parse_sse_line('data: {"choices":[{"delta":{"role":"assistant"}}]}') is None


def test_provider_falls_back_to_a_non_stream_shape(server) -> None:
    """A provider that ignores `stream: true` still gets read correctly."""
    import json as _json

    frame = "data: " + _json.dumps(
        {"choices": [{"message": {"content": "whole answer"}}]}
    ) + "\n\n"
    base = server(_Handler(frames=[frame, "data: [DONE]\n\n"]))
    provider = _provider(base)

    async def collect():
        return [c.text async for c in provider.stream([], model="m")]

    assert asyncio.run(collect()) == ["whole answer"]


def test_strip_prefix_passes_through_an_unprefixed_model() -> None:
    from app.conversation.providers import PROFILES, strip_prefix

    assert strip_prefix("deepseek-v4-flash", PROFILES["deepseek"]) == "deepseek-v4-flash"
    assert (
        strip_prefix("deepseek/deepseek-reasoner", PROFILES["deepseek"])
        == "deepseek-reasoner"
    )


def test_default_provider_is_unconfigured_without_keys(monkeypatch) -> None:
    from app.conversation import llm

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert llm.default_provider().key == "unconfigured"


def test_default_provider_builds_a_chain_from_configured_keys(monkeypatch) -> None:
    from app.conversation import llm

    monkeypatch.setenv("OLLAMA_API_KEY", "a")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "b")
    provider = llm.default_provider()
    assert provider.key == "fallback-chain"
    assert [p.key for p in provider.providers] == ["ollama_cloud", "deepseek"]


def test_api_key_is_not_leaked_in_a_repr(monkeypatch) -> None:
    """A provider that prints its key ends up in a stack trace and a log line."""
    from app.conversation.providers import OpenAiCompatibleProvider, PROFILES

    provider = OpenAiCompatibleProvider(
        profile=PROFILES["deepseek"], api_key="sk-super-secret-value"
    )
    assert "sk-super-secret-value" not in repr(provider)


# --- end to end: a real provider over a socket through the real turn loop ---


def test_turn_answers_from_a_real_provider_over_http(
    client, tenant, published_bot, server
) -> None:
    """The whole path: SSE route -> turn loop -> HTTP provider -> visitor.

    Unlike the fixture-based tests above, this one exercises the real
    `OpenAiCompatibleProvider` against a real socket, so the request shaping, the
    SSE parse and the turn loop's token handling are all proven together.
    """
    frames = [
        _frame("Standard shipping "),
        _frame("takes 3-5 business days."),
        "data: [DONE]\n\n",
    ]
    handler = _Handler(frames=frames)
    provider = _provider(server(handler))

    conversation_id = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=provider,
    )

    answer = "".join(p["text"] for e, p in events if e == "token")
    assert answer == "Standard shipping takes 3-5 business days."
    final = events[-1][1]
    assert final["degraded"] is False
    assert final["citations"], "grounding survives a real provider"
    assert handler.requests, "the provider must actually have been called"
    # The bot's instructions and the retrieved passages both ride in the payload.
    sent = handler.requests[0]
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][-1]["content"].endswith(
        "How long does standard shipping take?"
    )
