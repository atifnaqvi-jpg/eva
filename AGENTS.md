# EVA — Build Loop Charter (AGENTS.md)

You are the **EVA build loop**: the OpenHands coding agent that builds the EVA AI customer-support platform from a frozen blueprint under a strict gate constitution. EVA = multi-tenant SaaS chatbot platform (text-only, read-only, English-only MVP).

## Read order (first turn and whenever state is unclear)

1. `docs/PLAN.md` — the programme plan: requirements register, gate constitution (G0–G6), milestones, work packages, execution model, rulings status.
2. `docs/ARCHITECTURE.md` — frozen architecture (LOCKED — you may not edit it).
3. `docs/STACK.md` — locked stack and rejected alternatives (LOCKED — do not re-evaluate rejections).
4. `docs/FEATURES.md` (once WP-00 creates it) — the task registry you keep updated.
5. `docs/spec/blueprint-full.md` — full source blueprint for details not in the pruned docs.

## Gate constitution (short form; full text in docs/PLAN.md §2)

- **G0:** platform = this VM, repo `~/work/eva`. Resolved 2026-11-15.
- **G1:** `docs/ARCHITECTURE.md` and `docs/STACK.md` are checksum-locked by `tests/test_locked_docs.py`. Never edit the docs; a change requires a user ruling arriving as a pulse, which deliberately updates the recorded checksums.
- **G2:** `docs/FEATURES.md` is the only task ledger. Move only the rows the current work package names. Done = code merged + tests green + registry rows moved + commit hash reported.
- **G3 halt conditions — halt with `HALT(ruling): <question>` and stop the turn** when: a new dependency/package seems needed; a schema/API change beyond the blueprint is required; an MVP scope add/remove is suggested; a blueprint row is unimplementable or ambiguous (state both readings); an eval/test gate stays red after 3 focused attempts; anything destructive or outside the repo is needed. Halting is the designed mechanism, not failure.
- **G4:** each milestone's "Done when" is an observable gate. No WP from milestone N+1 until N's gate passes (gate review is done by the operator, not by you).
- **G5:** each pulse lists allowed paths. Report any outside-scope touch explicitly; "outside-scope touches: none" is the expected default.
- **G6:** eval/security gates block further WPs on regression.

## Standing rules

- Repo commits: conventional commits `wp-<id>: ...`, explicit `git add <files>` (never `-A`).
- No new dependencies without a ruling (G3 halt instead).
- No destructive operations. Nothing outside `~/work/eva`.
- **Authority rule:** composes from the NEWEST pulse only. Anything earlier in this conversation (including the pre-brief wandering episode) is superseded; if an earlier turn explored the filesystem or drew conclusions, discard them — verify state against `git log`, `docs/FEATURES.md`, and the newest pulse before acting.
- Report format at turn end: files changed (paths), tests run/passed counts, FEATURES rows moved (id + old→new status), outside-scope touches, commit hashes, open questions / `HALT(ruling)` if applicable.
- One pulse normally carries one WP. When a pulse grants multi-row cadence, keep going within that turn and end with a progress note before the turn budget exhausts.

## Current milestone

**M1 — Knowledge: passed** (evidence in `docs/PLAN.md` §8.5, open gaps in §8.6).
Next: M2. Work packages are tracked in `docs/PLAN.md` §7 and `docs/FEATURES.md`.

## Verifying against the live stack

The pytest suite calls the ASGI app in-process, so it structurally cannot see proxy,
container or compose wiring. Several bugs lived only in that gap, so a work package
that touches deployment is not done until it has been exercised through Caddy:

    sudo docker compose up -d --build
    curl -s http://localhost/health          # caddy -> dashboard/static
    curl -s http://localhost/api/v1/...      # caddy -> api

Lessons that cost real debugging time:

- **Never use `handle_path` for the API.** It strips the matched prefix, and the API
  mounts routes at `/api/v1/...`, so every request 404s. `handle` preserves the path.
  `tests/test_caddy_routing.py` guards this.
- **Cookies and plain HTTP.** The dev site is HTTP, so a `Secure` cookie is silently
  dropped by the browser and login appears to succeed but not persist. Set
  `SESSION_COOKIE_SECURE=true` only where TLS terminates in front.
- **The API and worker must not import torch.** Only the `embed` service holds the
  weights; everything else sets `EMBEDDING_QUERY_BACKEND=remote`. A missing setting
  surfaces as a 500 on the first FAQ, not at startup.
- **Crawl fixtures must run inside the compose network.** The worker cannot reach a
  server on the host loopback or the docker bridge gateway; run the fixture site as a
  container on the same network and use its service name.
- Prefer `docker compose exec` for probes: it proves what the container sees, not
  what the host sees.
- **Never bind a DB session at import time in a router.** The test harness reloads
  `app.db.session` to repoint the engine, so `from app.db.session import SessionLocal`
  keeps a stale engine and old credentials: the module passes alone and fails in the
  full suite. Use `from app.db import session as db_session`. This cost a whole
  debugging cycle in WP-16; the symptom was 11 widget tests failing only when run
  after other modules.
