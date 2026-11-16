"""M1 gate: prove the knowledge pipeline end to end against the live stack.

Everything goes through Caddy over HTTP, then the real arq worker and the real
BGE-M3 embed service. Nothing is stubbed. All fixtures are synthetic, per the
confidentiality charter: a generated PDF, a two-page site served from a scratch
container, and one FAQ pair.

Usage (stack already up):

    sudo docker compose up -d --build
    python scripts/m1_gate.py

Why the fixture site runs in a container: the crawl is performed by the worker,
which lives on the compose network and cannot reach a server bound to the host's
loopback or to the docker bridge gateway. So the site is started as a container
on the same network and addressed by container name.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://localhost"
FIXTURE_NAME = "eva_m1_fixture"
FIXTURE_PORT = 8899
PAGES = {
    "/": (
        "<html><body><h1>Shipping Policy</h1>"
        "<p>Standard delivery takes three to five business days.</p>"
        "<p>Orders above fifty dollars ship free.</p>"
        "<a href='/warranty'>Warranty</a></body></html>"
    ),
    "/warranty": (
        "<html><body><h1>Warranty</h1>"
        "<p>All electronics carry a twelve month warranty.</p></body></html>"
    ),
}


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run docker, adding sudo only if the socket is not reachable directly."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )
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
    """The network the running worker is attached to."""
    containers = docker("ps", "--format", "{{.Names}}").stdout.split()
    worker = next((c for c in containers if c.endswith("-worker-1")), None)
    if worker is None:
        raise SystemExit("no running worker container; start the stack first")
    return docker(
        "inspect", worker,
        "--format", "{{range $name, $net := .NetworkSettings.Networks}}{{$name}}{{end}}",
    ).stdout.strip()


def start_fixture(network: str) -> None:
    site = Path(tempfile.mkdtemp(prefix="eva-m1-site-"))
    (site / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    (site / "warranty").mkdir()
    for path, body in PAGES.items():
        target = site / ("index.html" if path == "/" else path.lstrip("/") + "/index.html")
        target.write_text(body)

    docker("rm", "-f", FIXTURE_NAME, check=False)
    docker(
        "run", "-d", "--name", FIXTURE_NAME, "--network", network,
        "-v", f"{site}:/srv:ro", "-w", "/srv",
        "python:3.12-slim",
        "python", "-m", "http.server", str(FIXTURE_PORT), "--bind", "0.0.0.0",
    )


def stop_fixture() -> None:
    docker("rm", "-f", FIXTURE_NAME, check=False)


def make_pdf(lines: list[str]) -> bytes:
    """Minimal single-page PDF with real text operators (no dependency)."""
    text = "BT /F1 12 Tf 40 780 Td 16 TL\n"
    for line in lines:
        escaped = line.replace("(", r"\(").replace(")", r"\)")
        text += f"({escaped}) Tj T*\n"
    text += "ET"
    stream = text.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def bot_id_for(email: str) -> str:
    """Look the tenant's bot up in the database.

    No read endpoint exposes the bot id yet (PLAN 8.6), so the gate reads it
    directly rather than inventing an API surface here.
    """
    sql = (
        "SELECT b.id FROM bots b"
        " JOIN tenants t ON t.id = b.tenant_id"
        " JOIN users u ON u.tenant_id = t.id"
        f" WHERE u.email = '{email}'"
    )
    user = os.environ.get("POSTGRES_USER", "eva")
    database = os.environ.get("POSTGRES_DB", "eva")
    return docker(
        "compose", "exec", "-T", "postgres",
        "psql", "-U", user, "-d", database, "-tAc", sql,
    ).stdout.strip()


def preview(client: httpx.Client, question: str) -> list[dict]:
    response = client.post("/api/v1/knowledge/preview", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()["hits"]


def show(label: str, hits: list[dict]) -> None:
    print(f"    {label}: {len(hits)} hits")
    for hit in hits[:3]:
        print(f"      {hit['score']:.3f}  {hit['text'][:66]!r}")


def main() -> int:
    network = compose_network()
    start_fixture(network)
    time.sleep(3)
    try:
        return run()
    finally:
        stop_fixture()


def run() -> int:
    client = httpx.Client(base_url=BASE, timeout=180.0)
    email = f"m1-{uuid.uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery"

    created = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "tenant_name": "M1 Gate"},
    )
    assert created.status_code == 201, created.text
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    print(f"[1] registered and logged in: {email}")

    pdf = make_pdf([
        "Refund Policy",
        "Customers may request a refund within thirty days of purchase.",
        "Refunds are issued to the original payment method within five business days.",
        "Shipping and handling charges are not refundable.",
    ])
    upload = client.post("/api/v1/knowledge/uploads?filename=refunds.pdf", content=pdf)
    assert upload.status_code == 201, upload.text
    print(f"[2] uploaded synthetic PDF -> version {upload.json()['version_id']}")

    crawl = client.post(
        "/api/v1/knowledge/crawls",
        json={"url": f"http://{FIXTURE_NAME}:{FIXTURE_PORT}/", "schedule": "weekly"},
    )
    assert crawl.status_code == 202, crawl.text
    print(f"[3] crawl scheduled for {sorted(PAGES)}")

    faq = client.post(
        "/api/v1/knowledge/faqs",
        json={
            "question": "Do you offer a student discount?",
            "answer": "Yes, fifteen percent with a valid student ID.",
        },
    )
    assert faq.status_code == 201, faq.text
    faq_version = faq.json()["version_id"]
    print(f"[4] FAQ created -> version {faq_version} (embedded synchronously)")

    states: dict[str, str] = {}
    deadline = time.time() + 300
    while time.time() < deadline:
        states = {
            d["title"]: d["status"]
            for d in client.get("/api/v1/knowledge/documents").json()
        }
        if len(states) >= 4 and not (set(states.values()) & {"pending", "processing"}):
            break
        time.sleep(5)
    else:
        print(f"TIMEOUT waiting for ingestion; states={states}")
        return 1
    print(f"[5] ingestion settled: {json.dumps(states)}")

    failed = {title: state for title, state in states.items() if state == "failed"}
    assert not failed, f"ingestion failed for {failed}"

    pdf_hits = preview(client, "How long do refunds take?")
    show("preview (PDF question)", pdf_hits)
    assert any("five business days" in h["text"] for h in pdf_hits)
    assert any("thirty days" in h["text"] for h in pdf_hits)

    crawl_hits = preview(client, "What warranty do electronics have?")
    show("preview (crawl question)", crawl_hits)
    assert crawl_hits[0]["text"].startswith("Warranty"), (
        "the crawled warranty page should rank first for its own question"
    )

    faq_hits = preview(client, "student discount")
    show("preview (FAQ question)", faq_hits)
    assert any("fifteen percent" in h["text"] for h in faq_hits)
    print("[6] all three sources retrievable through the real model")

    bot_id = bot_id_for(email)
    assert bot_id, "registration did not create a bot"

    second_pdf = make_pdf([
        "Refund Policy",
        "Refunds are now processed within two business days.",
    ])
    newer = client.post(
        "/api/v1/knowledge/uploads?filename=refunds-v2.pdf", content=second_pdf
    )
    assert newer.status_code == 201, newer.text
    second_version = newer.json()["version_id"]

    deadline = time.time() + 180
    while time.time() < deadline:
        by_version = {
            d["version_id"]: d["status"]
            for d in client.get("/api/v1/knowledge/documents").json()
        }
        if by_version.get(second_version) == "ready":
            break
        time.sleep(3)

    for label, path, version in (
        ("publish newer upload", "/api/v1/knowledge/publish", second_version),
        ("rollback to FAQ", "/api/v1/knowledge/rollback", faq_version),
    ):
        response = client.post(path, json={"bot_id": bot_id, "version_id": version})
        assert response.status_code == 200, response.text
        assert response.json()["active_knowledge_version_id"] == version
        print(f"[7] {label} -> {version}")

    unknown = client.post(
        "/api/v1/knowledge/publish",
        json={"bot_id": str(uuid.uuid4()), "version_id": faq_version},
    )
    assert unknown.status_code == 404, "publishing to an unknown bot must 404"
    print("[8] publish to an unknown bot is rejected")

    print("\nM1 GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
