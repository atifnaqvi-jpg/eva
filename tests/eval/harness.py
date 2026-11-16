"""Eval harness over a synthetic golden set (OPS-10, ARCHITECTURE 21.2).

What this measures, and what it deliberately does not:

* **Retrieval precision/recall@k** — measured exactly, because it depends only on
  ordering, and ordering is deterministic under the hashing embedder.
* **Correct-refusal rate** — measured exactly: an unanswerable question must fall
  below the floor.
* **Groundedness** is *not* scored here. It needs an LLM judge, which needs a
  provider (WP-13) and an approved key. Scoring it with a substring heuristic would
  produce a number that looks like a quality metric and is not one, which is worse
  than leaving it to WP-11 where an answer actually exists.

The gate is a tolerance, per G6: a run fails when a metric drops more than
``tolerance`` below its recorded baseline, or below an absolute floor. Baselines
live in ``baseline.json`` next to this file and are updated deliberately, so a
regression has to be *accepted* rather than silently absorbed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN_PATH = HERE / "golden_v1.json"
BASELINE_PATH = HERE / "baseline.json"

# G6: a drop beyond tolerance blocks merge. Absolute floors catch the case where
# the baseline itself was recorded from a broken run.
# Tolerance against the recorded baseline is the primary mechanism (G6). The
# absolute floors below are deliberately loose: they exist to catch a *broken*
# run (an all-zero metric, a deleted guard), not to certify quality. CI runs the
# non-semantic hashing embedder, so its ceiling is well below BGE-M3's; the floors
# are set to what that embedder can actually reach, and the regression detection
# comes from the baseline comparison. Real-embedder numbers are recorded in
# PLAN 8.8.
DEFAULT_TOLERANCE = 0.05
MIN_RECALL_AT_5 = 0.70
MIN_MRR = 0.55
MIN_CORRECT_REFUSAL = 0.50
MIN_ANSWER_RATE = 0.80


@dataclass(frozen=True)
class GoldenCase:
    id: str
    question: str
    expected_chunk_ids: tuple[str, ...]
    answerable: bool


@dataclass(frozen=True)
class GoldenSet:
    tenant_name: str
    documents: list[dict]
    cases: list[GoldenCase]

    @classmethod
    def load(cls, path: Path = GOLDEN_PATH) -> GoldenSet:
        raw = json.loads(path.read_text())
        cases = [
            GoldenCase(
                id=c["id"],
                question=c["question"],
                expected_chunk_ids=tuple(c["expected_chunk_ids"]),
                answerable=bool(c["answerable"]),
            )
            for c in raw["cases"]
        ]
        _validate(cases, raw["documents"])
        return cls(raw["tenant_name"], raw["documents"], cases)


def _validate(cases: list[GoldenCase], documents: list[dict]) -> None:
    """Fail loudly on a malformed set: a silently-broken golden set scores 0."""
    known = {chunk["id"] for doc in documents for chunk in doc["chunks"]}
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id}")
        seen.add(case.id)
        unknown = set(case.expected_chunk_ids) - known
        if unknown:
            raise ValueError(f"{case.id} expects unknown chunks {sorted(unknown)}")
        if case.answerable and not case.expected_chunk_ids:
            raise ValueError(f"{case.id} is answerable but names no chunk")
        if not case.answerable and case.expected_chunk_ids:
            raise ValueError(f"{case.id} is unanswerable but names chunks")


@dataclass
class Metrics:
    """Four numbers, each of which can actually fail on its own.

    Precision@5 is deliberately *not* here. The golden set has one expected chunk
    per question, so precision@5 is capped at 0.2 no matter how good retrieval is;
    a floor on it measures the dataset, not the system. ``mrr`` captures ranking
    quality on the same cases without that ceiling.

    ``correct_refusal_rate`` alone is also insufficient — refusing every question
    scores 1.0 on it — so ``answer_rate`` is reported alongside it and gated. The
    pair can only both be satisfied by a system that actually discriminates.
    """

    recall_at_k: float = 0.0
    mrr: float = 0.0
    correct_refusal_rate: float = 0.0
    answer_rate: float = 0.0
    answerable: int = 0
    unanswerable: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float]:
        return {
            "recall_at_k": round(self.recall_at_k, 4),
            "mrr": round(self.mrr, 4),
            "correct_refusal_rate": round(self.correct_refusal_rate, 4),
            "answer_rate": round(self.answer_rate, 4),
        }


def score(
    cases: list[GoldenCase],
    results: dict[str, tuple[list[str], bool]],
    *,
    k: int = 5,
) -> Metrics:
    """Score pre-computed per-case results.

    ``results`` maps case id to (ranked chunk slugs, declined). Keeping scoring
    separate from retrieval is what lets the regression test inject a deliberately
    broken ranking without a database.
    """
    metrics = Metrics()
    recall_hits = 0
    recall_total = 0
    reciprocal_ranks: list[float] = []
    answered = 0
    refused = 0

    for case in cases:
        ranked, declined = results[case.id]
        top = ranked[:k]
        if case.answerable:
            metrics.answerable += 1
            expected = set(case.expected_chunk_ids)
            recall_total += len(expected)
            if declined:
                metrics.failures.append(f"{case.id}: declined an answerable question")
                reciprocal_ranks.append(0.0)
                continue
            answered += 1
            recall_hits += len(expected & set(top))
            rank = next(
                (n for n, slug in enumerate(ranked, start=1) if slug in expected), None
            )
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            if rank is None:
                metrics.failures.append(
                    f"{case.id}: none of {sorted(expected)} retrieved"
                )
            elif rank > k:
                metrics.failures.append(f"{case.id}: correct chunk only at rank {rank}")
        else:
            metrics.unanswerable += 1
            if declined:
                refused += 1
            else:
                metrics.failures.append(
                    f"{case.id}: answered an unanswerable question via {top[:1]}"
                )

    metrics.recall_at_k = recall_hits / recall_total if recall_total else 0.0
    metrics.mrr = (
        sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    )
    metrics.correct_refusal_rate = (
        refused / metrics.unanswerable if metrics.unanswerable else 0.0
    )
    metrics.answer_rate = answered / metrics.answerable if metrics.answerable else 0.0
    return metrics


METRIC_NAMES = ("recall_at_k", "mrr", "correct_refusal_rate", "answer_rate")


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, float]:
    """Only the numeric metrics; the file also carries provenance keys."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: float(raw[k]) for k in METRIC_NAMES if isinstance(raw.get(k), (int, float))}