- **A security filter is invisible to tests that seed the happy path.** Every
  `bot_domains` fixture seeded `verified = true`, so dropping the `verified` filter
  broke nothing until someone read §16.1. When a query encodes a spec rule, write the
  test that violates it, not only the one that satisfies it.
- **Migrations are per-database; roles are cluster-wide.** `0001` does `CREATE ROLE`
  idempotently and `downgrade` only revokes this database's privileges, because
  dropping the role would break a sibling test database.

## Loop state (update at the end of each WP)

- **Done and CI-green**: WP-16 (widget tokens, verified domains, uploads, SEC-02 rate
  limits), WP-13 (provider resilience). Full suite 345 passed, 1 skipped.
- **M2 open rows**: `RAG-08`, `SEC-03`, `SEC-04`. M3 rows exist but G4 blocks starting
  M3 while M2 is open — WP-16 shipped M3-labelled rows (WIDG-05/14, SEC-02) that were
  unblocked because they are security/server work needing no widget frontend.
- **Blocked pending a ruling**: WIDG-01..04 (Preact + bundler are new deps, G3.1);
  RAG-08 / HALT-04 (which reranker — self-hosted needs ~1-2 GiB, hosted sends tenant
  text to a third party).

## Loop state (updated 2026-11-16, M4)

**M4 (handover) fully implemented and tested — every row in §13 now has passing
evidence; the gate itself is the operator's call.** Tested: triggers (explicit / keyword /
low-confidence / negative CSAT), contact form + `POST /handover`, `EmailChannel`,
transcript + LLM summary, AI silence after handover, business hours with an offline
banner and the contact form shown first (R43). Real email delivered to the local sink
with a real LLM summary. Suites green: backend 470 passed / 1 skipped, widget 39 passed
/ 9 skipped. The M4 gate is the operator's to call; nothing here declares it passed.

HAND-06 (signed webhooks, R44): `POST/GET/DELETE /api/v1/webhooks` + `POST /{id}/test`.
The secret is vaulted and returned once at creation; `GET` has no field that could
carry it. `conversation.created` fires after the conversation row commits, and
`handover.created` after `open_handover`'s idempotent return, so a repeat submit
pages the owner once. Delivery is off the request path. Two bugs the tests caught:
creation rejected **every hostname** (using the bare name as if it were an address),
and creation ignored `WEBHOOK_ALLOW_PRIVATE_HOSTS` while the send path honoured it.

M4 bugs found only by live browser runs (all fixed): `next_open()` 500'd the widget session
for every tenant without hours; the form never closed after a successful submit; `done`
left the spinner running; the form's layout was destroyed by a global `form{display:flex}`;
"Not now" could not dismiss the offline form; a failed submit told the visitor nothing.

**M4 gate rehearsed live, 2026-11-16** — all three clauses of PLAN §7 M4 observed end to
end through Caddy, not just in pytest: a real visitor message triggered a handover, the
staged email arrived in the sink with a real LLM summary and the full transcript, and
**two signed webhooks** (`conversation.created`, `handover.created`) reached a receiver
container, both verifying against the secret the API returned at creation. Offline mode
was observed in a browser with hours configured. This is rehearsal evidence, not a
verdict: the gate stays the operator's to call.

Rehearsal gotchas worth keeping: the receiver must be a **container on `eva_default`**
(the sandbox firewall blocks container → host, so a host-side listener times out — the
first run looked like a broken webhook and was an unreachable receiver); `docker` needs
`sudo -n` here, so any script driving it must not assume plain `docker`; and
`WEBHOOK_ALLOW_PRIVATE_HOSTS=true` was needed in `.env` for the duration and has been
removed again, because it disables the SSRF guard.

