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

## Loop state (updated 2026-11-17, M6 / WP-25)

**SEC-11 is done** (`c50809c`, CI green on both jobs at `923f931` run 44146696622 and
`8ab2168` run 44147925055) — privacy-notice template and DPA skeleton, registry row
`done (WP-25)`, reasoning in `docs/PLAN.md` §8.27. Read a *named* sha there, never "head":
the row was rewritten three times in one session because each citation went stale.

**The documents are generated, not written.** `app/compliance/facts.py` reads the
sub-processor list from `providers.PROFILES`, the retention window from
`retention.policy`, the purge schedule from `WorkerSettings.cron_jobs`, the prompt and
upload limits from `prompt`/`uploads`, and the data inventory from the migrations;
`app/compliance/render.py` emits `docs/compliance/*.md`. Regenerate with
`cd backend/api && python -m app.compliance.render`; `tests/test_compliance_docs.py`
fails if the files and the code disagree. Hand-editing a document is a red test, by
design.

**The asymmetry to remember.** `vault.ALLOWED_PROVIDERS` has four names but only
`PROFILES` can receive a prompt. `openai`/`anthropic` are storable and unusable, so
naming them as sub-processors would *overstate* the chain. A test asserts both
directions.

**Claims removed because the code contradicted them**, each of which a hand-written
template would have shipped: the dashboard has **no delete action** (export is a link,
erasure is API-only, so a subject request is operator-mediated); visitor attachment
bytes are **discarded, not stored** (`visitor_files` is metadata only), so nobody can
look at the file; the **database volume is not encrypted by the application** —
BYOK keys are, the volume is the operator's; there is **no change-password route**,
so nothing revokes sessions on one; the purge's **03:15 is the worker's local clock**, not
UTC — `arq` reads the container timezone and the image sets no `TZ`; and **only the export
is self-service** — erasure is an API call the operator makes, with no dashboard control.
That last one survived in DPA **§4** after being fixed in the notice — a contradiction
between two documents in the same work package — and it now has its own test,
`test_dpa_does_not_call_erasure_self_service`. The HALT-04 ruling also turned "the stack
contains no reranker" into a false premise while leaving the conclusion true: the reranker
is self-hosted, so it is still not a sub-processor, but the reason had to be rewritten.

**17 `[[FILL: ...]]` markers** are the operator's before publication: party names and
addresses, breach window, D3 chain position, governing law, liability, hosting and
per-provider terms/regions, database-at-rest, the hosting region, backup retention,
effective date. The DPA
explicitly does **not** claim a backup regime — no restore drill exists yet.

**The WIDG-11 Escape flake is confirmed many times; the gate is roughly half red.**
Red so far: `825fd9d`, `b660863`, `f931b52`, `55bcbaa`, `5f04724`, `4e6b6e6`, `a70d490`,
`f72a2a9`; green: `1ba051f`, `c50809c`, `8a8aa92`, `c80afee`, `b991d6e`, `e6bf90f`,
`9ed4b0e`, `217fcce`, `923f931`, `8ab2168`. Every one at the same
`backend/api/widget` subtree `7ed153c`, with `test` green on all of them. Do not trust a
count in this file either — check `git rev-parse <sha>:backend/api/widget` and re-read the
run page. The panel closes and reopens one tick later
(`expanded=["false@595","true@596"]`). It is a widget defect, still unfixed, and it
makes roughly half of all pushes look red. Do not re-diagnose it from scratch: read
PLAN §8.26 and §8.27, then fix `loader.ts:484` / `app.tsx:384`. **It is now the single
biggest source of false red here, and it deserves its own row.**

**M6 still open**: `SEC-12` (CCPA/CPRA — the notice says so and does not pre-empt it),
`SEC-13`, `OPS-01..05`, `OPS-08`, and the rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-17, M6 / WP-23)

**SEC-10 is done** (`514d4df`, CI green on both jobs; the implementation landed in
`825fd9d`). Data-subject rights now have both halves of §19.3: on-demand erasure of a
conversation and the visitor identifiers it holds, and a SAR export that names everything
erasure removes. Registry row: `done (WP-23)`; the full reasoning, including the mutation
that caught nothing, is in `docs/PLAN.md` §8.26.

