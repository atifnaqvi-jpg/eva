"""OPS-10: the eval gate, wired into CI.

Two kinds of test live here:

* ``test_golden_set_passes_the_gate`` — the real pipeline over the synthetic golden
  corpus. This is the blocking gate (G6).
* ``test_gate_blocks_a_seeded_regression`` — proves the gate can fail. A gate that
  has never failed is not a gate, so this injects a ranking regression and asserts
  the gate rejects it, then confirms the clean run still passes.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

EVAL_DIR = Path(__file__).resolve().parent / "eval"
sys.path.insert(0, str(EVAL_DIR))

import harness  # noqa: E402
import run_eval  # noqa: E402
from conftest import _admin_dsn  # noqa: E402

from app.db.session import make_session_factory  # noqa: E402
from app.config import TENANT_SETTING  # noqa: E402


@pytest.fixture
def session(migrated_db: str) -> Iterator[Session]:
    """App-role session whose tenant GUC survives commits.

    ``make_session_factory`` is the production factory; using it here means the
    eval runs under the same RLS behaviour as the API rather than a looser setup.
    """
    engine = create_engine(
        "postgresql+psycopg://eva_app:appsecret@127.0.0.1:5432/" + migrated_db
    )
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture(scope="module")
def golden() -> harness.GoldenSet:
    return harness.GoldenSet.load()


def test_golden_set_is_well_formed(golden: harness.GoldenSet) -> None:
    assert 50 <= len(golden.cases) <= 100, "21.2 specifies 50-100 pairs"
    assert any(c.answerable for c in golden.cases)
    assert any(not c.answerable for c in golden.cases), (
        "without unanswerable cases the refusal metric is undefined"
    )


def test_golden_set_passes_the_gate(
    migrated_db: str, session: Session, golden: harness.GoldenSet
) -> None:
    seeded = run_eval.seed(session, golden, admin_dsn=_admin_dsn(migrated_db))
    try:
        results, metrics = run_eval.run(session, golden, seeded)
    finally:
        run_eval.cleanup(session, seeded, admin_dsn=_admin_dsn(migrated_db))

    passed, reasons = harness.gate(metrics, baseline=harness.load_baseline())
    report = harness.summarize(metrics, passed, reasons)
    print(report)
    assert passed, report
    # Individual failures are expected under the non-semantic CI embedder; the
    # gate is about the aggregates. A wholesale collapse (every case failing) is
    # still caught, because that drives the metrics under their floors.
    assert len(metrics.failures) < len(golden.cases) / 2, report


def test_gate_blocks_a_seeded_regression(golden: harness.GoldenSet) -> None:
    """A deliberately broken ranker must be rejected by the same gate.

    The regression here is realistic: returning candidates in insertion order
    instead of relevance, which is what a broken sort or a dropped ORDER BY does.
    """
    all_slugs = [c["id"] for d in golden.documents for c in d["chunks"]]
    broken = {case.id: (all_slugs, False) for case in golden.cases}

    metrics = harness.score(golden.cases, broken)
    passed, reasons = harness.gate(metrics, baseline=harness.load_baseline())

    assert not passed, "insertion-order ranking must not pass the gate"
    assert metrics.mrr < harness.MIN_MRR
    assert any("mrr" in r for r in reasons)


def test_gate_blocks_a_regression_in_refusals_only(golden: harness.GoldenSet) -> None:
    """Answering everything must fail even if precision is untouched."""
    perfect = {
        case.id: (list(case.expected_chunk_ids) or all_slugs(golden), False)
        for case in golden.cases
    }
    metrics = harness.score(golden.cases, perfect)
    passed, reasons = harness.gate(metrics, baseline=harness.load_baseline())

    assert not passed, "answering unanswerable questions must fail the gate"
    assert metrics.correct_refusal_rate == 0.0
    assert any("correct_refusal_rate" in r for r in reasons)


def test_gate_respects_tolerance_against_a_recorded_baseline(golden: harness.GoldenSet) -> None:
    clean = {
        case.id: (
            list(case.expected_chunk_ids) if case.answerable else [],
            not case.answerable,
        )
        for case in golden.cases
    }
    metrics = harness.score(golden.cases, clean)
    assert metrics.mrr == 1.0 and metrics.correct_refusal_rate == 1.0
    assert metrics.recall_at_k == 1.0 and metrics.answer_rate == 1.0

    # A baseline equal to the current values passes.
    passes, reasons = harness.gate(metrics, baseline=metrics.as_dict(), tolerance=0.05)
    assert passes, reasons

    # A degraded run against that same baseline fails: drop one answerable case's
    # ranking to rank 3, which lowers MRR by more than the tolerance.
    degraded = dict(clean)
    first_answerable = next(c for c in golden.cases if c.answerable)
    expected = list(first_answerable.expected_chunk_ids)
    filler = [s for s in all_slugs(golden) if s not in expected][:2]
    degraded[first_answerable.id] = (filler + expected + filler, False)
    weaker = harness.score(golden.cases, degraded)
    assert weaker.mrr < 1.0

    passed_degraded, reasons = harness.gate(
        weaker, baseline=metrics.as_dict(), tolerance=0.01
    )
    assert not passed_degraded, "a real MRR drop must be rejected against the baseline"
    assert any("mrr" in r for r in reasons)


def all_slugs(golden: harness.GoldenSet) -> list[str]:
    return [c["id"] for d in golden.documents for c in d["chunks"]]


def test_eval_records_a_real_baseline(
    migrated_db: str, session: Session, golden: harness.GoldenSet
) -> None:
    """The recorded baseline must match what the pipeline actually produces.

    If this drifts, the tolerance gate protects a stale number and stops meaning
    anything — so drift is a failure with instructions rather than a silent pass.
    """
    seeded = run_eval.seed(session, golden, admin_dsn=_admin_dsn(migrated_db))
    try:
        _, metrics = run_eval.run(session, golden, seeded)
    finally:
        run_eval.cleanup(session, seeded, admin_dsn=_admin_dsn(migrated_db))

    recorded = harness.load_baseline()
    assert recorded, (
        "tests/eval/baseline.json is missing; record it with the measured values "
        f"{metrics.as_dict()}"
    )
    current = metrics.as_dict()
    for key, value in recorded.items():
        assert abs(current[key] - value) <= 0.02, (
            f"baseline {key}={value} drifted from measured {current[key]}; "
            "re-record it deliberately if the change is intended"
        )
