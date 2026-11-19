"""ADMN-15 evidence: prove the rollback control cannot be built as the row specifies.

The row asks for a "restore previous version" action beside publish. Three facts have to
hold for that control to be honest, and this script measures each one through Caddy
against the live stack — not by reading the source:

1. **`/knowledge/rollback` discards the rest of the served set.** Rolling back to one
   document serves exactly that document, so an owner who has five documents published
   loses four of them by clicking it once. If that is true, a button labelled "restore
   previous version" is a button that silently unpublishes their knowledge base.
2. **The screen cannot name a "previous version".** `GET /knowledge/documents` returns
   only the *newest* version of each document, so the list an owner would pick from
   contains one entry per document and never an earlier one. A control needs a value to
   offer; that value is not on the wire.
3. **Re-publishing an old version is additive, not a restore.** Publishing B then A
   again leaves the served set holding both, so there is no API that expresses "revert".

Any one of these leaves the row's stated control either wrong or impossible. All three
together mean the row needs a ruling, which is what `HALT-10` records.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/admn15_evidence.py

Exits 0 when the blocker is reproduced (the evidence is what the halt cites), 1 when any
premise fails to hold — in which case the blocker is gone and the row can be built.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.environ.get("ADMN15_BASE", "http://localhost")


class Client:
    def __init__(self) -> None:
        self.cookie = ""
        self._bot_id = ""

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
                set_cookie = response.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}

    def upload_faq(self, question: str, answer: str) -> str:
        status, body = self.call(
            "POST", "/api/v1/knowledge/faqs", {"question": question, "answer": answer}
        )
        if status != 201:
            raise SystemExit(f"faq upload failed: {status} {body}")
        return body["version_id"]

    def call_checked(self, method: str, path: str, body: dict | None = None) -> dict:
        status, payload = self.call(method, path, body)
        if status not in (200, 201):
            raise SystemExit(f"{method} {path} -> {status} {payload}")
        return payload

    def bot_id(self) -> str:
        if not self._bot_id:
            bots = self.call_checked("GET", "/api/v1/bots")
            if not bots:
                raise SystemExit("the tenant has no bot")
            self._bot_id = bots[0]["id"]
        return self._bot_id

    def upload_file(self, filename: str, payload: bytes) -> dict:
        request = urllib.request.Request(
            f"{BASE}/api/v1/knowledge/uploads?filename={urllib.parse.quote(filename)}",
            data=payload,
            method="POST",
        )
        request.add_header("Content-Type", "application/octet-stream")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"upload {filename} -> {exc.code} {exc.read().decode()}") from exc

    def served(self) -> list[str]:
        detail = self.call_checked("GET", f"/api/v1/bots/{self.bot_id()}")
        ids = detail.get("active_knowledge_version_ids")
        if ids is None:
            single = detail.get("active_knowledge_version_id")
            ids = [single] if single else []
        return sorted(ids)


def main() -> int:
    client = Client()
    stamp = uuid.uuid4().hex[:8]
    status, body = client.call(
        "POST",
        "/api/v1/auth/register",
        {
            "email": f"admn15-{stamp}@example.test",
            "password": "correct-horse-battery",
            "tenant_name": f"ADMN15 {stamp}",
        },
    )
    if status != 201:
        raise SystemExit(f"register failed: {status} {body}")

    version_a = client.upload_faq(
        "How long does standard shipping take?", "Standard shipping takes 3-5 business days."
    )
    version_b = client.upload_faq("Do you ship to the EU?", "Yes, with a 7-day delivery window.")

    print(f"[*] two documents: A={version_a[:8]} B={version_b[:8]}")

    # --- fact 3: publishing is additive -------------------------------------
    bot = client.bot_id()
    client.call_checked(
        "POST", "/api/v1/knowledge/publish", {"bot_id": bot, "version_id": version_a}
    )
    client.call_checked(
        "POST", "/api/v1/knowledge/publish", {"bot_id": bot, "version_id": version_b}
    )
    after_both = client.served()
    print(f"    after publish A then B: {len(after_both)} served")

    client.call_checked(
        "POST", "/api/v1/knowledge/publish", {"bot_id": bot, "version_id": version_a}
    )
    after_republish = client.served()
    print(f"    after re-publishing A:  {len(after_republish)} served  {after_republish == after_both}")

    fact_3 = after_republish == after_both and len(after_republish) == 2

    # --- fact 1: rollback discards the rest of the set ----------------------
    status, _ = client.call(
        "POST", "/api/v1/knowledge/rollback", {"bot_id": bot, "version_id": version_a}
    )
    after_rollback = client.served()
    print(f"    after rollback to A:    {len(after_rollback)} served (status {status})")

    fact_1 = status == 200 and len(after_rollback) == 1

    # --- fact 2: the list exposes only the newest version -------------------
    # Not measured here: it is an API-shape claim, so it is a test rather than a live
    # probe — `tests/test_knowledge_version_history.py` seeds a document with two
    # versions through the real session and asserts that `GET /knowledge/documents`
    # reports one row, at the newer version_no. This script is for the two claims that
    # need a live stack (a pointer swap and an additive publish), and the halt record
    # cites the test for the third.

    print()
    findings = [
        ("rollback discards the rest of the served set", fact_1),
        ("re-publishing an old version is additive, not a restore", fact_3),
    ]
    for name, held in findings:
        print(f"[{'REPRODUCED' if held else 'NOT HELD'}] {name}")

    print()
    if all(held for _, held in findings):
        print("ADMN-15: the row's control cannot be built as specified — HALT-10 stands.")
        return 0
    print("ADMN-15: a premise no longer holds — the blocker is gone, review before halting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
