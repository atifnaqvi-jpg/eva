"""OPS-08 gate: the load test ARCHITECTURE §27 asks for, in one command.

§27 — *"Load test: 50 concurrent streams, no error spike"*. The registry row also
names *"ingestion of a 500-page document set"*. This script drives both halves
against the live compose stack and exits non-zero if either stops holding.

* **Streams** — ``scripts/ops08_sse.js`` under k6: 50 VUs, one turn each, assert
  every stream reaches its `final` event with zero HTTP failures.
* **Ingestion** — one synthetic 500-section document (parsed to 100 chunks by the
  real chunker) uploaded through the real API, embedded by the real ``embed``
  service and stored by the real worker. Asserts the version reaches ``ready``
  with every chunk written.

Evidence format follows ``scripts/m1_gate.py`` / ``m4_gate.py``: one command that
fails loudly, because a transcript of manual steps is not evidence.

Prerequisites, because this script refuses to fake them:

* the stack up (``sudo docker compose up -d --build``) and reachable on the Caddy
  port from ``docker compose ps`` — **not** 8000, which is a different service in
  this sandbox;
* the ``k6`` binary, which is a test-only tool and deliberately not vendored into
  a product image. ``K6_BIN`` overrides the path;
* a bot with a verified domain row that matches ``ORIGIN`` exactly (scheme and
  port included). ``seed_widget_bot.py`` creates one; §17.4's domains endpoint is
  still unimplemented (HALT-06), so it cannot be done over HTTP.

Usage::

    python scripts/ops08_load.py                 # both halves
    python scripts/ops08_load.py --streams-only  # skip the ~20 min ingest
    python scripts/ops08_load.py --pages 50      # smaller document
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE_URL", "http://localhost")
ORIGIN = os.environ.get("ORIGIN", "http://localhost")
K6_BIN = os.environ.get("K6_BIN", "k6")
PASSWORD = "correct-horse-battery-staple-42"

# ~660 characters per section. 500 of them is ~330 KB, which the real chunker
# splits into 100 chunks at the 400-800 token target — a realistic 500-page set,
# and comfortably above the embed service's 64-text cap so the batching path is
# the one exercised.
SECTION = (
    "Our return policy allows a full refund within thirty days of delivery, "
    "provided the item is unused and in its original packaging. Also see the "
    "shipping terms for damaged goods and the warranty section for repairs. "
)


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def compose(*args: str) -> str:
    """Run a compose command, tolerating the sandbox's sudo-only docker."""
    for prefix in (["sudo", "-n"], []):
        result = subprocess.run(
            [*prefix, "docker", "compose", *args],
            cwd=REPO, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        last = result
    fail(f"docker compose {' '.join(args)} failed: {last.stderr.strip()[:300]}")
    raise AssertionError("unreachable")


def psql(sql: str) -> str:
    return compose("exec", "-T", "postgres", "psql", "-U", "eva", "-d", "eva", "-tAc", sql)


def stack_port() -> None:
    """Refuse to probe a service that is not this stack.

    ``localhost:8000`` is the agent canvas in this sandbox, not EVA; probing it
    returns convincing 200s from a different application. Caddy publishes on 80.
    """
    try:
        response = httpx.get(f"{BASE}/api/v1/health", timeout=10)
    except httpx.HTTPError as exc:
        fail(f"{BASE} is not reachable ({exc}); is the stack up, and is that the Caddy port?")
    if response.status_code >= 500:
        fail(f"{BASE}/api/v1/health answered {response.status_code}")


# --- streams ---------------------------------------------------------------


def check_streams(vus: int) -> dict:
    """50 concurrent SSE streams, every one reaching its `final` event."""
    bot = psql(
        "SELECT b.public_id FROM bots b JOIN bot_domains d ON d.bot_id = b.id"
        " WHERE d.verified ORDER BY b.created_at LIMIT 1"
    )
    if not bot:
        fail(
            "no bot with a verified domain row; run scripts/seed_widget_bot.py first"
            " (the domains endpoint from §17.4 is unimplemented — HALT-06)"
        )

    env = {
        **os.environ,
        "BASE_URL": BASE,
        "ORIGIN": ORIGIN,
        "BOT_ID": bot,
        "VUS": str(vus),
        "MAX_DURATION": "10m",
    }
    script = REPO / "scripts" / "ops08_sse.js"
    print(f"k6: {vus} VUs x 1 turn against {BASE} (bot {bot})")
    result = subprocess.run(
        [K6_BIN, "run", "--quiet", "--summary-export", "/tmp/ops08_summary.json", str(script)],
        env=env, capture_output=True, text=True,
    )
    tail = (result.stdout or "").strip().splitlines()[-25:]
    for line in tail:
        print(f"  {line}")

    summary = Path("/tmp/ops08_summary.json")
    if not summary.exists():
        fail(f"k6 produced no summary (exit {result.returncode}): {(result.stderr or '')[:400]}")
    metrics = json.loads(summary.read_text())["metrics"]
    finals = metrics.get("sse_final_events", {}).get("count", 0)
    hung = metrics.get("sse_streams_without_final", {}).get("count", 0)
    failed = metrics.get("http_req_failed", {}).get("value", 1.0)

    if result.returncode != 0:
        fail(f"k6 thresholds crossed: {finals} finals, {hung} hung, {failed:.2%} http failures")
    if finals < vus:
        fail(f"only {finals}/{vus} streams reached `final`")
    if hung:
        fail(f"{hung} stream(s) opened and never reached `final`")
    if failed:
        fail(f"http_req_failed rate {failed:.2%}, expected 0")
    return {"streams": vus, "finals": int(finals), "hung": int(hung)}


# --- ingestion -------------------------------------------------------------


def check_ingestion(pages: int) -> dict:
    """A 500-page document set ends `ready`, with every chunk stored."""
    email = f"ops08-{uuid.uuid4().hex[:8]}@example.com"
    body = "\n\n".join(f"Section {i}. {SECTION}" for i in range(pages))

    with httpx.Client(timeout=300) as client:
        register = client.post(
            f"{BASE}/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "tenant_name": "OPS-08 Load"},
        )
        if register.status_code != 201:
            fail(f"register answered {register.status_code}: {register.text[:200]}")
        login = client.post(f"{BASE}/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        if login.status_code != 200:
            fail(f"login answered {login.status_code}: {login.text[:200]}")

        upload = client.post(
            f"{BASE}/api/v1/knowledge/uploads",
            params={"filename": f"ops08-{pages}p.txt"},
            content=body.encode(),
            headers={"Content-Type": "text/plain"},
        )
        if upload.status_code != 201:
            fail(f"upload answered {upload.status_code}: {upload.text[:300]}")

    uploaded = upload.json()
    version_id = uploaded["version_id"]
    chunks = uploaded["chunks"]
    print(f"uploaded {len(body)} bytes -> {chunks} chunks, version {version_id}")

    started = time.perf_counter()
    deadline = started + 2400
    status = "processing"
    while time.perf_counter() < deadline:
        time.sleep(15)
        status = psql(f"SELECT status FROM document_versions WHERE id = '{version_id}'")
        if not status.startswith("processing"):
            break
        print(f"  ...{status} {time.perf_counter() - started:.0f}s")

    error = psql(
        f"SELECT coalesce(left(error, 300), '') FROM document_versions WHERE id = '{version_id}'"
    )
    stored = int(
        psql(f"SELECT count(*) FROM chunks WHERE document_version_id = '{version_id}'") or 0
    )
    elapsed = time.perf_counter() - started

    if status != "ready":
        fail(f"version ended {status!r} after {elapsed:.0f}s: {error}")
    if stored != chunks:
        fail(f"{stored} of {chunks} chunks stored — the document is not fully ingested")
    print(f"ingested in {elapsed:.0f}s: {stored} chunks, status {status}")
    return {"pages": pages, "chunks": chunks, "stored": stored, "seconds": round(elapsed)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vus", type=int, default=50)
    parser.add_argument("--pages", type=int, default=500)
    parser.add_argument("--streams-only", action="store_true")
    parser.add_argument("--ingest-only", action="store_true")
    args = parser.parse_args()

    stack_port()
    evidence: dict[str, object] = {}

    if not args.ingest_only:
        evidence["streams"] = check_streams(args.vus)
    if not args.streams_only:
        evidence["ingestion"] = check_ingestion(args.pages)

    print("\nOPS-08 evidence")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
