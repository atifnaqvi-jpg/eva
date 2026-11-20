"""OPS-14: the query embed must not block the event loop, and repeats are free.

The defect these tests exist for is not a wrong answer, which is why no test of the
answer could have caught it. It is a *scheduling* defect: ``conversation/service.py``
called the synchronous ``embedder.embed`` from inside an async generator, so one
turn's embed round trip held the event loop for its whole duration and every other
connection in the process waited for it — measured as 21-41 of 50 streams reaching
``final`` and a p90 of 85-194 s (PLAN §8.50).

Two consequences for how these are written:

* every test here has to be able to **fail**, and the ones that matter fail on the
  old code by observing that the loop was blocked or that HTTP was spent twice. A
  test that asserts "a vector came back" passes against the defect.
* nothing here is about vector quality. The provider is deterministic and the
  assertions are on *calls made* and *which thread ran them*, so a change to the
  embedding model cannot make these red for an unrelated reason.
"""

import asyncio
import threading
import time
import uuid

import pytest

from app.retrieval import embedding


class RecordingEmbedder:
    """Deterministic provider that counts calls and can be made slow.

    ``delay`` is what makes "the loop kept running" observable: a blocking call of
    the same duration produces no progress at all, so a heartbeat that advanced is
    evidence the work left the loop rather than a race that happened to win.
    """

    def __init__(
        self,
        *,
        key: str = "recording-3",
        dimension: int = 3,
        delay: float = 0.0,
        explode: bool = False,
    ) -> None:
        self.key = key
        self.dimension = dimension
        self.delay = delay
        self.explode = explode
        self.calls: list[list[str]] = []
        self.threads: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.threads.append(threading.get_ident())
        if self.delay:
            time.sleep(self.delay)
        if self.explode:
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return [
            [float(len(t)), float(sum(map(ord, t)) % 97), 1.0] for t in texts
        ]


@pytest.fixture(autouse=True)
def fresh_cache():
    """Each test starts from an empty cache, so a hit is earned inside the test."""
    embedding.query_cache().clear()
    yield
    embedding.query_cache().clear()


def _run(coro):
    return asyncio.run(coro)


# --- the loop must keep running while the embed is in flight -----------------


def test_a_slow_embed_does_not_freeze_the_event_loop() -> None:
    """The core of OPS-14, stated as the property rather than the mechanism.

    A heartbeat task runs while one query is being embedded. Against a synchronous
    call on the loop thread the heartbeat cannot advance at all until the embed
    returns, so this is red on the old code and cannot be satisfied by a source
    reading. The threshold is generous (one tick of a 10 ms sleeper during a 300 ms
    embed) because the point is "any progress", not a latency figure.
    """
    provider = RecordingEmbedder(delay=0.3)
    ticks = 0

    async def scenario() -> list[float]:
        nonlocal ticks

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            return await embedding.embed_query(
                provider, tenant_id=uuid.uuid4(), text="where is my order"
            )
        finally:
            beat.cancel()

    vector = _run(scenario())

    assert len(vector) == 3
    assert ticks >= 5, (
        f"the event loop only advanced {ticks} times during a 300 ms embed; the call "
        "is still running on the loop thread, which is the defect OPS-14 names"
    )


def test_the_embed_runs_on_a_worker_thread_not_the_loop_thread() -> None:
    """The mechanism the test above measures, pinned separately.

    Separate because they fail for different reasons: a future refactor could keep
    the loop responsive with a different mechanism, but if the call moved *back*
    onto the loop thread this one names it outright instead of only losing ticks.
    """
    provider = RecordingEmbedder()

    async def scenario() -> int:
        loop_thread = threading.get_ident()
        await embedding.embed_query(
            provider, tenant_id=uuid.uuid4(), text="refunds"
        )
        return loop_thread

    loop_thread = _run(scenario())

    assert provider.threads != [loop_thread], (
        "embed ran on the event loop thread; asyncio.to_thread is what keeps a "
        "blocking HTTP client from stalling every other connection"
    )


def test_two_slow_embeds_overlap_instead_of_serialising_on_the_loop() -> None:
    """Concurrency, which is the property 50 simultaneous visitors actually need.

    Two queries with a slow provider must finish in about one delay, not two. A
    single offloaded call would pass the heartbeat test and still serialise every
    turn behind the others if the offload were a lock rather than a thread.
    """
    provider = RecordingEmbedder(delay=0.3)

    async def scenario() -> tuple[list[float], list[float]]:
        return await asyncio.gather(
            embedding.embed_query(provider, tenant_id=uuid.uuid4(), text="one"),
            embedding.embed_query(provider, tenant_id=uuid.uuid4(), text="two"),
        )

    started = time.perf_counter()
    _run(scenario())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.55, (
        f"two 300 ms embeds took {elapsed:.2f} s, so they serialised; concurrent "
        "turns would queue behind each other one layer above the embed service"
    )


# --- the cache ---------------------------------------------------------------


