"""ADMN-13: walk publish / preview / rollback in a browser with two documents live.

PLAN §7 M5 and ARCHITECTURE §18.2 ("staged changes -> test questions side-by-side
(draft vs live) -> publish with one click -> rollback available"), against the
knowledge-set model RAG-11 shipped (`docs/PLAN.md` §8.40).

The clause this row exists for is the one the in-process suite cannot see. RAG-11's
tests assert retrieval over a set; they cannot see the screen an owner uses, and
ADMN-05's own Chromium pass was recorded against the single-pointer model it
superseded. So the walk here is a real browser against a real stack, and every
assertion is read off the rendered page:

* **nothing published** is a real starting state (the live column says so)
* **one document published** answers, and the other still does not — this is the
  control that keeps "both live" from being a tautology
* **two documents published** both answer from the live set, which is the §8.22
  defect reversed
* **the served column survives a reload**, so it comes from the API and not from the
  click's local state
* **rollback** re-points to one document and the served set says so

The screen has no rollback control yet, and adding one would be a new screen control
(a scope change), so rollback is driven through the same endpoint publish uses from
inside the authenticated browser session, and its *effect* is then read off the
screen. That limit is stated here rather than glossed over in the registry.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/admn13_walk.py

Exits non-zero if any clause stops holding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "backend" / "api" / "widget"
DRIVER = ROOT / "scripts" / "admn13_driver.mjs"

BASE = os.environ.get("ADMN13_BASE", "http://localhost")

QUESTION_A = "How long does standard shipping take?"
ANSWER_A = "Standard shipping takes 3-5 business days."
QUESTION_B = "Do you ship to the EU?"
ANSWER_B = "Yes, we ship to the EU with a 7-day delivery window."


def _norm(value: object) -> str:
    """Meaning, not code points — the preview prints the chunk exactly as stored."""
    return "".join(ch if str(ch).isalnum() else " " for ch in str(value or "").lower())


def _finds(column: str | None, answer: str) -> bool:
    return _norm(answer) in _norm(column)


def _served(report: dict, key: str) -> int:
    return int((report.get("served_counts") or {}).get(key, -1))


def clauses(report: dict) -> list[tuple[str, bool]]:
    """Every claim this row makes, evaluated against one driver report.

    Kept as a pure function over the report so each clause can be shown to fail:
    ``tests/test_admn13_walk.py`` flips one field at a time and asserts the matching
    clause goes red. A gate whose verdict is computed inline cannot be checked that
    way, and §8.25/§8.32 both record what an unfalsifiable gate costs.
    """
    draft_only = report.get("probe_draft_only") or {}
    a_only = report.get("probe_a_only") or {}
    b_after_a = report.get("probe_b_after_a") or {}
    a_after_b = report.get("probe_a_after_b") or {}
    b_after_b = report.get("probe_b_after_b") or {}
    a_after_rollback = report.get("probe_a_after_rollback") or {}
    b_after_rollback = report.get("probe_b_after_rollback") or {}
    notices = [
        (report.get("publish_1") or {}).get("notice") or "",
        (report.get("publish_2") or {}).get("notice") or "",
    ]

    return [
        (
            "two documents were added and both are listed",
            len(report.get("rows_before_publish") or []) >= 2,
        ),
        (
            "nothing is published at the start, and the screen says so",
            _finds(draft_only.get("draft"), ANSWER_B)
            and "nothing is published" in (draft_only.get("live") or "").lower(),
        ),
        (
            # The discriminating clause: with one document published, the draft knows
            # both and only the live column is narrower. If the live column were simply
            # the draft, this is the assertion that goes red — "both live" below would
            # still pass, which is exactly the trap a scope-blind check falls into.
            "publishing the first document serves it and not the other",
            _served(report, "before_publish") == 0
            and _finds(a_only.get("draft"), ANSWER_B)
            and _finds(a_only.get("live"), ANSWER_A)
            and not _finds(b_after_a.get("live"), ANSWER_B),
        ),
        (
            "publishing a second document adds it to the served set",
            _finds(a_after_b.get("live"), ANSWER_A)
            and _finds(b_after_b.get("live"), ANSWER_B),
        ),
        (
            "both documents are marked live in the table after a reload",
            _served(report, "after_publish_2") == 2,
        ),
        (
            "the publish notice reports how many documents are served",
            any("2 documents" in notice for notice in notices),
        ),
        (
            "rollback re-points the served set to one document",
            (report.get("rollback") or {}).get("status") == 200
            and _served(report, "after_rollback") == 1
            and _finds(a_after_rollback.get("live"), ANSWER_A)
            and not _finds(b_after_rollback.get("live"), ANSWER_B),
        ),
    ]


def drive() -> dict:
    if not (WIDGET / "node_modules" / "playwright").is_dir():
        raise SystemExit(
            f"playwright is not installed under {WIDGET}/node_modules.\n"
            "Run `npm ci && npx playwright install chromium` there first."
        )
    env = {
        **os.environ,
        "ADMN13_BASE": BASE,
        "ADMN13_WIDGET": str(WIDGET),
        "ADMN13_EMAIL": f"admn13-{uuid.uuid4().hex[:8]}@example.test",
        "ADMN13_PASSWORD": "correct-horse-battery",
        "ADMN13_WORKSPACE": f"ADMN13 Walk {uuid.uuid4().hex[:6]}",
        "ADMN13_QA_QUESTION": QUESTION_A,
        "ADMN13_QA_ANSWER": ANSWER_A,
        "ADMN13_QB_QUESTION": QUESTION_B,
        "ADMN13_QB_ANSWER": ANSWER_B,
    }
    result = subprocess.run(
        ["node", str(DRIVER)],
        cwd=WIDGET,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"browser driver failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    print(f"[*] walking publish/preview/rollback on {BASE} with two documents")
    report = drive()
    for step in report.get("steps", []):
        print(f"  - {step['name']}: {step['detail']}")

    if report.get("fatal"):
        print(f"\nFAILED: the browser stopped at: {report['fatal']}")
        return 1
    if report.get("errors"):
        print(f"\nFAILED: uncaught JS errors on the screen: {report['errors']}")
        return 1

    print()
    failures = []
    for clause, ok in clauses(report):
        print(f"[{'PASS' if ok else 'FAIL'}] {clause}")
        if not ok:
            failures.append(clause)

    print()
    if failures:
        print(f"ADMN-13: FAILED — {len(failures)} clause(s) did not hold: {'; '.join(failures)}")
        return 1
    print("ADMN-13: every clause observed (rollback driven through the API: no screen control).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
