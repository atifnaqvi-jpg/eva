"""Seed a synthetic tenant and bot for the widget browser matrix.

The browser tests need a real published bot with a verified origin, because the point
is to exercise the session origin check rather than bypass it. Everything here is
synthetic, per the confidentiality charter.

**Why this writes the database directly:** there is no bot-settings endpoint yet
(ADMN-03 is M5) and no bot-domains endpoint at all, so `tests/test_widget_tokens.py`
seeds domains the same way. Registration *does* create the tenant's bot, so only the
domain row is inserted by hand.

Usage, with the stack up:

    python scripts/seed_widget_bot.py --origin http://localhost

Prints the public id on stdout, which is what the browser job needs. Pass the scheme
explicitly: `origin_matches` normalises a bare host to `https://`, so `localhost`
alone would not match a plain-HTTP dev stack.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

import httpx
import psycopg

BASE = "http://localhost"
API_DIR = Path(__file__).resolve().parents[1] / "backend" / "api"


def _admin_dsn(dbname: str) -> str:
    host = os.environ.get("TEST_PG_HOST", "127.0.0.1")
    port = os.environ.get("TEST_PG_PORT", "5432")
    user = os.environ.get("TEST_PG_USER", "eva")
    password = os.environ.get("TEST_PG_PASSWORD", "changeme")
    return f"host={host} port={port} user={user} password={password} dbname={dbname}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--origin", default="http://localhost")
    parser.add_argument("--db", default=os.environ.get("EVA_DB", "eva"))
    args = parser.parse_args()

    email = f"widget-{uuid.uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery"
    tenant_name = f"Widget Matrix {uuid.uuid4().hex[:6]}"

    with httpx.Client(base_url=args.base, timeout=120.0) as client:
        created = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "tenant_name": tenant_name},
        )
        if created.status_code != 201:
            raise SystemExit(f"register failed: {created.status_code} {created.text}")

        logged_in = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if logged_in.status_code != 200:
            raise SystemExit(f"login failed: {logged_in.status_code} {logged_in.text}")

        # One synthetic FAQ so the bot can actually answer; without a published
        # knowledge version every turn degrades to the handover message.
        faq = client.post(
            "/api/v1/knowledge/faqs",
            json={
                "question": "What is the return window?",
                "answer": "Unopened items can be returned within 30 days.",
            },
        )
        if faq.status_code != 201:
            raise SystemExit(f"faq failed: {faq.status_code} {faq.text}")
        version_id = faq.json()["version_id"]

    with psycopg.connect(_admin_dsn(args.db)) as conn:
        row = conn.execute(
            """
            SELECT b.id::text, b.public_id::text, b.tenant_id::text
              FROM bots b
              JOIN tenants t ON t.id = b.tenant_id
             WHERE t.name = %s
             ORDER BY b.created_at DESC
             LIMIT 1
            """,
            (tenant_name,),
        ).fetchone()
        if row is None:
            raise SystemExit("no bot was created for the new tenant")
        bot_id, public_id, tenant_id = row

        conn.execute(
            """
            INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)
            VALUES (%s, %s, %s, true)
            ON CONFLICT (bot_id, domain) DO NOTHING
            """,
            (tenant_id, bot_id, args.origin),
        )
        conn.commit()

    # Publish after the bot id is known: the endpoint needs both ids.
    with httpx.Client(base_url=args.base, timeout=120.0) as client:
        client.post("/api/v1/auth/login", json={"email": email, "password": password})
        published = client.post(
            "/api/v1/knowledge/publish",
            json={"bot_id": bot_id, "version_id": version_id},
        )
        if published.status_code not in (200, 201, 202):
            raise SystemExit(
                f"publish failed: {published.status_code} {published.text}"
            )

    print(public_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
