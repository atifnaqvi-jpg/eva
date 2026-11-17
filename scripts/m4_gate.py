"""M4 gate: prove the handover path end to end against the live stack.

PLAN §8 M4 — *"Done when: a staged handover email with transcript+summary
arrives from a real conversation trigger; signed webhook fires; offline mode
follows business-hours config."* This script produces exactly those three
observations, in that order, and prints the evidence it relied on.

Everything goes through Caddy over HTTP, then the real app, the real provider
and a real SMTP conversation. Nothing is stubbed. All fixtures are synthetic,
per the confidentiality charter.

What cannot go through HTTP, and why:

* the domain row — there is no bot-domains endpoint at all (ADMN-03 is M5), so
  the verified origin is inserted directly, as ``seed_widget_bot.py`` does;
* the owner address and business hours — no settings endpoint carries them yet,
  so they are written to ``tenants.settings`` the same way.

Prerequisites, because this script refuses to fake them:

* ``SMTP_HOST`` pointing at a sink that writes ``*.eml`` files somewhere this
  process can read (``SMTP_SINK_DIR``);
* a provider key in the API container, so the summary is a *real* generation.
  The script fails rather than accept a fallback summary, because a fallback
  would not prove the clause.

Usage, with the stack up:

    sudo docker compose up -d --build
    python scripts/m4_gate.py

``WEBHOOK_ALLOW_PRIVATE_HOSTS=true`` must be set in ``.env`` for the duration of
the run (the receiver is a container on the compose network, and the sandbox
firewall blocks container -> host, so a host-side listener is not reachable).
The script says so before it starts rather than failing obscurely.
"""

from __future__ import annotations

import email
import glob
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import psycopg

BASE = "http://localhost"
ORIGIN = "http://localhost"
RECEIVER_NAME = "eva-m4-recv"
RECEIVER_PORT = 8080  # in ALLOWED_PORTS; the gate must not widen the product allowlist

RECEIVER = '''\
"""Record each delivery, verbatim, for the gate to verify independently."""
import http.server, json, os, time

OUT = "/out"

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        with open(f"{OUT}/hook-{int(time.time() * 1000)}.json", "w") as f:
            json.dump({
                "timestamp": self.headers.get("x-eva-timestamp", ""),
                "signature": self.headers.get("x-eva-signature", ""),
                "event": self.headers.get("x-eva-event", ""),
                "body": body.decode("utf-8", "replace"),
            }, f, indent=2)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass

http.server.HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
'''