**The fact worth not rediscovering.** `DELETE /api/v1/conversations/{id}` is an *additive*
route: §17.5 lists no DELETE, but it adds a new path under an existing resource with no
shape or schema change, and §17.3 already carries an upload deviation of the same kind. It
is written up as a deviation rather than assumed to be free. If the operator prefers
`POST /conversations/{id}/erase`, that is a contract call, not an implementation detail.

**The trap that shaped the code.** Erasure is auditable *or* complete, not both by the
obvious means: `audit_logs` is append-only and never purged, so logging the erased record
would deposit the subject's name, address, session id and page URL in the one table
nothing can ever delete — the erasure undone by its own receipt. The audit detail therefore
carries counts and the id, and a test asserts the four identifier values are absent from it.

**The export was quietly incomplete.** `visitor_session_id` and the `visitor_files` list
were missing from both formats; `format_version` is now 2. Anyone changing the export must
keep it a complete answer to "what do you hold about me", because it *is* the SAR mechanism.

**RLS is what makes erasure tenant-safe, not the predicate.** Removing
`AND tenant_id = :tenant_id` from the `DELETE` breaks no test — RLS refuses the row. Same
finding WP-19 recorded for the read path. Keep the predicate as defence in depth, but do
not cite the isolation test as proof that the application layer is the guard.

**A flaky gate now has proof, not a theory.** `widget-browser` failed on `825fd9d` and
passed on `1ba051f` with a byte-identical widget subtree (`7ed153c`) — the WIDG-11 Escape
case is intermittent. The annotation narrows it: the panel closes and then **reopens one
tick later**, consistent with the app frame's `ready` post (`app.tsx:384`) racing the
loader's `case "ready": void openChat()` (`loader.ts:484`). Unfixed and out of scope for
SEC-10; it will bite again.

**M5's gate is still the operator's to call** (PLAN §8.22); nothing here touches it.

## Loop state (updated 2026-11-17, M5 / WP-19) — HISTORICAL, superseded by the M6 block above

**M4 is CLOSED — operator ruled PASSED 2026-11-17 05:10 UTC** on the evidence in
`docs/PLAN.md` §8.20, with clause 1's "real conversation trigger" narrowed to a real
widget session. M5 entry authorised in the same pulse.

**M5 / WP-19 in progress: dashboard conversations, export and analytics.**
Backend done: `GET /api/v1/conversations` (filters + pagination), `/{id}` (summary +
transcript + turns), `/{id}/export?format=txt|json`, `/api/v1/analytics/summary`.
Screens done: `/conversations`, `/conversations/[id]`, `/analytics`, nav shell.
Registry: ADMN-06/07/08/09 `done`; ADMN-01/02 `partial`.

Also done in WP-19: `/login` (there was no way to get a session at all), and ADMN-03
bot configuration — `GET /bots`, `GET/PATCH /bots/{id}`, `/bot`. Registry: ADMN-01/02
still `partial`, ADMN-03/06/07/08/09 `done`. Remaining M5: ADMN-04/05 knowledge admin,
ADMN-10 settings, ADMN-11 onboarding, ADMN-12 embed snippet, ADMN-02 home.

Suites green, measured with CI's own commands: backend **495 passed / 2 skipped**
(`pytest --ignore=tests/test_eval_gate.py`) — 481 after the first WP-19 slice plus 14 for
bot config — and eval gate **6 passed**. Dashboard `tsc --noEmit` clean, `next build` 8
routes, `npm test` **7 passed**. Widget unchanged at 39 passed / 9 skipped (not re-run).

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

**M4 gate rehearsed live, 2026-11-16, and made reproducible 2026-11-17.** All three clauses
of PLAN §7 M4 observed end to end through Caddy; `scripts/m4_gate.py` now does it in one
command and exits non-zero if any clause stops holding. Report and caveats: PLAN §8.20.
Still rehearsal evidence, not a verdict: the gate stays the operator's to call.

Rehearsal gotchas worth keeping: the receiver must be a **container on `eva_default`**
(the sandbox firewall blocks container → host, so a host-side listener times out — the
first run looked like a broken webhook and was an unreachable receiver); `docker` needs
`sudo -n` here, so any script driving it must not assume plain `docker`; and
`WEBHOOK_ALLOW_PRIVATE_HOSTS=true` was needed in `.env` for the duration and has been
removed again, because it disables the SSRF guard.