Next: the M4 gate (operator's call), then M5. The `eva-smtp-sink`
container is ad-hoc (not in `compose.yaml`) — a real SMTP
provider must be configured before anyone calls email delivery production-ready.

**M2 GATE PASSED** (`c1ae84f`, PLAN §8.16). M0/M1/M2/M3 closed — M3 (widget frontend)
is done and was closed by the operator, not by this loop; do not re-open it.

Recent commits: `c1ae84f` M2 gate report, `21099e0` WP-13 BYOK vault,
`722c881` SEC-02 rate limiting + the widget upload limiter fix, `62830ba` RAG-08,
`8e81d33` lessons, `3b61689`.

Facts worth not rediscovering:

- **`app/db/` has no `__init__.py`.** It is a namespace package, so `from app import db`
  never binds `db.session` and `importlib.reload(db.session)` raises AttributeError.
  Import `app.db.session` as a module and reload that object.
- **`tenant_session()` closes without committing.** A route that writes and returns
  without an explicit `session.commit()` replies 201 and persists nothing. Every
  mutating route commits explicitly; check this when adding one.
- **`client_ip()` validates and collapses proxy rules in one place.** Auth and admin
  each had a drifting local copy; do not reintroduce per-router copies of it.
- **The widget upload route is the one that had no rate limit.** All widget routes now
  share `_rate_limit()`. A per-route limiter is a policy the next route forgets.
- **Deploy before believing a test count.** `docker compose up -d --build api`
  (needs `sudo -n`), then exercise the route through Caddy. Three of the last four real
  bugs were invisible to the suite.
- **Migrations are applied manually**, not by an entrypoint:
  `sudo -n docker compose exec -T api alembic upgrade head`.
- **`.secrets/` holds the age identity and ciphertext**, both gitignored. Losing
  `.secrets/age-identity` loses every stored tenant provider key by design.

## Testing lessons worth keeping

- **`curl` and `TestClient` do not enforce CORS, cookies or CSP.** Anything that runs
  in a browser must be verified in a browser, on a *different origin* than the API, or
  the evidence proves nothing about the real deployment. The widget shipped four
  work packages with zero CORS and every test green.
- **A test that cannot fail is not evidence.** Mutation-check it: break the thing it
  claims to protect and confirm it goes red. Twice here an "obviously failing"
  injection silently no-op'd (string anchor absent from a minified bundle) and produced
  a false green.
- **`skip` must be honest and visible.** Optional-dependency tests (`browser.test.mjs`)
  skip with a reason and the run reports `skipped 9`, never `pass 0` pretending to be
  success. Route skips through a runtime guard: `node:test` reads the `skip` option at
  *definition* time, before `before()` can discover a missing browser.
- **When a registry row says "blocked", re-check the premise.** WIDG-16 was blocked on
  "no browser available"; `npm i playwright` + `npx playwright install chromium` took
  two minutes.
- **Verify a seed/fixture script against the real schema before trusting it.** The first
  `seed_widget_bot.py` invented `/api/v1/admin/bots` and `bot_domains.origin`; neither
  exists. `psql \d` first, then write.
- **A 201 in the log is not a working feature.** M4's contact form logged two 201s while
  the visitor saw the form still sitting there and a spinner that never stopped: the
  loader sent a bare `final` (the app never learned the handover happened) and `done`
  had no handler. Read the response body back into the UI, and drive the flow in a
  browser before calling a UI package done.
- **Check what a helper returns on the unconfigured path, not just the configured one.**
  `hours()` omits `week` when no schedule is set, and `next_open()` read it directly —
  so every tenant with default settings got a 500 on `POST /session`, i.e. the widget
  did not load at all. One test of the default case would have caught it.
- **A CSS module-level rule leaks into every component.** `.form{...}` and a global
  `form{display:flex}` fought; `hidden` also loses to any `display` declaration. The
  contact form rendered as one overlapping row and its submit button sat outside a
  380px panel. Screenshot the new state, do not infer it from passing a11y assertions.
- **Never let the receiver verify its own signature.** A listener checking HMACs with a
  secret it was handed at startup passes even when the two sides disagree — it proves the
  listener, not the sender. Recompute in the gate, from the secret the API returned once
  at creation. Same rule for any "did it sign correctly" check.
- **A gate must not relax the control it is testing.** The webhook receiver wanted port
  8099 and was refused by `ALLOWED_PORTS` (80/443/8080/8443 — correct SSRF hygiene). Moving
  the receiver to 8080 was the fix; widening the allowlist would have deleted the
  behaviour under test. Read a refusal as a constraint on the *harness* first.
- **Assert against what the system returned, not against a literal you expect.** A gate
  asserted `"3-5 business days"` and failed a correct email because the model wrote an en
  dash. Compare with the API's own answer string, normalised for whitespace. An assertion
  on model spelling or wording will flap.
- **`scripts/m*_gate.py` is the evidence format; a transcript of manual steps is not.**
  Every milestone gate here is one command that fails loudly. If the gate cannot fail,
  it is documentation, not evidence — the same mutation rule as tests.
- **An SSE event with a key on one branch and not another is a trap for the next
  consumer.** The `final` event carries `degraded` on the answered and cached paths and
  omits it entirely on the declined path, so `turn["degraded"]` raises precisely on a
  refusal. The widget protocol does not read it, so nothing is broken today — but check
  the shape on *every* branch before asserting on a field.
- **A feature can be `done` and still unprovable without configuration.** The handover
  email was silent because the tenant had no `settings.handover.email`, leaving the row
  `recorded` with a NULL outcome. That is designed behaviour, not a bug, but it means
  the gate has to configure an owner address — and that no owner can yet.

## Confidentiality (standing — binds every turn, every future repository)

You NEVER upload, commit, or push anything confidential to ANY GitHub repository or remote — not just this one. Forbidden in commits, on any branch, at any time:

- API keys, tokens, passwords, private keys, provider credentials, session secrets.
- Customer or visitor data, real personal information (names, emails, phone numbers, addresses).
- Machine-internal topology: IP addresses, tailnet addresses, hostnames, SSH key paths, console URLs.
- Conversation events, agent transcripts, `.env` files, database dumps.

Secrets reach runtime only through `.env` (gitignored; placeholders only in `.env.example`) or the system secret store. If you are unsure whether something is confidential: do not commit it, flag it in your report under "confidentiality check", and halt with `HALT(ruling)`. Pushes go only to remotes you were given in a pulse.