def test_a_repeated_question_costs_no_second_embed() -> None:
    """The row's second half: a repeat question must not buy the same vector twice."""
    provider = RecordingEmbedder()
    tenant = uuid.uuid4()

    first = _run(embedding.embed_query(provider, tenant_id=tenant, text="shipping?"))
    second = _run(embedding.embed_query(provider, tenant_id=tenant, text="shipping?"))

    assert provider.calls == [["shipping?"]], (
        f"the provider was asked {len(provider.calls)} times for one question"
    )
    assert first == second


def test_a_cache_hit_returns_a_copy_so_a_caller_cannot_corrupt_the_cache() -> None:
    """An aliased list would let one turn mutate every later hit's vector."""
    provider = RecordingEmbedder()
    tenant = uuid.uuid4()

    first = _run(embedding.embed_query(provider, tenant_id=tenant, text="warranty"))
    first[0] = 999.0
    second = _run(embedding.embed_query(provider, tenant_id=tenant, text="warranty"))

    assert second[0] != 999.0, "the cache handed out its own list"


def test_the_cache_is_tenant_scoped_even_though_the_vector_is_not() -> None:
    """A shared key would make a hit disclose that another tenant asked this.

    The vector is a pure function of (model, text), so scoping costs one embed per
    tenant for a shared question. That is the safe direction: a support question can
    itself be sensitive, and this is the only thing that stops one being inferred.
    """
    provider = RecordingEmbedder()
    _run(embedding.embed_query(provider, tenant_id=uuid.uuid4(), text="my invoice"))
    _run(embedding.embed_query(provider, tenant_id=uuid.uuid4(), text="my invoice"))

    assert len(provider.calls) == 2, "two tenants shared one tenant's cache entry"


def test_a_different_model_key_is_a_miss_because_it_is_a_different_vector_space() -> None:
    """§16.1: a model change is a new vector space, so a cross-model hit is garbage.

    ``RemoteEmbedder`` refuses vectors from the wrong model for the same reason; a
    cache that ignored the key would reintroduce the mismatch one layer up, where it
    would be invisible because the vector is well-formed and simply meaningless.
    """
    tenant = uuid.uuid4()
    first = RecordingEmbedder(key="model-a")
    second = RecordingEmbedder(key="model-b", dimension=3)

    _run(embedding.embed_query(first, tenant_id=tenant, text="returns"))
    _run(embedding.embed_query(second, tenant_id=tenant, text="returns"))

    assert len(second.calls) == 1, "a hit crossed two model keys"


def test_a_different_dimension_is_a_miss_too() -> None:
    """Key *and* dimension, because two models can share a name across deployments."""
    tenant = uuid.uuid4()
    first = RecordingEmbedder(dimension=3)
    second = RecordingEmbedder(dimension=4)

    _run(embedding.embed_query(first, tenant_id=tenant, text="returns"))
    _run(embedding.embed_query(second, tenant_id=tenant, text="returns"))

    assert len(second.calls) == 1


def test_a_failure_is_not_cached() -> None:
    """An outage must not be remembered as an answer.

    If the raise were cached, one transient embed failure would make every later
    turn for that tenant degrade — a permanent outage created by a temporary one.
    """
    tenant = uuid.uuid4()
    broken = RecordingEmbedder(explode=True)
    with pytest.raises(ModuleNotFoundError):
        _run(embedding.embed_query(broken, tenant_id=tenant, text="shipping?"))

    working = RecordingEmbedder()
    vector = _run(embedding.embed_query(working, tenant_id=tenant, text="shipping?"))

    assert len(working.calls) == 1, "the failure was cached and served as a hit"
    assert len(vector) == 3


def test_the_cache_is_bounded_and_evicts_the_oldest_entry() -> None:
    """Bounded memory: support traffic is unbounded, so the cache cannot be."""
    cache = embedding.QueryEmbeddingCache(max_entries=2)
    keys = [
        cache.key("t", "m", 3, text) for text in ("one", "two", "three")
    ]
    for i, key in enumerate(keys):
        cache.put(key, [float(i)])

    assert len(cache) == 2
    assert cache.get(keys[0]) is None, "the oldest entry was not the one evicted"
    assert cache.get(keys[1]) == [1.0]
    assert cache.get(keys[2]) == [2.0]


def test_the_hit_and_miss_counters_report_what_happened() -> None:
    """So an operator can tell "the cache is working" from "the cache is never hit".

    Not decoration: a cache that is never hit is indistinguishable from no cache at
    all on every metric this row produces, which is the §8.38 trap in a third place.
    """
    provider = RecordingEmbedder()
    tenant = uuid.uuid4()
    cache = embedding.query_cache()

    _run(embedding.embed_query(provider, tenant_id=tenant, text="orders"))
    _run(embedding.embed_query(provider, tenant_id=tenant, text="orders"))

    assert cache.hits == 1
    assert cache.misses == 1