Next: **nothing is startable without a ruling.** The M4 gate is the operator's to call,
and G4 blocks every M5 WP (WP-19 onward) until it passes — WP-19 is M5 work, not a
carry-over that can be started on the agent's own authority. The `eva-smtp-sink`
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

### Widget cannot serve a new tenant (2026-11-17, HALT-06)

`bot_domains` has no writer and `POST /api/v1/bots/{id}/domains` (§17.4) is unimplemented,
so `POST /widget/session` 403s for every tenant created since the schema — the widget
renders, then says "this widget has no allowed domains configured". To exercise the widget
end-to-end in dev, insert a verified domain directly:

```sql
INSERT INTO bot_domains (tenant_id, bot_id, domain, verified)
VALUES ('<tenant>', '<bot>', 'http://127.0.0.1:8099', true);
```

The stored value must **match the host page's origin exactly**, scheme and port
included. `tokens.origin_matches` normalises a bare entry as `https://<entry>`, so a row
of `localhost` does not match a page served over `http://...:port` — the widget loads and
then reports "this domain is not allowed to use this widget".

Also note `LLM_PROVIDER` is unset in the dev stack (`config.py` defaults it to
`unconfigured`), so a real chat turn returns "Sorry, something went wrong" — that is the
environment, not a regression. Session minting and the domain check can still be verified
with `POST /api/v1/widget/session {"bot_public_id": ...}` plus an `Origin` header.

Playwright for browser checks lives in `backend/api/widget/node_modules` (Python module is
not installed). Scripts must be placed inside that directory to resolve the import.

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
- **Never write a test count you did not run in that session.** The M4 report first said
  "471 passed", which was 465 (the main suite, `--ignore=tests/test_eval_gate.py`) plus 6
  (the eval gate, a separate CI step) added together and presented as one run. G4 asks
  for counts precisely because they are checkable; a plausible number is worse than an
  absent one. Run it, then paste it.
- **`localhost:8000` in this sandbox is the agent-canvas ingress, not the EVA stack.**
  Caddy publishes on **port 80**. Probing `:8000` returned `200` for dashboard paths and
  `{"detail":"Not Found"}` for API paths, and the 200s were the canvas frontend — a
  convincing-looking green probe of the wrong service. Before any live check, get the
  port from `docker compose ps`, and sanity-probe a route you did *not* change: if that
  also fails, the stack is not the thing you are talking to.
- **`tenant_session` closes without committing.** A route that writes and forgets
  `session.commit()` returns a perfectly good 200 and persists nothing. Every write test
  must re-read through a *second* request; asserting on the mutation's own response body
  cannot catch this.
- **A test client's cookie follows the last login.** `_register`/`_login` switch the
  session cookie, so a cross-tenant test written as "register A, register B, probe as A"
  is silently probing as B and passes for the wrong reason. Capture both tokens and
  `_as()` explicitly before each call.
- **Read `pending`/`recorded` in the `handovers` table before diagnosing them.** Both look
  like failure. `pending` means a trigger fired and the widget is still showing the
  contact form; `recorded` means deliberately not sent. The real gap is that a delivery
  which *throws* also leaves `pending`, so the two states are indistinguishable in SQL.

## Confidentiality (standing — binds every turn, every future repository)

You NEVER upload, commit, or push anything confidential to ANY GitHub repository or remote — not just this one. Forbidden in commits, on any branch, at any time:

- API keys, tokens, passwords, private keys, provider credentials, session secrets.
- Customer or visitor data, real personal information (names, emails, phone numbers, addresses).
- Machine-internal topology: IP addresses, tailnet addresses, hostnames, SSH key paths, console URLs.
- Conversation events, agent transcripts, `.env` files, database dumps.

Secrets reach runtime only through `.env` (gitignored; placeholders only in `.env.example`) or the system secret store. If you are unsure whether something is confidential: do not commit it, flag it in your report under "confidentiality check", and halt with `HALT(ruling)`. Pushes go only to remotes you were given in a pulse.
