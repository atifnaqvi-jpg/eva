"""OPS-12: a per-turn provider outcome, and a usage ledger that is not zero.

ARCHITECTURE §22 asks for *"LLM latency/error by provider"* and OPS-04's alert list
includes a provider error-rate spike. Neither could be built: **no provider outcome was
persisted anywhere** — a failure was logged to stdout and the degraded path returned, so
the rate had neither a numerator nor a provider dimension. Separately the ledger was
*structurally* zero: `record_usage` computed `units = (prompt_tokens or 0) +
(completion_tokens or 0)` and its only caller passed `None`/`None`, so `sum(units)` was
0 across every row, `check_budget` could never trip, and `/analytics/summary` reported
zero tokens (measured in PLAN §8.33: 22 rows, `sum(units)=0`, zero rows above).

Three claims are defended here, and each one has a way to be false that a happy-path
test would miss:

* **The outcome row exists on both branches.** A success *and* a total failure are
  asserted, because the failure is the one an alert reads and the one that used to
  leave no trace.
* **The counts are real, not the estimate.** A provider that reports `usage` must have
  its numbers stored verbatim — and the request must ask for them, or an
  OpenAI-compatible stream never sends a usage frame at all.
* **Nothing but a type name is persisted on the error path.** A provider message is
  built from up to 300 characters of the response body and this table is read by a
  poller, not an incident reader, so a canary in the message must not reach the row.
"""

import os
import uuid

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import asyncio

import psycopg
import pytest
from conftest import _admin_dsn, app_dsn

from app.conversation import controls
from app.conversation.llm import CompletionChunk

from test_conversation import (  # noqa: F401 - fixtures used by pytest
    FakeProvider,
    _run_turn,
    _start,
    client,
    published_bot,
    stub_embedder,
    tenant,
)
from test_provider_resilience import BrokenProvider

CANARY = "CANARY-question-text-must-not-be-persisted"


class ReportingProvider(FakeProvider):
    """Streams an answer and then the trailing usage frame, like a real provider."""

    key = "reporting"

    def __init__(self, reply: str, *, prompt_tokens: int, completion_tokens: int):
        super().__init__(reply=reply)
        self.usage = (prompt_tokens, completion_tokens)

    async def _chunks(self):
        for word in self.reply.split(" "):
            yield CompletionChunk(text=word + " ")
        yield CompletionChunk(
            text="",
            prompt_tokens=self.usage[0],
            completion_tokens=self.usage[1],
        )


class LoudBrokenProvider(BrokenProvider):
    """Fails with a message that carries content-like text, to be sure it is dropped."""

    def __init__(self, key: str = "loud") -> None:
        super().__init__(key=key)
        self.key = key

    def stream(self, messages, *, model, max_tokens=1024):
        self.requests.append(list(messages))

        async def gen():
            raise RuntimeError(f"upstream said: {CANARY}")
            yield  # pragma: no cover - unreachable

        return gen()


def _rows(migrated_db: str, tenant_id: str) -> list[dict]:
    """Read the outcome rows as the owner, so RLS cannot hide a bug."""
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT provider, model, outcome, error_class, conversation_id"
            " FROM provider_outcomes WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
    return [
        {
            "provider": r[0], "model": r[1], "outcome": r[2],
            "error_class": r[3], "conversation_id": str(r[4]),
        }
        for r in rows
    ]


# --- the outcome row exists on both branches -------------------------------


def test_a_grounded_answer_records_a_success_outcome(
    client, tenant, published_bot, migrated_db
) -> None:
    """The denominator of OPS-04's error rate. Without it the alert has no ratio."""
    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=FakeProvider(reply="3-5 days."),
    )

    rows = _rows(migrated_db, tenant["tenant_id"])
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "success"
    assert rows[0]["error_class"] is None
    assert rows[0]["conversation_id"] == conversation_id
    assert rows[0]["provider"] == "fake"


def test_a_total_provider_failure_records_a_failure_outcome(
    client, tenant, published_bot, migrated_db
) -> None:
    """The numerator — and the branch that previously left no durable trace at all."""
    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=LoudBrokenProvider("dead"),
    )

    rows = _rows(migrated_db, tenant["tenant_id"])
    assert len(rows) == 1, rows
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["error_class"] == "RuntimeError"
    assert rows[0]["conversation_id"] == conversation_id


def test_the_failure_row_carries_the_type_name_and_not_the_message(
    client, tenant, published_bot, migrated_db
) -> None:
    """A provider message can echo the prompt; this table is read by a poller.

    Same rule OPS-02 applies to spans (``error_event`` records
    ``type(exc).__name__``), for the same reason: the value leaves the box's
    debugging context and lands in a table nobody reads for judgement.
    """
    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=LoudBrokenProvider("dead"),
    )

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        blob = conn.execute(
            "SELECT coalesce(string_agg("
            "  coalesce(error_class,'') || coalesce(provider,'') || coalesce(model,''),"
            "  '|'), '') FROM provider_outcomes WHERE tenant_id = %s",
            (tenant["tenant_id"],),
        ).fetchone()[0]
    assert blob, "nothing was written, so this proves nothing"
    assert CANARY not in blob


