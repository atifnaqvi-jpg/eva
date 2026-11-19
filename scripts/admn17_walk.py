"""ADMN-17: drive the restore control on /knowledge in real Chromium.

The in-process half of this row (`tests/test_knowledge_rollback.py`, 12 tests against
real Postgres, retrieval included) proves the service restores a whole set. This half
proves the **screen** offers that and not something else, because the defect the ruling
removed lived entirely in the wiring between a button label and an operation: a control
called "restore previous version" that called a serve-one-document endpoint would pass
every server test in the repository.

Four publishes, three restores. The counts are the assertion: 4 → 3 → 2 → 1, each the
whole set the previous publish replaced. The shape that fails is the old one, where each
restore yields **1** regardless of what came before.

Two more properties a browser is needed for and a source test cannot give:

* the control is **absent or disabled before anything can be restored**, so an owner is
  never offered a request the server refuses;
* the document table's `Served` column agrees with the API after each reload — a restore
  that moved the pointer without the table reflecting it is the M4 "201 in the log is not
  a working feature" defect on this screen.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/admn17_walk.py

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
DRIVER = ROOT / "scripts" / "admn17_driver.mjs"

BASE = os.environ.get("ADMN17_BASE", "http://localhost")

# Four documents published one at a time give three distinct earlier sets, so a restore
# can be pressed three times before there is nothing left. Written here as the row's own
# arithmetic rather than a magic number per clause.
DOCUMENTS = 4
EXPECTED_COUNTS = list(range(DOCUMENTS - 1, 0, -1))

_COUNT_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
}


def _notice_names_count(entry: dict) -> bool:
    """The notice must state the restored count, as digits or as the word.

    Both spellings are accepted because the screen writes "3 documents" for a set and
    "that one document" for a singleton, and pinning one spelling would fail a correct
    notice — the M4 lesson about asserting on model wording.
    """
    count = len(entry.get("versions") or [])
    notice = entry.get("notice") or ""
    if str(count) in notice:
        return True
    return count in _COUNT_WORDS and _COUNT_WORDS[count] in notice


def clauses(report: dict) -> list[tuple[str, bool]]:
    """Every claim this row makes, evaluated against one driver report.

    A pure function over the report so ``tests/test_admn17_walk.py`` can flip one field
    at a time and show the matching clause goes red. A verdict computed inline alongside
    the browser could not be checked that way.
    """
    before = report.get("control_before_publish") or {}
    publishes = report.get("after_each_publish") or []
    restores = report.get("restores") or []
    exhausted = report.get("control_after_exhausted") or {}

    counts = [len(entry.get("versions") or []) for entry in restores]
    live_rows = [entry.get("live_rows") for entry in restores]
    publish_counts = [len(entry.get("versions") or []) for entry in publishes]

    return [
        (
            # An owner with nothing published has nothing to restore, so the control must
            # say so instead of offering a request that 409s.
            "no restore is offered before the first publish",
            before.get("rendered") is True and before.get("disabled") is True,
        ),
        (
            # Publishing is additive (RAG-11); if this is wrong the restores below are
            # restoring from a set that was never built.
            "each publish adds one document to the served set",
            publish_counts == list(range(1, DOCUMENTS + 1)),
        ),
        (
            # The row's defect, in one line: the old endpoint produced 1 every time.
            "each restore serves the whole earlier set, not one document",
            counts == EXPECTED_COUNTS,
        ),
        (
            # A pointer moved and the table did not follow is the M4 defect shape: the
            # count column is the screen's own answer, read after a reload.
            "the Served column agrees with the API after every restore",
            live_rows == counts and all(row > 0 for row in live_rows),
        ),
        (
            # The screen must report what came back, and the number must come from the
            # response rather than a local guess. A one-document restore reads "…that one
            # document again", so the word is the count there.
            "the notice reports how many documents came back",
            all(_notice_names_count(entry) for entry in restores),
        ),
        (
            # The chain ends: the first publish replaced nothing, so there is no fourth
            # restore to offer.
            "the control is disabled once the history is exhausted",
            exhausted.get("rendered") is True and exhausted.get("disabled") is True,
        ),
        (
            # And it said why, rather than going quietly grey.
            "the exhausted state explains itself",
            "Nothing to restore" in (exhausted.get("hint") or ""),
        ),
    ]


def drive() -> dict:
    if not (WIDGET / "node_modules" / "playwright").is_dir():
        raise SystemExit(
            f"playwright is not installed under {WIDGET}/node_modules.\n"
            "Run `npm ci && npx playwright install chromium` there first."
        )
    run = uuid.uuid4().hex[:8]
    env = {
        **os.environ,
        "EVA_BASE_URL": BASE,
        "EVA_WIDGET": str(WIDGET),
        "EVA_EMAIL": f"admn17-{run}@example.test",
        "EVA_PASSWORD": "correct-horse-battery",
        "EVA_WORKSPACE": f"ADMN17 {run}",
        "EVA_RUN": run,
    }
    result = subprocess.run(
        ["node", str(DRIVER)],
        cwd=WIDGET,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise SystemExit(f"browser driver produced no report:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    print(f"[*] driving the restore control on {BASE}")
    report = drive()

    if report.get("fatal"):
        print(f"\nFAILED: the browser stopped at: {report['fatal']}")
        return 1

    for step in report.get("steps") or []:
        print(f"    {step['name']}: {step['detail']}")

    print()
    failures = []
    for clause, ok in clauses(report):
        print(f"[{'PASS' if ok else 'FAIL'}] {clause}")
        if not ok:
            failures.append(clause)

    print()
    if failures:
        print(f"ADMN-17: FAILED — {len(failures)} clause(s) did not hold: {'; '.join(failures)}")
        return 1
    print("ADMN-17: every clause observed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