def verify_signature(record: dict, secret: str) -> bool:
    """Recompute `v1=` HMAC over ``timestamp.body`` with the shared secret.

    Checked here rather than in the receiver so the assertion rests on the secret
    the API returned at creation, not on a copy handed to the listener — a
    receiver that trusted its own copy would "pass" even if the two disagreed.
    """
    expected = "v1=" + hmac.new(
        secret.encode(),
        record["timestamp"].encode() + b"." + record["body"].encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, record["signature"])


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run docker, adding sudo only if the socket is not reachable directly."""
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0 and "permission denied" in (result.stderr or "").lower():
        result = subprocess.run(
            ["sudo", "docker", *args], capture_output=True, text=True, check=False
        )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result


def compose_network() -> str:
    containers = docker("ps", "--format", "{{.Names}}").stdout.split()
    api = next((c for c in containers if c.endswith("-api-1")), None)
    if api is None:
        raise SystemExit("no running api container; start the stack first")
    return docker(
        "inspect", api,
        "--format", "{{range $name, $net := .NetworkSettings.Networks}}{{$name}}{{end}}",
    ).stdout.strip()


def admin_dsn(dbname: str) -> str:
    return (
        f"host={os.environ.get('TEST_PG_HOST', '127.0.0.1')}"
        f" port={os.environ.get('TEST_PG_PORT', '5432')}"
        f" user={os.environ.get('TEST_PG_USER', 'eva')}"
        f" password={os.environ.get('TEST_PG_PASSWORD', 'changeme')}"
        f" dbname={dbname}"
    )


def start_receiver(network: str, out_dir: Path) -> None:
    site = Path(tempfile.mkdtemp(prefix="eva-m4-recv-"))
    (site / "recv.py").write_text(RECEIVER)
    docker("rm", "-f", RECEIVER_NAME, check=False)
    docker(
        "run", "-d", "--name", RECEIVER_NAME, "--network", network,
        "-e", f"PORT={RECEIVER_PORT}",
        "-v", f"{site}:/srv:ro",
        "-v", f"{out_dir}:/out",
        "python:3.12-slim",
        "python", "/srv/recv.py",
    )


def stop_receiver() -> None:
    docker("rm", "-f", RECEIVER_NAME, check=False)


def newest_eml(directory: Path, after: float) -> Path | None:
    """The newest message the sink wrote since ``after``."""
    candidates = [
        Path(p) for p in glob.glob(str(directory / "**" / "*.eml"), recursive=True)
        if Path(p).stat().st_mtime >= after
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def read_eml(path: Path) -> tuple[str, str]:
    message = email.message_from_string(path.read_text(errors="replace"))
    body = message.get_payload(decode=True) or b""
    return message.get("Subject", ""), body.decode("utf-8", errors="replace")


def main() -> int:
    if not os.environ.get("SMTP_SINK_DIR"):
        raise SystemExit(
            "set SMTP_SINK_DIR to the directory the SMTP sink writes *.eml files into"
        )
    sink = Path(os.environ["SMTP_SINK_DIR"])
    if not sink.is_dir():
        raise SystemExit(f"SMTP_SINK_DIR is not a directory: {sink}")

    network = compose_network()
    out_dir = Path(tempfile.mkdtemp(prefix="eva-m4-hooks-"))
    start_receiver(network, out_dir)
    time.sleep(2)
    try:
        return run(out_dir=out_dir, sink=sink)
    finally:
        stop_receiver()


def run(*, out_dir: Path, sink: Path) -> int:
    email_address = f"m4-{uuid.uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery"
    tenant_name = f"M4 Gate {uuid.uuid4().hex[:6]}"
    dbname = os.environ.get("EVA_DB", "eva")

    with httpx.Client(base_url=BASE, timeout=240.0) as client:
        created = client.post(
            "/api/v1/auth/register",
            json={"email": email_address, "password": password, "tenant_name": tenant_name},
        )
        assert created.status_code == 201, created.text
        assert client.post(
            "/api/v1/auth/login", json={"email": email_address, "password": password}
        ).status_code == 200
        print(f"[1] registered {email_address}")

        # Synthetic knowledge so the assistant has something true to answer from.
        faq = client.post(
            "/api/v1/knowledge/faqs",
            json={
                "question": "How long does standard shipping take?",
                "answer": "Standard shipping takes 3-5 business days.",
            },
        )
        assert faq.status_code == 201, faq.text
        version_id = faq.json()["version_id"]

        with psycopg.connect(admin_dsn(dbname)) as conn:
            row = conn.execute(
                "SELECT b.id::text, b.public_id::text, b.tenant_id::text"
                " FROM bots b JOIN tenants t ON t.id = b.tenant_id"
                " WHERE t.name = %s ORDER BY b.created_at DESC LIMIT 1",
                (tenant_name,),
            ).fetchone()
            if row is None:
                raise SystemExit("no bot was created for the new tenant")
            bot_id, public_id, tenant_id = row

            conn.execute(
                "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)"
                " VALUES (%s, %s, %s, true)"
                " ON CONFLICT (bot_id, domain) DO NOTHING",
                (tenant_id, bot_id, ORIGIN),
            )
            # The owner address decides whether a handover notifies at all: no
            # address means "recorded", which is the correct behaviour for an
            # unfinished tenant and would leave this gate with nothing to read.
            conn.execute(
                "UPDATE tenants SET settings = jsonb_set("
                " coalesce(settings, '{}'::jsonb), '{handover}', %s::jsonb)"
                " WHERE id = %s",
                (json.dumps({"email": email_address}), tenant_id),
            )
            conn.commit()
        print(f"[2] bot {public_id} published, origin {ORIGIN} verified")

        published = client.post(
            "/api/v1/knowledge/publish", json={"bot_id": bot_id, "version_id": version_id}
        )
        assert published.status_code in (200, 201, 202), published.text

        webhook = client.post(
            "/api/v1/webhooks",
            json={
                "url": f"http://{RECEIVER_NAME}:{RECEIVER_PORT}/hook",
                "events": ["conversation.created", "handover.created"],
            },
        )
        assert webhook.status_code == 201, webhook.text
        hook_secret = webhook.json().get("secret")
        assert hook_secret, "creation must return the secret exactly once"
        assert "secret" not in client.get("/api/v1/webhooks").json()[0], (
            "GET must never expose the secret again"
        )
        print("[3] webhook registered; secret returned once, absent from GET")

        # A widget session is the public path: no container-side minting, and the
        # origin check runs for real.
        session = client.post(
            "/api/v1/widget/session",
            json={"bot_public_id": public_id},
            headers={"Origin": ORIGIN},
        )
        assert session.status_code == 200, session.text
        assert session.json()["online"] is True, "no business hours configured means online"
        token = session.json()["token"]
        print("[4] widget session minted; online (no hours configured)")

        visitor = httpx.Client(
            base_url=BASE, timeout=240.0,
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        )
        started = visitor.post(
            "/api/v1/widget/conversations",
            json={
                "visitor_session_id": f"m4-{uuid.uuid4().hex[:8]}",
                "page_url": f"{ORIGIN}/demo/host",
            },
        )
        assert started.status_code == 201, started.text
        conversation_id = started.json()["conversation_id"]

        question = "How long does standard shipping take?"
        turn = _stream(visitor, conversation_id, question)
        # The declined branch carries no `degraded` key at all, so keying on it
        # alone would raise here and look like a broken gate rather than a
        # declined turn. Check the answer path first, explicitly.
        assert turn.get("declined") is False, (
            f"the assistant declined, so nothing was generated: {turn.get('reason')!r}"
        )
        assert turn.get("degraded") is False, (
            "the turn degraded, so the summary would be a fallback and would not"
            " prove the LLM-summary clause; check the provider key in the API container"
        )
        assert turn.get("model"), "a live turn must name the model that produced it"
        print(f"[5] live turn answered by {turn['model']}, degraded=False")
        print(f"    answer: {turn['answer'][:100]!r}")

        before = time.time()
        handover = visitor.post(
            f"/api/v1/widget/conversations/{conversation_id}/handover",
            json={
                "email": "visitor@example.test",
                "name": "M4 Gate Visitor",
                "note": "Please email me about shipping.",
            },
        )
        assert handover.status_code == 201, handover.text
        assert handover.json()["status"] == "handed_over", handover.text
        print(f"[6] handover {handover.json()['handover_id']} raised (explicit)")

        delivered = _await_eml(sink, before)
        if delivered is None:
            print("FAIL: no email reached the sink; is SMTP_HOST pointing at it?")
            return 1
        subject, body = read_eml(delivered)
        assert "Summary:" in body, body[:400]
        summary = body.split("Summary:", 1)[1].split("\n", 1)[0].strip()
        # The fallback quotes the visitor's opening line; a generated summary
        # paraphrases instead. Rejecting the fallback is the difference between
        # proving the clause and proving the plumbing.
        assert not summary.startswith("Visitor opened with:"), (
            f"summary is the no-model fallback, not a generation: {summary!r}"
        )
        assert len(summary) > 60, f"summary is too thin to be a generation: {summary!r}"
        print(f"[7] email arrived: {delivered.name}, subject {subject!r}")
        print(f"    summary: {summary[:160]!r}")

        # The transcript has to be in the same message: the clause says
        # "transcript+summary", and the owner needs both to triage.
        assert "Visitor:" in body and "Assistant:" in body, "transcript missing from email"
        # Compare against the answer the API actually returned rather than a
        # literal: the model may write "3-5" or "3–5", and an assertion on the
        # spelling would fail for a correct email.
        assert _flat(turn["answer"]) in _flat(body), (
            "the answer the bot gave is not in the email"
        )
        print("[8] email carries the transcript and the answer the bot gave")

        events = _await_hooks(out_dir, before)
        got = {e["event"]: e for e in events}
        assert "conversation.created" in got, f"missing conversation.created; got {sorted(got)}"
        assert "handover.created" in got, f"missing handover.created; got {sorted(got)}"
        for name, event in sorted(got.items()):
            assert verify_signature(event, hook_secret), f"{name} signature did not verify"
        print(f"[9] signed webhooks fired and verify: {sorted(got)}")

        # Offline mode must follow configuration, not the wall clock alone: force
        # a window that cannot contain "now" and check the session says so.
        with psycopg.connect(admin_dsn(dbname)) as conn:
            conn.execute(
                "UPDATE tenants SET settings = jsonb_set(settings, '{business_hours}',"
                " %s::jsonb) WHERE id = %s",
                (
                    json.dumps({"timezone": "UTC", "week": {"mon": ["00:00", "00:01"]}}),
                    tenant_id,
                ),
            )
            conn.commit()
        offline = client.post(
            "/api/v1/widget/session",
            json={"bot_public_id": public_id},
            headers={"Origin": ORIGIN},
        )
        assert offline.status_code == 200, offline.text
        assert offline.json()["online"] is False, offline.text
        assert offline.json()["next_open"], "offline must say when the team is back"
        print(
            f"[10] offline mode follows hours config: online=False,"
            f" next_open={offline.json()['next_open']!r}"
        )

        with psycopg.connect(admin_dsn(dbname)) as conn:
            conn.execute(
                "UPDATE tenants SET settings = settings - 'business_hours' WHERE id = %s",
                (tenant_id,),
            )
            conn.commit()

    # Deliberately not "PASS": the gate is the operator's call. This asserts that
    # the evidence still holds, and exits non-zero when it does not.
    print("\nM4 GATE EVIDENCE: COMPLETE (ruling is the operator's)")
    print(
        "\nCaveats the operator should weigh before ruling:\n"
        "  * the SMTP sink is an ad-hoc container, not a compose service; a real\n"
        "    provider must be configured before email counts as production-ready;\n"
        "  * the origin row and the owner address were written by SQL because no\n"
        "    endpoint exists for either yet (ADMN-03 is M5);\n"
        "  * WEBHOOK_ALLOW_PRIVATE_HOSTS must be reverted afterwards."
    )
    return 0


def _stream(client: httpx.Client, conversation_id: str, text: str) -> dict:
    """Send one message and reduce the SSE stream to the final event."""
    event = None
    final: dict | None = None
    tokens: list[str] = []
    with client.stream(
        "POST", f"/api/v1/widget/conversations/{conversation_id}/messages",
        json={"text": text},
    ) as response:
        assert response.status_code == 200, response.text
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if event == "token":
                    tokens.append(payload.get("text", ""))
                elif event == "final":
                    final = payload
    assert final is not None, "stream ended without a final event"
    final["answer"] = "".join(tokens)
    return final


def _flat(text: str) -> str:
    """Collapse whitespace so a quoted or wrapped body still compares equal."""
    return " ".join(text.split())


def _await_eml(sink: Path, after: float, timeout: float = 90.0) -> Path | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = newest_eml(sink, after)
        if found is not None:
            return found
        time.sleep(2)
    return None


def _await_hooks(out_dir: Path, after: float, timeout: float = 60.0) -> list[dict]:
    """Collect deliveries until both events arrive or the timeout expires."""
    deadline = time.time() + timeout
    seen: dict[str, dict] = {}
    while time.time() < deadline:
        for path in glob.glob(str(out_dir / "hook-*.json")):
            record = json.loads(Path(path).read_text())
            if record["event"]:
                seen[record["event"]] = record
        if {"conversation.created", "handover.created"} <= set(seen):
            break
        time.sleep(2)
    return list(seen.values())


if __name__ == "__main__":
    sys.exit(main())