def test_a_fallback_answering_provider_is_the_one_named(
    client, tenant, published_bot, migrated_db
) -> None:
    """Attribution must be the provider that served, not the chain that wrapped it.

    The chain is itself an ``LlmProvider`` whose ``key`` is ``fallback-chain``; naming
    it would make every row's provider dimension the same string and the per-provider
    error rate meaningless.
    """
    from app.conversation.resilience import FallbackChain

    conversation_id = _start(client, published_bot["bot_id"])
    chain = FallbackChain(
        providers=[
            BrokenProvider("primary"),
            FakeProvider("Standard shipping is 3-5 days."),
        ]
    )
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=chain,
    )

    rows = _rows(migrated_db, tenant["tenant_id"])
    assert [r["outcome"] for r in rows] == ["success"], rows
    assert rows[0]["provider"] == "fake"
    assert rows[0]["provider"] != "fallback-chain"


# --- the ledger is no longer structurally zero -----------------------------


def _usage_row(migrated_db: str, tenant_id: str) -> tuple:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        return conn.execute(
            "SELECT provider, model, units FROM usage_records"
            " WHERE tenant_id = %s AND kind = 'chat'",
            (tenant_id,),
        ).fetchone()


def test_reported_token_counts_reach_the_ledger_and_the_message(
    client, tenant, published_bot, migrated_db
) -> None:
    """The provider's own numbers, stored verbatim, not the local estimate.

    812/120 are chosen because the estimator cannot coincidentally produce them: the
    prompt is a handful of words, so an estimate that leaked through would be small
    and different.
    """
    conversation_id = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=ReportingProvider(
            "3-5 days.", prompt_tokens=812, completion_tokens=120
        ),
    )

    provider, model, units = _usage_row(migrated_db, tenant["tenant_id"])
    assert units == 932, units
    assert provider == "reporting"
    assert model

    final = events[-1][1]
    assert final["usage"] == {
        "prompt_tokens": 812, "completion_tokens": 120, "reported": True,
    }

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        stored = conn.execute(
            "SELECT prompt_tokens, completion_tokens FROM messages"
            " WHERE conversation_id = %s AND role = 'assistant'",
            (conversation_id,),
        ).fetchone()
    assert stored == (812, 120)


def test_a_provider_that_reports_nothing_still_produces_a_nonzero_ledger(
    client, tenant, published_bot, migrated_db
) -> None:
    """The old behaviour in one line: ``FakeProvider`` sends no usage frame.

    An estimate is a different claim from a measurement, so the `final` event says
    which one it is rather than letting a consumer chart them as the same thing.
    """
    conversation_id = _start(client, published_bot["bot_id"])
    events = _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=FakeProvider(reply="3-5 days."),
    )

    _provider, _model, units = _usage_row(migrated_db, tenant["tenant_id"])
    assert units > 0, "the ledger is structurally zero again"

    final = events[-1][1]
    assert final["usage"]["reported"] is False
    assert final["usage"]["prompt_tokens"] > 0
    assert final["usage"]["completion_tokens"] > 0


def test_the_recorded_units_trip_the_budget_stop(
    client, tenant, published_bot, migrated_db
) -> None:
    """CONV-14 was unreachable because the ledger was always 0, not because it works.

    The budget is lowered to just under the spend of one turn; the second turn must be
    refused. Before this row, the same setup spent nothing and the stop never fired.
    """
    tenant_id = tenant["tenant_id"]
    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=ReportingProvider(
            "3-5 days.", prompt_tokens=500, completion_tokens=100
        ),
    )

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute(
            "UPDATE tenants SET settings = coalesce(settings, '{}'::jsonb)"
            " || '{\"monthly_token_budget\": 400}'::jsonb WHERE id = %s",
            (tenant_id,),
        )
        conn.commit()

    try:
        second = _run_turn(
            client,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            question="And expedited?",
            provider=FakeProvider(reply="1-2 days."),
        )
    finally:
        with psycopg.connect(_admin_dsn(migrated_db)) as conn:
            conn.execute(
                "UPDATE tenants SET settings = settings - 'monthly_token_budget'"
                " WHERE id = %s",
                (tenant_id,),
            )
            conn.commit()

    assert second[0][0] == "error", second
    assert second[0][1]["code"] == "budget_exceeded"


# --- the request asks for the counts --------------------------------------


def test_a_streaming_request_asks_the_provider_for_usage() -> None:
    """Without this an OpenAI-compatible stream never sends a usage frame.

    Measured against Ollama Cloud: the option is accepted and the stream ends with a
    `choices: []` frame carrying `usage`. The assertion is on the payload because the
    absence is silent — the answer still arrives, just without counts.
    """
    from app.conversation.providers import _payload

    body = _payload([], model="m", max_tokens=16, stream=True)
    assert body["stream_options"] == {"include_usage": True}

    non_streaming = _payload([], model="m", max_tokens=16, stream=False)
    assert "stream_options" not in non_streaming, (
        "a non-streaming request already carries usage; the option is stream-only"
    )


