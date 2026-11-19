"""ADMN-16: drive the retention editor and the domain controls in real Chromium.

The in-process suite cannot see this screen. `tests/test_settings_controls.py` reads the
source and calls the client, which catches a drifted constant or a mis-wired handler —
but not the two things a browser is needed for:

* a form whose `min`/`max` the API refuses, where the refusal has to become a visible
  sentence rather than a number that silently reverts;
* a control that appears to save while the request never left, which is only
  disprovable by re-reading the server's own value after the click.

Both are checked, and so is the property ADMN-16 must not break: **adding a domain
leaves the origin refused.** A control that verified on add would serve a host nobody
proved they control, which is the defect SEC-15 closed.

The successful-verify path is **not** covered here — it needs a TXT record in a resolver
the container can reach, which is SEC-15's fixture (`tests/test_domain_verification.py`
drives it against a real UDP server) and not this row's. Stated rather than skipped
silently.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/admn16_walk.py

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
DRIVER = ROOT / "scripts" / "admn16_driver.mjs"

BASE = os.environ.get("ADMN16_BASE", "http://localhost")


def clauses(report: dict) -> list[tuple[str, bool]]:
    """Every claim this row makes, evaluated against one driver report.

    A pure function over the report, so each clause can be shown to fail:
    ``tests/test_admn16_walk.py`` flips one field at a time and asserts the matching
    clause goes red. A gate whose verdict is computed inline cannot be checked that way.
    """
    form = report.get("retention_form") or {}
    saved = report.get("retention_after_valid_save") or {}
    rejected = report.get("retention_after_rejected_save") or {}
    added = report.get("domain_after_add")
    removal = report.get("after_remove") or {}

    return [
        (
            "the retention editor renders with the policy's own bounds",
            form.get("min") == "1"
            and form.get("max") == "730"
            and bool(form.get("value")),
        ),
        (
            # The server is re-read, so a form that showed a new number without the
            # request leaving is red here.
            "a valid window persists and the card re-reads the stored number",
            saved.get("api_days") == 14 and saved.get("card_mentions") is True,
        ),
        (
            # The boundary is the row's stated requirement; a control that only accepts
            # valid input has not been shown to refuse anything.
            "an out-of-range window is refused with the server's message and changes nothing",
            rejected.get("api_days") == 14
            and rejected.get("refusal_visible") is True
            and rejected.get("message_names_bound") is True,
        ),
        (
            "adding a domain stores it unverified and shows the record to publish",
            bool(added)
            and added.get("verified") is False
            and bool(added.get("record_name"))
            and added.get("record_on_screen") is True,
        ),
        (
            # The property that keeps add and verify separate: a control that verified
            # on add would serve a host nobody proved they control.
            "the widget still refuses that origin after the add",
            report.get("widget_status_after_add") == 403,
        ),
        (
            "removing the domain takes it off the screen and out of the API",
            removal.get("still_in_api") is False
            and removal.get("still_on_screen") is False,
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
        "EVA_BASE_URL": BASE,
        "EVA_WIDGET": str(WIDGET),
        "EVA_EMAIL": f"admn16-{uuid.uuid4().hex[:8]}@example.test",
        "EVA_PASSWORD": "correct-horse-battery",
        "EVA_TEST_DOMAIN": f"shop.{uuid.uuid4().hex[:8]}.admn16.test",
        "EVA_WORKSPACE": f"ADMN16 {uuid.uuid4().hex[:6]}",
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
    print(f"[*] driving the settings controls on {BASE}")
    report = drive()

    if report.get("fatal"):
        print(f"\nFAILED: the browser stopped at: {report['fatal']}")
        return 1

    print()
    failures = []
    for clause, ok in clauses(report):
        print(f"[{'PASS' if ok else 'FAIL'}] {clause}")
        if not ok:
            failures.append(clause)

    print()
    if failures:
        print(f"ADMN-16: FAILED — {len(failures)} clause(s) did not hold: {'; '.join(failures)}")
        return 1
    print("ADMN-16: every clause observed (verify-with-a-live-record is SEC-15's fixture).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