def gate(
    metrics: Metrics,
    *,
    baseline: dict[str, float] | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[bool, list[str]]:
    """Return (passed, reasons). Reasons are empty when it passes."""
    baseline = baseline or {}
    reasons: list[str] = []

    floors = {
        "recall_at_k": MIN_RECALL_AT_5,
        "mrr": MIN_MRR,
        "correct_refusal_rate": MIN_CORRECT_REFUSAL,
        "answer_rate": MIN_ANSWER_RATE,
    }
    current = metrics.as_dict()
    for name, floor in floors.items():
        value = current[name]
        if value < floor:
            reasons.append(f"{name}={value:.3f} below absolute floor {floor:.3f}")
        recorded = baseline.get(name)
        if recorded is not None and value < recorded - tolerance:
            reasons.append(
                f"{name}={value:.3f} dropped {recorded - value:.3f} from baseline "
                f"{recorded:.3f} (tolerance {tolerance:.2f})"
            )
    return (not reasons), reasons


def summarize(metrics: Metrics, passed: bool, reasons: list[str]) -> str:
    lines = [
        "eval: golden_v1",
        f"  recall@5={metrics.recall_at_k:.3f} mrr={metrics.mrr:.3f} "
        f"refused-unanswerable={metrics.correct_refusal_rate:.3f} "
        f"answered-answerable={metrics.answer_rate:.3f}",
        f"  cases: {metrics.answerable} answerable, {metrics.unanswerable} unanswerable",
        f"  gate: {'PASS' if passed else 'FAIL'}",
    ]
    lines.extend(f"  ! {r}" for r in reasons)
    lines.extend(f"  - {f}" for f in metrics.failures[:10])
    if len(metrics.failures) > 10:
        lines.append(f"  - ... {len(metrics.failures) - 10} more")
    return "\n".join(lines)
