"""M5 gate: prove the owner script a stranger would follow, without a developer.

PLAN §7 M5 — *"Done when: the owner script — sign up -> create bot -> add
knowledge -> test -> customise -> copy snippet — completes without developer
help; export produces transcript files; analytics reconcile with DB counts."*

The driver in ``m5_driver.mjs`` walks those steps in a real browser and reports
what the screens showed; this script asserts the clauses and reconciles the
analytics screen against the database. It reads the UI rather than calling the
API for the steps the criterion calls UI steps, because "completes without
developer help" is a claim about screens — an API-only run would pass while the
page was blank.

One thing is set up out of band and it is stated rather than hidden: a
**verified domain row**. No endpoint registers or verifies a domain (HALT-06),
so without it a visitor can never reach a new tenant's bot and the export and
analytics clauses would have nothing real to reconcile. The row is inserted
between the two phases, exactly as ``m4_gate.py`` and ``seed_widget_bot.py`` do.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/m5_gate.py

Exits non-zero if any clause stops holding.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg

BASE = "http://localhost"
ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "backend" / "api" / "widget"
DRIVER = ROOT / "scripts" / "m5_driver.mjs"


def admin_dsn(dbname: str) -> str:
    return (
        f"host={os.environ.get('TEST_PG_HOST', '127.0.0.1')}"
        f" port={os.environ.get('TEST_PG_PORT', '5432')}"
        f" user={os.environ.get('TEST_PG_USER', 'eva')}"
        f" password={os.environ.get('TEST_PG_PASSWORD', 'changeme')}"
        f" dbname={dbname}"
    )


def drive(phase: str, env: dict[str, str]) -> dict:
    result = subprocess.run(
        ["node", str(DRIVER)],
        cwd=WIDGET,
        env={**os.environ, "M5_BASE": BASE, "M5_PHASE": phase, "M5_WIDGET": str(WIDGET), **env},
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"browser driver failed in phase {phase}:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def report(clause: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {clause}")
    return ok


def main() -> int:
    if not (WIDGET / "node_modules" / "playwright").is_dir():
        raise SystemExit(
            f"playwright is not installed under {WIDGET}/node_modules.\n"
            "Run `npm ci && npx playwright install chromium` there first."
        )

    shots = Path(tempfile.mkdtemp(prefix="eva-m5-shots-"))
    state = Path(tempfile.mkdtemp(prefix="eva-m5-state-")) / "state.json"
    email = f"m5-{uuid.uuid4().hex[:8]}@example.test"
    question = "How long does standard shipping take?"
    answer = "Standard shipping takes 3-5 business days."
    greeting = "Hi! Ask us anything about shipping."

    shared = {
        "M5_STATE": str(state),
        "M5_SHOTS": str(shots),
        "M5_QUESTION": question,
        "M5_ANSWER": answer,
        "M5_EMAIL": email,
        "M5_PASSWORD": "correct-horse-battery",
        "M5_WORKSPACE": f"M5 Gate {uuid.uuid4().hex[:6]}",
        "M5_BOT": "M5 Support",
        "M5_GREETING": greeting,
    }

    print(f"[*] the owner is a stranger with no instructions: {email}")
    print(f"[*] screenshots: {shots}")

    first = drive("one", shared)
    for step in first.get("steps", []):
        print(f"  - {step['name']}: {step['detail']}")
    if first.get("fatal"):
        print(f"\nFAILED: the browser stopped at: {first['fatal']}")
        return 1
    if first.get("errors"):
        print(f"\nFAILED: uncaught JS errors on the owner path: {first['errors']}")
        return 1

    snippet = first.get("snippet")
    if not snippet:
        print("\nFAILED: the bot page never showed a snippet, so nothing can embed")
        return 1

    dbname = os.environ.get("EVA_DB", "eva")
    with psycopg.connect(admin_dsn(dbname)) as conn:
        conn.execute(
            "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)"
            " SELECT b.tenant_id, b.id, %s, true FROM bots b WHERE b.public_id = %s"
            " ON CONFLICT (bot_id, domain) DO NOTHING",
            (BASE, snippet),
        )
        conn.commit()
    print(f"[*] verified domain row for {BASE} inserted (no endpoint does this: HALT-06)")

    second = drive("two", {**shared, "M5_BOT_ID": snippet})
    for step in second.get("steps", []):
        print(f"  - {step['name']}: {step['detail']}")
    if second.get("fatal"):
        print(f"\nFAILED: the visitor path stopped at: {second['fatal']}")
        return 1
    if second.get("errors"):
        print(f"\nFAILED: uncaught JS errors on the visitor path: {second['errors']}")
        return 1

    observed = {**first, **second}
    print()
    failures: list[str] = []
    for clause, ok in [
        ("sign up completes and leaves the login page", bool(first.get("steps"))),
        ("the snippet is on screen, so the owner can copy it", observed.get("snippetIsOnScreen")),
        ("the Q&A appears as a document row", (observed.get("knowledgeRows") or 0) >= 1),
        ("publishing reports the content live", observed.get("publishSaidLive")),
        ("the test box answers from the live content", observed.get("testFoundAnswer")),
        ("customisation survives a reload", observed.get("customisationPersisted")),
        ("a visitor's launcher renders on a host page", observed.get("launcherVisible")),
        ("the widget answers the visitor from that content", observed.get("widgetAnswered")),
        ("the conversation is listed", observed.get("conversationsListed")),
        ("the txt export contains the question", observed.get("exportHasQuestion")),
        ("the txt export contains the answer", observed.get("exportHasAnswer")),
        ("the json export parses", observed.get("jsonExportParses")),
    ]:
        if not report(clause, bool(ok)):
            failures.append(clause)

    with psycopg.connect(admin_dsn(dbname)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM conversations c JOIN users u ON u.tenant_id = c.tenant_id"
            " WHERE u.email = %s",
            (email,),
        ).fetchone()
    in_db = row[0] if row else 0
    print(f"[*] conversations in the database for this tenant: {in_db}")

    screen = observed.get("analyticsText") or ""
    numbers = {int(n) for n in re.findall(r"\b(\d+)\b", screen)}
    if not report("a visitor conversation exists to reconcile against", in_db >= 1):
        failures.append("a conversation exists")
    elif not report(
        f"the analytics screen shows the database count ({in_db})", in_db in numbers
    ):
        failures.append("analytics reconciles with DB counts")
        print(f"        screen said: {screen[:500]}")

    print()
    if failures:
        print(f"M5 GATE: FAILED — {len(failures)} clause(s) did not hold: {'; '.join(failures)}")
        return 1
    print("M5 GATE: every clause observed. Whether this clears M5 is the operator's ruling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