def test_the_usage_frame_is_classified_before_the_empty_choices_early_return() -> None:
    """The frame has ``choices: []``, so an early return on empty choices eats it."""
    from app.conversation.providers import parse_sse_line

    frame = (
        'data: {"choices": [], "usage": {"prompt_tokens": 32, "completion_tokens": 8}}'
    )
    assert parse_sse_line(frame) == ("usage", (32, 8))

    # A keep-alive with no usage on it is still nothing, not a zero-value frame.
    assert parse_sse_line('data: {"choices": []}') is None


def test_a_frame_carrying_both_text_and_usage_yields_the_text() -> None:
    """Providers differ on which frame closes the stream; neither shape may lose a token."""
    from app.conversation.providers import parse_sse_line

    frame = (
        'data: {"choices": [{"delta": {"content": "end"}}],'
        ' "usage": {"prompt_tokens": 1, "completion_tokens": 1}}'
    )
    assert parse_sse_line(frame) == ("delta", "end")


# --- the table's own guarantees --------------------------------------------


def test_the_outcome_column_accepts_only_the_two_values(
    client, migrated_db: str, tenant, published_bot
) -> None:
    """A CHECK constraint is the guarantee; the Python guard is the good error message."""
    conversation_id = _start(client, published_bot["bot_id"])
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO provider_outcomes"
                " (tenant_id, conversation_id, outcome) VALUES (%s,%s,'maybe')",
                (tenant["tenant_id"], conversation_id),
            )


def test_the_python_guard_rejects_an_unknown_outcome_before_the_constraint() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")
    session = Session(engine)  # never used: the guard runs before any SQL
    with pytest.raises(ValueError):
        controls.record_provider_outcome(
            session,
            tenant_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            provider="p",
            model="m",
            outcome="maybe",
        )


def test_outcome_rows_cascade_away_with_their_conversation(
    client, tenant, published_bot, migrated_db
) -> None:
    """Telemetry about a call, not billing: an erasure must take it.

    ``usage_records`` is deliberately the opposite (ON DELETE SET NULL) because token
    totals are the tenant's accounting. This table names the conversation, so it goes.
    """
    conversation_id = _start(client, published_bot["bot_id"])
    _run_turn(
        client,
        tenant_id=tenant["tenant_id"],
        conversation_id=conversation_id,
        question="How long does standard shipping take?",
        provider=FakeProvider(reply="3-5 days."),
    )
    assert len(_rows(migrated_db, tenant["tenant_id"])) == 1

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute(
            "DELETE FROM conversations WHERE id = %s", (conversation_id,)
        )
        conn.commit()

    assert _rows(migrated_db, tenant["tenant_id"]) == []


def test_the_table_is_tenant_isolated_by_rls(migrated_db: str, tenant_ids) -> None:
    """The policy is the guard, and it is asserted on its own.

    A lost policy makes a query *succeed and return too much* — a failure mode that
    looks like every other green test, so it gets its own assertion rather than
    riding on an application-level predicate.
    """
    tenant_a, tenant_b = tenant_ids
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conversation = conn.execute(
            "INSERT INTO conversations (tenant_id, bot_id, visitor_session_id)"
            " SELECT %s, id, 'vs-rls' FROM bots WHERE tenant_id = %s RETURNING id",
            (str(tenant_a), str(tenant_a)),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO provider_outcomes"
            " (tenant_id, conversation_id, provider, model, outcome)"
            " VALUES (%s,%s,'p','m','success')",
            (str(tenant_a), str(conversation)),
        )
        conn.commit()

    with psycopg.connect(app_dsn(migrated_db)) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_b),))
        assert conn.execute(
            "SELECT count(*) FROM provider_outcomes"
        ).fetchone()[0] == 0, "tenant B can read tenant A's provider outcomes"

        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_a),))
        assert conn.execute(
            "SELECT count(*) FROM provider_outcomes"
        ).fetchone()[0] == 1, "tenant A cannot read its own provider outcome"


def test_the_stream_survives_a_provider_that_sends_only_usage() -> None:
    """An empty answer is a failure even when counts arrive; it must not be a success."""
    from app.conversation.resilience import FallbackChain

    class UsageOnlyProvider:
        key = "usage-only"

        def stream(self, messages, *, model, max_tokens=1024):
            async def gen():
                yield CompletionChunk(text="", prompt_tokens=5, completion_tokens=0)

            return gen()

        def complete(self, messages, *, model, max_tokens=1024):
            raise AssertionError("not used")

    chain = FallbackChain(
        providers=[UsageOnlyProvider(), FakeProvider("3-5 days.")]
    )

    async def collect():
        return [c.text async for c in chain.stream([], model="m") if c.text]

    assert asyncio.run(collect()) == ["3-5 ", "days. "], (
        "the usage frame must not count as 'yielded', or the fallback is skipped"
    )
