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

## Loop state (updated 2026-11-19, M5 / WP-43) — OPS-05, a threshold the live stack corrected

**OPS-05 is done** (code `0f7be76`). Registry row `backlog` → `done`; `ADMN-08`'s misnamed
field corrected in the same commit. `backend/api/app/analytics/gaps.py`,
`tests/test_knowledge_gaps.py` (20 tests). Backend **1029 passed / 3 skipped** (1009/3
baseline, measured before any edit), eval gate 6 passed, security scan clean (9 baselined,
0 open). Reasoning in PLAN §8.44.

**The field was named after something it did not measure.** `top_unanswered_questions`
ranked visitor messages by exact-text count, and the most-asked question in a support chat is
usually the best-answered one. `/summary` now returns `top_questions` (a frequency list,
honestly named) and the declines live at `GET /api/v1/analytics/knowledge-gaps`. Do not fold
them back into one field.

**A gap is `confidence < bots.confidence_threshold`**, read from the column the turn loop
already writes — no new column, no flag, no schema change, no migration. The
`confidence IS NULL` paths (injection refusal, handover offer, provider outage) are excluded:
counting them would report safety refusals as content gaps.

**`CLUSTER_SIMILARITY = 0.68` was set by the live stack, not by reasoning.** The first draft's
0.62 came from four paraphrase pairs and then merged "do you offer a warranty" into "what is
your refund policy" on a real tenant through Caddy. A 15-pair probe: same-topic **0.694–0.803**,
different-topic **0.432–0.674** — 0.62 was inside the wrong band. Two tests pin both sides
(`> 0.6736`, `< 0.6942`). **The margin is only ~0.02** and the module says so: BGE-M3 puts
short support questions in a narrow band, so adjacent-topic merges and same-topic splits will
occur on real traffic. The counts and phrasings are the actionable part; the label is
illustrative.

**"Could not cluster" is not "no gaps"** — §8.38/§8.41's rule on an analytics field. An
embedder that raises returns `clustered: false` with the turns still listed plus a note; never
`[]`, never a 500. The empty path never calls the embedder at all (no torch in the API image).

**13 mutations, 13 red**, and the two that started green were real: dropping the `tenant_id`
predicate leaves the suite green because **RLS refuses first** (WP-19/23/34 again — the
predicate is defence in depth, not the guard), so a test now asserts the policy *and*
`FORCE ROW LEVEL SECURITY` on all three tables read plus the positive direction, since a
forced-RLS table with no policy denies everything and would make the isolation test vacuous;
and the window test first aged only the replies, leaving the `LATERAL` join nothing to pair.

**Two false claims found on that screen are reported, not fixed.** `answered_turn_rate` reads
100% while **384 of 473** live assistant turns are below the floor, because `counts.answered`
is `confidence IS NOT NULL` and the decline path writes a real score — so a decline counts as
an answer and the field's own definition is false. That is ADMN-08's pre-existing defect with
no registry row of its own; fixing it needs the bot's floor joined into a query that lacks it,
and `tests/test_conversation_archive.py:510` asserts today's arithmetic. Recorded on the
ADMN-08 row.

**CI on `0f7be76` (run 44518767004): `test` success, `security` success, `widget-browser`
failed at step 8 "Install browsers"** — `security.ubuntu.com` returned **503** for six
packages, `playwright install failed for chromium after 3 attempts`, so steps 9–10 (migrate,
matrix) were **skipped**. That is **not** the WIDG-11 signature and the widget subtree is
byte-identical `7ed153c` at `0f7be76` and `cadf91f`. An upstream mirror outage, not this
change; not re-run (no rerun-capable token here). **When `widget-browser` is red, read the
failing step first** — the flake lives in step 10, and a step-8 failure is infrastructure.

**M5 still open**: `ADMN-05` (its own Chromium gate) and `ADMN-10`'s domain control;
`ADMN-01`/`ADMN-02` partial. M6 unchanged by this WP.

## Loop state (updated 2026-11-18, M5 / WP-40) — SEC-15, the writer that `bot_domains` never had

**SEC-15 is done** (code+docs `771ae2a`). Registry row `backlog` → `done`. Reasoning in PLAN
§8.41. `backend/api/app/widget/domains.py`, `backend/api/app/botconfig/domains_router.py`,
`tests/test_domain_verification.py` (41 tests). Backend **991 passed / 3 skipped** (950/3
baseline), eval gate 6 passed, security scan clean (9 baselined, 0 open). **No schema change,
no migration, no new dependency.**

**The widget could not serve a single new tenant, and this row is why.** `bot_domains` had no
writer anywhere in the codebase: `POST /widget/session` answered 403 `no_allowed_domains` for
every origin, so the snippet the dashboard hands out could never work. Only tenants seeded by
hand (AGENTS.md's HALT-06 SQL) had a working widget.

**Add and verify are two endpoints, and merging them is the defect.** §17.2 says one route,
"Add + verify allowed domain", which hides two operations with different trust. Adding stores
the row **unverified** with a fresh 128-bit token; verifying is an explicit action that finds
`eva-verify=<token>` at `_eva-verify.<domain>` and only then flips the flag. HALT-06's option 1
("self-assert on add") is the cheap reading that makes `verified` decorative, and a combined
route is how it would return. Nothing verifies on add, on a timer or on a read.

**Assert the widget, not the row.** The `verified` column is never the assertion on its own —
the origin is driven through `POST /widget/session` and is 403 after the add, 200 after the
verify, 403 again after the delete. A test on the row would stay green if
`_allowed_domains` stopped filtering; this one cannot. That is WP-16's `verified` lesson, and
it is the reason this file keeps repeating it.

**No `dnspython`, because adding it would have been a G3.1 halt.** Measured: `import dns`
fails in the api image, `dig`/`nslookup`/`host` are absent, and the only mention of it in the
installed closure is a `psycopg` *dev* extra. So `domains.py` speaks DNS with `socket` and
`struct` — one question, one record type, ~100 lines — and the tests drive it against a

**real UDP server**, not a stub at `query_txt`. The packet builder, the length-prefixed TXT
reader and the compression-pointer guard are the parts that break, and a stub above them
proves none of them.

**"Could not ask" is not "no record", in a third place now.** §8.38 was a metrics graph and
this is a customer's DNS panel: returning `[]` for a truncated reply tells an owner to fix a
record that is already correct. So TC, SERVFAIL, a mismatched query id, a forward-pointing
compression pointer, an unreachable resolver and a silent one each raise `lookup_failed` →

**503**, while NXDOMAIN/NODATA is an honest 200 carrying the name and value to publish. Four
of the twelve mutations attack that boundary; all four are red.

**The SSRF guard is on the name, because the name is tenant-controlled.** We never connect to
the tenant's host — the reason TXT beat `/.well-known/`, which would have added a server-side
fetch of a tenant-chosen URL to §21.2's class. What remains is an existence oracle for
internal names, so IPs, single-label hosts and `.local/.localhost/.localdomain/.internal/
.intranet/.lan/.home/.corp/.arpa/.onion` are refused before a packet is built.
`.test/.example/.invalid` stay allowed: RFC 2606 guarantees they name no internal service, and
allowing them is what lets the suite use the real UDP path.

**One mutation stayed green and it was a real gap.** Dropping `AND bot_id = :bot_id` left all
40 tests passing: RLS scopes by *tenant* and the MVP creates one bot per tenant, so a
cross-bot leak is invisible. A sibling bot is now seeded explicitly and asserted to see
nothing and to be unable to verify or delete the first bot's row. 12/12 red afterwards. When a
predicate is added for a rule the schema cannot enforce, write the test that violates it.

**A bug the tests found: one bad legacy row 500'd the whole listing.** The dev seed writes
`http://127.0.0.1:8099` straight into `bot_domains` and `record_name` refuses an IP by design,
so `_summary` raised for every listing on that bot. The row is now listed with
`record_name: ""` and the sibling domains still appear — naming the record is a display
concern, not a reason to hide the table.

**Verified live through Caddy, not only in-process.** Register → add → `verified: false` →
widget 403 → a **fixture resolver container on `eva_default`** answering the record (a
host-side listener is unreachable: the sandbox firewall blocks container→host, M4's lesson
again) → verify 200 `the record matched` → widget 200 on that origin → **403 on
`https://evil.example.com`**. Probe rows and the container were removed; `.env` was restored
byte-for-byte (the `EVA_DNS_RESOLVER` override was a temporary append).

**Two false sentences were corrected rather than left standing.** The settings screen
(ADMN-10) and the onboarding checklist both said no verification mechanism had been chosen and
that the widget refuses every origin. Both are now false, so both describe the TXT mechanism
and say plainly that the add/verify *control* is still missing. Do not build that control in
this row's name: it is ADMN-10's.

**CI on `771ae2a` (run 44415254889): `test` success, `security` success, `widget-browser`
red** at step 10 "Browser matrix", annotation naming **WIDG-11** outright
(`expanded=["false@594","true@595"]`), widget subtree byte-identical at `7ed153c` on this sha
and its parent `0a831eb`. The known flake, not this change; the defect is still unfixed and
still owned by the widget row. Not re-run to chase a green.

**`d1effed` (run 44420954115) is green on all three jobs** — including
`widget-browser`. That sha differs from `771ae2a` only in docs and the deletion of one
unused test helper (`git diff --stat 771ae2a d1effed -- backend/` is empty, widget subtree
`7ed153c` on both), so the identical backend passed the matrix on the next sample. That is
the flake's ~50% rate, **not** evidence WIDG-11 is fixed.

**Not claimed**: no UI (ADMN-10), no IDN (a Unicode name is refused with "publish the
punycode form" rather than guessed), no CAA, no re-verification sweep, no wildcard handling.

**M5 still open**: `ADMN-05` (its own browser gate) and `ADMN-10`'s domain control. M6 is
unchanged by this WP.

## Loop state (updated 2026-11-18, M5 / WP-39) — RAG-11, the pointer that had to stop being one

**RAG-11 is done** (code `9f6322e`; CI run **44389096503 green on all three jobs** —
`test` 1m34s, `security`, `widget-browser`; the WIDG-11 flake did not fire and that defect
is still unfixed). Registry row `backlog` → `done`; `ADMN-05` moved `blocked (HALT-07)` →
`partial (unblocked by RAG-11)` because the ruling removed the product blocker but its own
Chromium gate has not been re-run. Migration `0008_knowledge_sets`,
`tests/test_knowledge_sets.py` (11 tests). Backend **950 passed / 3 skipped** (935/3
baseline, measured before any edit), eval gate 6 passed, security scan clean (9 baselined,
0 open). Reasoning in PLAN §8.40.

**HALT-07's OPTION 2 is now the implementation, not a plan.** §8.22 measured the defect on
a live stack: with one `active_knowledge_version_id`, publishing a second document made the
first unreachable — a shipping question declined at best 0.4367 against a 0.55 floor while
the content sat indexed in the same tenant. A per-version `published` flag would not have
fixed it (a flag cannot say *"these five, not those five"*, and a diff of flags has no
atomic swap), which is why the ruling chose snapshot sets. **Do not "simplify" this back to
a column** — §10.5's single-row pointer swap survives because the pointer is one column,
not because it points at one version.

**Publishing is additive at the service layer, not just in the UI.** A publish inherits the
live members, adds the named version, and keeps **the newest version per document**. The
collapse is correctness, not tidiness: a plain union serves the superseded and superseding
version of one document side by side, so an owner who corrects a page is answered from the
text they replaced. `/rollback` is deliberately **not** additive — one document, one pointer
swap.

**The retired column was cleared, not just stopped being read.** `0008` wraps every existing
`active_knowledge_version_id` in a one-member set, re-points the bot, then NULLs it. The
column stays because §16.1 lists it and the ruling migrates rather than drops. A test writes
an unrelated version into it and asserts the served scope does not change — **two pointers
that can disagree is the bug being replaced**, and the next reader would reintroduce it.

**The mutation that stayed green, recorded not papered over.** Dropping
`AND m.tenant_id = :tenant_id` from the membership query leaves the suite green: RLS refuses
the row first. Same finding as WP-19, WP-23, WP-34 — the predicate is defence in depth, not
the guard. The isolation test now asserts the policy in **both** directions as `eva_app`:
the foreign set is invisible **and** the tenant's own set is visible. The negative half
alone is vacuous — a table with RLS forced and no policy denies *everything*, so deleting
the policy kept the test green until the positive assertion existed. Reading through `eva`
(the admin role) proves nothing at all: it is a superuser with BYPASSRLS.

**`knowledge_set_members.tenant_id` is not in the ruling's column list and was added.** Every
tenant-owned table in §16.1 carries one and is fail-closed under RLS (§16.3). A member table
scoped only by `set_id` would be the only tenant table with no policy of its own, and a lost
predicate there is a query that *succeeds and returns another tenant's knowledge*.

**API shape changed on existing paths, written up rather than assumed free.** Publish and
rollback now return `active_knowledge_set_id` + `active_knowledge_version_ids`; the old
`active_knowledge_version_id` is gone from those bodies because a set cannot honestly answer
it. `GET /bots[/{id}]` gained both fields and **kept** the single id as a one-version *view*
(null when the set holds several) — so an onboarding check that tests "one non-null id" now
reads as "nothing published" for a five-document bot. Check that shape before writing a new
consumer.

**Verified beyond the suite.** Migration exercised on a scratch database (`upgrade 0007` →
seed → `upgrade head` → `downgrade 0007` → `upgrade head`) and on the **live dev stack**: 32
pre-existing bots backfilled to 32 one-member sets, legacy column cleared; additive publish
then driven through Caddy (publish A → publish B → both served → rollback → A only). The
downgrade deliberately leaves multi-document sets NULL rather than silently serving one of
five.

**Docs-only commits still go red.** `63b415b` (Markdown only, widget subtree byte-identical
`7ed153c`) was green on `test` and `security` and **red on `widget-browser` alone**, the
annotation naming WIDG-11 in full — the known ~50% Escape flake, not this change. Do not
re-run to chase a green.

**Not claimed**: no live Chromium pass over the multi-document case (ADMN-05 owns that
screen's gate), and no UI for composing a set by hand — sets are created by publish and
rollback, which is what the ruling asked for.

**M5 still open**: `ADMN-05` (its own browser gate), `OPS-05`, and the M5/M6 rows recorded
below. The M6 blocks are unchanged by this WP.


## Loop state (updated 2026-11-18, M6 / WP-38) — OPS-04, five alerts and the two ways a transition rule fails

**OPS-04 is done** (code `72c408b`; CI run **44369966455 green on all three jobs** —
`test`, `security` *and* `widget-browser`, so the WIDG-11 flake did not fire and that defect
is still unfixed). Registry row `backlog` → `done`. Reasoning in PLAN §8.39.
`backend/api/app/observability/alerts.py`, `backend/worker/worker/alert_job.py`,
`tests/test_alerts.py` (53 tests). Backend **935 passed / 3 skipped** (882/3 baseline), eval
gate 6 passed, security scan clean (9 baselined, 0 open).

**HALT-09's Reading B is discharged, not reinterpreted.** The two signals that could not
fire were made real first (OPS-12's `provider_outcomes` and token ledger, OPS-09's backup
producer), so this row is a poller and not a set of branches with no source. The ruling's
five thresholds are the shipped defaults; the two tenant-scoped pairs also take a
per-tenant override in `tenants.settings -> alerts`.

**No new dependency, no new container.** `redis` is a hard `Requires-Dist` of the pinned
`arq==0.28.0` and `shutil.disk_usage` is stdlib — HALT-09 measured that before the ruling,
and the poller therefore runs as a **minute cron beside the 03:15 purge** in the worker
image. `compose.yaml` gained one line: a read-only mount of OPS-09's backup directory into
the worker, without which `backup_failure` would report "destination is not visible"
forever. Do not turn this into a compose service; it buys a container and a supply chain for
one query a minute.

**The defect worth not rediscovering is in the transition memory.** `dispatch` must clear a
remembered scope that produced **no reading** this sweep: a fixed provider outage empties
its five-minute window, so the recovered provider would otherwise stay "already alerted" for
the life of the process and the *next* outage would be silently suppressed. The mirror-image
bug is re-sending every minute, which trains an operator to filter the channel. Both
directions are tested and both mutations are red.

**"Cannot see" is not "nothing there", in an alert this time.** Unreachable Redis is a
**warn**, not `ok` — a silent poller is indistinguishable from a healthy system, which is
worse than the absent-metric trap §8.38 fixed on the graph. Depth is `ZCARD`; `LLEN` on
arq's sorted set answers `WRONGTYPE`.

**An alert and a hard stop must read one ledger.** The budget reading calls
`controls.monthly_token_usage`, the helper CONV-14 stops on, and the provider ratio takes
both halves from `provider_outcomes` because `usage_records` records answered turns only.

**A test that could not fail, again.** The first "never a tenant's address" test set
`ALERT_EMAIL`, so *any* fallback was invisible and a mutation to `SMTP_FROM` stayed green.
It is now tested where the fallback would fire: a tenant with a handover address, no
operator address, `SMTP_FROM` set. 18/18 mutations red afterwards.

**`eva-smtp-sink` captures, never delivers.** A real provider is an `.env` change (HALT-09
point 3), so live evidence proves the channel, not a human receiving mail. Alert state is
per process: a worker restart re-announces an active alert.

**M6 still open**: `OPS-05` (knowledge-gap detection), `OPS-07`'s uptime-probe half (the
delivery channel now exists but uptime is not one of the five signals, and `/health` is
still a 404 through Caddy — §8.33, and HALT-08 ruling point 5 keeps it that way), and the
rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-18, M6 / WP-37) — OPS-03, the exposition defect only a real database showed

**OPS-03 is done** (WP-37; code `370cb70`; CI run **44342240592 green on all three
jobs** — `test`, `security` *and* `widget-browser`, so the WIDG-11 flake did not fire and
that defect is still unfixed). The docs commit `a0ca7b5` (Markdown only, three files, widget
subtree byte-identical `7ed153c`) was green on `test` and `security` and **red on
`widget-browser` alone** — step 10 "Browser matrix", bare exit code 1, no per-engine
annotation: the §8.27 WIDG-11 flake at its known ~50% rate, not this change. Not re-run to
chase a green. Registry row `backlog` → `done`. Reasoning in
PLAN §8.38. `backend/api/app/observability/metrics.py`, `tests/test_metrics.py` (32 tests).
Backend **882 passed / 3 skipped** (848/2 baseline), eval gate 6 passed, security scan clean
(9 baselined, 0 open).

**Both endpoints were already in the ruling, so neither is a deviation.** Do not re-litigate
`GET /admin/metrics/summary` — HALT-08 ruling point 3 names it. It is mounted at
`/api/v1/admin/metrics/summary` behind `RequireAdmin` + `ScopedSession`, so it is tenant
scoped. `/metrics` has **no** `/api/v1` prefix and is **deliberately not in `caddy/Caddyfile`**:
an unauthenticated scrape and §22's observability access control cannot both hold on the
public origin. Measured through Caddy: `/metrics` → 404, `/api/v1/bots` → 401.

**The independent parser the ruling demanded was too lenient, and only the live database
said so.** It used `dict.setdefault` for `# HELP`/`# TYPE`, so a duplicate name overwrote and
reported a clean parse — while `render` was emitting `eva_llm_latency_p50_seconds` **77 times**
(one per active tenant): 545 families for 55 samples, 83 KB. A real Prometheus rejects a
duplicate metric family. `render` now merges families by name, sorts samples for byte-stable
output and omits empty families (5 families / 65 lines / 5.3 KB after). The parser asserts
uniqueness of HELP, TYPE and every `(name, labels)` series. **Lesson: parse the real endpoint,
not a hand-built sample** — every unit test passed while the contract was violated.

**Two more traps in the same code.** Label *values* legitimately contain braces (a route
template is `/api/v1/conversations/{conversation_id}`), so splitting the label block on the
first `}` reads a valid line as malformed — it passed alone and failed in the full suite, once
another test had recorded a templated route. And `scope["route"].path_format` **drops the
`include_router` prefix** (fastapi 0.163.1), so labelling on it raw merges `/api/v1/widget/x`
with `/api/v1/x` into one series: a graph that looks plausible and is wrong. `route_template`
recovers the prefix by segment alignment; unmatched paths get the fixed `(unmatched)` label so
no client input reaches a label.

**Queue depth is `ZCARD`, and the recorded `LLEN = 0` was not evidence of anything.** arq
pushes with `zadd` (`arq/connections.py:175`), so the queue is a sorted set; `LLEN` on a real
zset returns `WRONGTYPE` (measured in the live Redis). The `LLEN = 0` in §8.33/§8.37 was a
**key that did not exist**, and Redis answers 0 for any type on a missing key. Unreachable
Redis omits the family rather than reporting 0 — "queue empty" and "cannot see the queue" must
not look the same on a graph.

**`messages.latency_ms` had no writer**: 473 assistant rows, **0** populated. The latency
metric would have been zero by construction — OPS-12's defect class (§8.37) in a second place
— so the producer is fixed in `conversation/service.py` (measured around `provider.stream`,
skipped on the degraded path, whose duration is time-to-failure and would make an outage look
slow). Verified live through Caddy: `latency_ms = 527` persisted and
`eva_llm_latency_p50_seconds{provider="ollama_cloud"} 0.527`.

**Counters are per process — a stated gap, not a hidden one.** §20.1 specifies two uvicorn
workers and the container runs one, so `eva_http_requests_total` and the SSE counters are
correct today and would each report ~half under two. Every *business* number is computed from
the store on request, which is correct under any worker count.

**9/9 mutations red** (counter naming, route template, unmatched bucketing, label escaping,
SSE `completed` flag, tenant loop, error-ratio query, latency producer, family merge — the last
turns 6 red). The SSE test was rewritten: completion means "a `final` event was emitted", so a
disconnect after `final` is a completion, not an abort.

**This unblocks the OPS-04 transport question but not OPS-04.** The metrics transport now
exists, so "OPS-04 inherits OPS-03's transport" is discharged; OPS-04 is still blocked on its
own HALT-09 scope (poller, thresholds in code, `ALERT_EMAIL`). Do not re-file it behind this
row.

**M6 still open**: `OPS-04` (HALT-09, Reading B ruled — data now present), `OPS-05`, `OPS-07`'s
uptime-alerting half, and the rest of WP-25's §27 sweep. `/health` through Caddy is still a 404
(§8.33, untouched — and the HALT-08 ruling point 5 says explicitly that it stays that way).

## Loop state (updated 2026-11-18, M6 / WP-36) — OPS-12, a ledger that was zero by construction

**OPS-12 is done** (code `36dc0c8`). Registry row `backlog` → `done`. Reasoning in PLAN
§8.37. Migration `0007_provider_outcomes`, `tests/test_provider_outcomes.py` (15 tests).
Backend **849 passed / 1 skipped** with credentials present, **848 / 2** without (the
`test_conversation` skip is keyed on a configured provider); eval gate 6 passed, security
scan clean (9 baselined, 0 open). Suite baseline before the edit was **833 passed / 2
skipped**.

**CI run 44320474682 on `36dc0c8`**: `test` **success** and `security` **success**;
`widget-browser` **red at step 10 "Browser matrix"**. The annotation names the defect
outright — *"Escape from the host page left the panel open (focus=BUTTON:Close chat,
expanded=["false@592","true@592"])"*, the documented WIDG-11 close-then-reopen race
(§8.26/8.27) — and the widget subtree is `7ed153c` at this sha and at its parent
`f9d349e`, with an **empty** `git diff -- backend/api/widget`. The matrix re-run locally
against the live stack at this sha is **9/9 chromium, 9/9 firefox, 0 skipped**, including
the Escape case. Parent `f9d349e` was red on the same job with the same annotation, so
this is the flake following the tree, not this change.

**CI run 44324731300 on `cdba501`** (the registry/docs commit, code identical to
`36dc0c8`) is **green on all three jobs** — `test`, `security` *and* `widget-browser`. That
is the same coin landing the other way, not a fix: nothing between the two shas touches
the widget, and the WIDG-11 defect is still unfixed and still owned by the widget row.

**The ledger was zero because the caller said so.** `units = (prompt_tokens or 0) +
(completion_tokens or 0)` with its only call site passing `None, None` — the providers had
already parsed real counts and they were dropped one frame above. So CONV-14's budget stop
was dead code in every deployment and `/analytics/summary` reported 0 tokens (§8.33). **The
counts were never even requested**: no `stream_options.include_usage`, so an
OpenAI-compatible stream sends no usage frame at all. Two shape traps in that fix — the
frame has **empty `choices`** (the existing early return ate it) and arrives **after** the
last token (no chunk left to attach it to, and a stream has no return value), so counts ride
a trailing empty-text chunk.

**Attribution is the serving link, not the chain.** `FallbackChain.key` is
`fallback-chain`; stamping that would give every row the same provider and make a
per-provider error rate meaningless. The chain stamps the answering provider's key onto
each chunk with `dataclasses.replace`.

**A latent bug in the same corner.** `if not yielded: breaker.record_success()` treated
*any* chunk as an answer, so a usage-only frame would count as success and **suppress the
fallback that exists for exactly that case**. Guarded on `chunk.text`.

**`error_class` is `type(exc).__name__`, never the message** — a provider message is built
from up to 300 chars of the response body and can echo the prompt, and this value is
persisted and read by a poller. OPS-02's rule, in a table instead of a trace.

**Measured through Caddy, not only in-process.** `docker compose up -d --build api`, live
database migrated to `0007`: a real widget turn returned `final.usage {"prompt_tokens":
365, "completion_tokens": 56, "reported": true}` and the database holds one
`provider_outcomes` row (`ollama_cloud`, success) and one `usage_records` row with
`units=421`. The degraded path driven inside the container writes `(unconfigured, failure,
RuntimeError)` and **no** usage row. `0007` was exercised on a scratch database through
`downgrade -1` → `upgrade head` before the live migration; probe rows and the scratch
database were dropped and verified gone.

**7/7 mutations red**: drop the failure-path row (2 tests), stamp the chain's own key (1),
leak the message into `error_class` (2), restore `"usage": {}` (2), weaken the RLS predicate
to `true` (1), let a usage frame count as `yielded` (1), revert the token threading to
`None` (3).

**OPS-04 is now unblocked on its data but still not implemented**: all five signals have
events (this row + OPS-09's producer), and HALT-09 Reading B supplies default thresholds,
but there is no poller, no threshold code and no `ALERT_EMAIL`. `usage_records.provider`
covers *answered* turns only, so the error rate must union `provider_outcomes` with it
rather than reading either alone.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (HALT-09, data now present), `OPS-05`,
`OPS-07`'s uptime-alerting half, and the rest of WP-25's §27 sweep. `/health` through
Caddy is still a 404 (§8.33, untouched).

## Loop state (updated 2026-11-18, M6 / WP-35) — FUT-04, a dedicated database and the worker pointed at the wrong one

**FUT-04 is done as readiness, not as a shipped SKU** (code+docs `0f80e84`). Registry
row `backlog` → `done`. One command: `python scripts/fut04_single_tenant.py check`.
Reasoning in PLAN §8.36. `tests/test_single_tenant_readiness.py` (19 tests). Backend
**833 passed / 2 skipped** (814 baseline, fixed before any edit), eval gate 6 passed,
security scan clean (9 baselined, 0 open). CI run **44302596253**: `test` and `security`
green; `widget-browser` red at step 10 with the annotation naming **WIDG-11**
(`expanded=["false@583","true@584"]`), widget subtree `7ed153c` at this sha and its
parent — the known flake, not this change.

**One line was the whole defect, and no test in the suite could see it.**
`compose.yaml`'s `worker` had `APP_DATABASE_URL: .../eva` hard-coded while `api` built
both DSNs from `${POSTGRES_DB:-eva}`. With `POSTGRES_DB=customer_acme` the API read the
customer's database and the **ingestion worker wrote chunks into the shared one** — the
widget then answers from an empty knowledge base while every health check passes. Found
by rendering `docker compose config` with a non-default `POSTGRES_DB`; the in-process
suite structurally cannot see compose wiring (AGENTS.md says so, and this is the third
bug to live in that gap). Now all three DSNs follow `${POSTGRES_DB:-eva}`.

**The most transferable finding: migration `0001` rewrites a *cluster-wide* credential.**
It runs `ALTER ROLE eva_app ... PASSWORD` on every upgrade, and a role is cluster-wide
while a database is not. Migrating a second database on a shared cluster with a different
`EVA_APP_DB_PASSWORD` therefore **changes the password the first deployment is using** —
measured here, on this cluster: after the probe, `eva_app` worked with the new password on
`eva` and was refused with the old one. Repaired immediately and re-verified. The script
now refuses with exit 2 when the role exists and rejects the configured password, and
**allows** a missing role (a dedicated cluster's first migration is supposed to create
it). Combined with §8.34's "`pg_dump` does not carry roles", this means *"same cluster,
new database"* is **not** equivalent to a dedicated cluster, and the guard says so rather
than rotating a credential silently. Do not "simplify" that guard away.

**Comparing a fresh database to the live one proves the wrong thing.** The command
migrates **two** scratch databases and diffs them against **each other**; the live
database carries whatever drift its own history left, so a fresh-vs-live match is a
coincidence, not a reproducibility result. Comparing policies by **name** is the other
trap — 17 policies on each side with different predicates is not identical, and the lost
policy fails as a query that *succeeds and returns too much*.

**One mutation stayed green and is recorded, not papered over.** Making `run_check`
inventory one database twice left all 19 tests passing, because every equality check is
also satisfied by self-comparison. The report now carries `compared` and the test asserts
the two names are distinct *and* are the two that were migrated. Six mutations total, all
red now. The security scan also caught a genuine `py-sql-interpolation` finding in the
new script (`CREATE DATABASE {name}`) — fixed with `psycopg.sql.Identifier` rather than
baselined, because an identifier that reaches SQL text is exactly what the rule exists for.

**Verified through the containers, not only in-process.** Migrated a dedicated database
from inside the `api` container, ran the `api` **image** against it (`docker compose run`):
register `201`, `/auth/me` `/bots` `/conversations` `/analytics/summary` `/health` all
`200`, no code change. Tenant present in the dedicated database and **absent** from `eva`.
Scratch databases dropped and verified gone; the live `eva_app` login re-checked working.

**Not claimed**: no per-tenant provisioning command, no migration tool for extracting an
existing tenant from the shared database. §16.3 asks for neither and R13 defers the
offering.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (HALT-09), `OPS-05`, and the rest of
WP-25's §27 sweep. `/health` through Caddy is still a 404 (PLAN §8.33, untouched).

## Loop state (updated 2026-11-18, M6 / WP-34) — OPS-11, gating the right surface

**OPS-11 is done** (code `848d6bf`; CI run **44295556727 green on all three jobs** —
`test`, `security` *and* `widget-browser`, so the WIDG-11 flake did not fire on this
sample and that defect is still unfixed). Registry row `backlog` → `done`. Reasoning in
PLAN §8.35. `backend/api/app/observability/debug.py`, `tests/test_observability_access.py`
(17 tests). Backend **814 passed / 2 skipped** (797 baseline + 17), eval gate 6 passed,
security scan clean.

**The obvious reading was the wrong control.** §22 says "conversation-level debugging
requires owner role". Gating `GET /conversations/{id}` would have deleted ADMN-06 — a
viewer is *supposed* to read conversations, and that text is what the handover email and
the SAR export carry. What moved is the **observability** half: per-turn `model`,
`latency_ms`, `prompt_tokens`, `completion_tokens`, plus the conversation's
`usage_records`, now on `GET /api/v1/conversations/{id}/debug` behind `RequireOwner`.

**The transcript route already answered those same four fields to any role**, which would
have made the new gate a second door on the view it protects. They are `null` below owner
now. `content`, `citations`, `confidence` and CSAT stay: `compile_transcript` has always
put confidence in the email the owner receives, so hiding the field leaves the same number
one paragraph away. Gating a value that is already in the email is theatre.

**The export is deliberately NOT narrowed.** It is R32's data-subject access mechanism;
gating it answers a legal request with a partial record, which is worse than the defect
this row fixes. A test pins that a *viewer* still gets the telemetry from the export.

**`tenant_session` strikes again.** Writing the audit row and returning answers 200 and
records nothing. `session.commit()` is explicit. Dropping it → **4 tests red**. And the
detail is counts only (`{"turns": n, "usage_records": n}`), because `audit_logs` is
append-only and never purged: a receipt quoting the conversation would put the visitor's
name, email and session id in the one table nothing can delete — the SEC-10 erasure trap
in a second place. Both the view and the stored JSONB are asserted absent of content and
of every seeded identifier.

**A mutation that did NOT go red — recorded, not papered over.** Dropping
`AND tenant_id = :tenant_id` leaves the suite green: RLS refuses the row. Measured
directly as `eva_app` in a tenant scope owning nothing (0 visible, 0 by id). The predicate
is defence in depth, **not** the guard — same finding as WP-19 and WP-23. A separate
unscoped test pins the policy, because a lost policy makes the query *succeed and return
too much*. Do not cite the isolation test as proof the application layer is the guard.

**Stated limit: refusals are not audited.** `RequireOwner` stops a non-owner before the
handler runs, so a 403 leaves no trace. Auditing denials needs a role-check-shaped
dependency that can write — a wider change than this row. §22 asks for debugging *access*
to be recorded, and every granted one is.

**Deviation, written up not assumed free**: §17.5 lists no `/debug` path. It adds a path
under an existing resource with no schema change — the same class as SEC-10's DELETE. If
the operator would rather fold the diagnostics into the transcript view behind a role
check (no new path), that is a contract call.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (HALT-09), `OPS-05`, and the rest of
WP-25's §27 sweep. `/health` through Caddy is still a 404 (recorded in §8.33, untouched).


## Loop state (updated 2026-11-18, M6 / WP-23) — OPS-08, load test + a real ingest fix

**OPS-08 is done** (code `4826dc1`; docs `103676b`, `891397d`; run **44273253739 green on all three
jobs**). Registry row `backlog` → `done`. Reasoning in the row itself. One command:
`python scripts/ops08_load.py` (streams via `scripts/ops08_sse.js`).

**The load test found a bug that made 500-page ingestion impossible.** `RemoteEmbedder.embed`
passed every chunk text in one `/embed` call and the service caps a request at `MAX_BATCH=64`
(`backend/embed/service.py`), so **every document over 64 chunks failed permanently with 422** —
which is to say, the ingestion half of this row could not pass for any realistic document, and no
test covered it because every existing embed test used a handful of texts. Measured, then fixed:
500 sections → 100 chunks → `ready`, 100/100 chunks stored.

**Batch size is 8, not the 64 cap, and the timeout had to move with it.** A real 727-token chunk
costs 5–15 s on the 2-vCPU embed container, so a 64-text request runs ~10 min and dies on the
client timeout — the first version of the fix still failed with `timed out` for exactly that
reason. `timeout` 120 s → 300 s and `batch_size` 8 keep one request inside its own deadline.
`MAX_EMBED_BATCH` reads the same `EMBEDDING_MAX_BATCH` variable the service does, so client and
server cannot drift, and a configured `batch_size` above the cap raises instead of 422-ing once per
document.

**The 429s in a sustained run are SEC-02 working.** 50 VUs × several turns from one host IP trips
`ip_rate_limited` at request 61 (`IP_REQUESTS_PER_MINUTE` = 60) — 104 of 522 requests over 60 s.
Fifty real visitors arrive from fifty addresses, so the *harness* bends: one turn per VU, which
still puts 50 streams in flight simultaneously. **Do not raise the limit to make this green**; that
deletes the control under test. Same rule §8.20 recorded for the webhook port allowlist.

**`sse_streams_without_final` is the metric that matters.** A 200 that opens and never emits
`final` is a hung visitor while `http_req_failed` stays 0, so a status-code-only check would pass a
broken SE. Verified the gate is not vacuous: 80 VUs turns it red.

**Known, not fixed — needs a ruling.** 1244 s for one 500-page document is serialized CPU-bound
encoding (`model.encode` holds a global `_lock`; embed pegged at 196% of its 2-vCPU quota), so a
large ingest occupies the only worker for ~20 min. That is a §20.3 capacity trigger for OPS-03's
metrics, not a correctness bug.

**Registry citation defect found, not silently fixed**: the row cited `§21.1`, which does not exist —
`docs/ARCHITECTURE.md` has no §21 at all, and **§27 is duplicated verbatim** (lines 816 and 837).
The requirement is unambiguous ("Load test: 50 concurrent streams, no error spike") and is what was
tested; the citation is corrected to §20.2/§27 and the duplicate left alone because ARCHITECTURE is
checksum-locked (G1).

**`widget-browser` was red on `4826dc1` and green on `891397d`**, widget subtree byte-identical
(`7ed153c`) at both — bare `exit code 1` at step 10 with no per-engine annotation, the WIDG-11
Escape flake signature. Ruled out rather than assumed: the matrix re-runs **9/9 chromium and 9/9
firefox** locally at that sha, including the Escape case. Parent `31d30a3` was green.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (HALT-09), `OPS-05`, `OPS-09`, and the rest of
WP-25's §27 sweep.

## Loop state (updated 2026-11-18, M6 / WP-33) — OPS-09, a backup that proves it is one

**OPS-09 is done** (code+docs `670516f`). Registry row `backlog` → `done`.
`scripts/ops09_backup.py` (`backup` / `verify` / `drill`), `ops/systemd/eva-backup.{service,timer}`,
`docs/ops/RUNBOOK.md`, `tests/test_ops09_backup.py` (45 tests). Backend **797 passed / 2 skipped**
(752 + 45), eval gate 6 passed, security scan clean. Reasoning in PLAN §8.34.

**The backup now exists, and §8.33 said it did not.** OPS-04's *backup-failure* alert was
watching a job with no script, no cron and no service. The producer is now real; **the alert is
still OPS-04's** and still halted (HALT-09), but the dependency inversion is gone.

**Two traps a counts-only drill calls a pass — both measured, not theorised.**

1. `pg_restore --data-only` into a migrated database **fails on foreign keys**: `COPY` replays
   in archive order against live constraints, so `usage_records` aborts, 16 tables skip, and you
   get `tenants` 75 / `conversations` **0**. Restore the **whole** archive into an **empty**
   database — that is also the only path that brings back RLS policies and grants.
2. **`--no-privileges` silently removes the `eva_app` grants** while every row count matches
   (787 conversations, 1047 messages, 17 policies). The restore "succeeds" and every request
   500s. The drill therefore reads one tenant's conversations **as `eva_app` in a tenant scope**
   (measured 719/719 scoped, **0 unscoped**); the unscoped read is its own test because a lost
   policy makes the query *succeed* and return too much.

**Roles are not in `pg_dump`.** `eva_app` is created by migration code, so a fresh-box restore
needs the cluster role to pre-exist. This is invisible until someone tries it.

**Drill measured 25.76 s** end to end (of which the `downgrade -1` + `upgrade head` rollback is
12.77 s), 2876 rows / 19 tables, revision intact. It is a scratch DB **on the same box** — not
§20.4's fresh-VPS RTO, which is still unmeasured and recorded as such.

**Rollback is tested for the migration half only.** The drill proves the **head** revision
downgrades and re-upgrades (`0006` → `0005` → `0006`); it does not prove every migration is
reversible, and the runbook says that rather than implying it. The image half is a `docker tag`
procedure.

**Confidentiality is enforced, not documented.** The age identity is **always excluded** from
the files archive — matched by name *and* env var, because an identity at the default path is
named by no variable, and archiving both would make the backup a plaintext key store. Dumps are
encrypted by default (`OPS09_AGE_RECIPIENT` required; the dump is piped *through* `age` so no
plaintext ever lands on disk). A destination **inside the repository is refused outright**; one
on the database's own filesystem warns and leaves `off_box_proven: false`. **The vault ciphertext
is root-owned 600** — an unprivileged `tar` reads 0 files — so the script streams `tar -czf -`
and retries via `sudo -n tar`, recording `read_with_privilege: true`.

**No new dependency, no new image.** The backup runs on the host; `pg_dump` is only in the
postgres image, `age` only on the host. A systemd timer, not a compose service.

**8/8 mutations red**, and one of them found a real bug: an RLS refusal *raised* out of
`rls_check` instead of being reported, which aborted the drill and hung in cleanup. Refusals are
now red verdicts and every docker call has a timeout.

**Two compliance sentences became false when the drill landed and were regenerated**: the DPA's
"until the restore drill is complete and timed … must not claim a backup regime" and the US
notice's "no restore drill exists yet". Both now describe the regime and keep the disclosure that
matters — **an erasure does not reach a backup**.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (HALT-09, now unblocked on its backup signal),
`OPS-05`, and the rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-18, M6 / WP-32) — OPS-04 HALTED, not implemented

**OPS-04 did not land, by design.** It halted as **HALT-09** (PLAN §8.1) with no code
written. Registry row moved `backlog` → `blocked (HALT-09)`. Two docs files changed, no
source file. Baseline was fixed **before** any edit at **748 passed / 2 skipped**, identical
to WP-30's and WP-31's record. Halt docs `4e83fa8`, CI green on **all three** jobs (`test`,
`security` *and* `widget-browser`) at run 44240691441.

**The inherited explanation was wrong, and finding that was the point.** AGENTS.md and the
OPS-03 row both said OPS-04 "inherits whichever metrics transport the ruling picks", i.e. it
was queued behind HALT-08. It is not: an alerting poller needs no Prometheus. Two of the five
signals are computable today with **no new dependency** — `shutil.disk_usage` (stdlib) and
`LLEN arq:queue` via `arq.constants`, and `redis` is a hard `Requires-Dist` of the pinned
`arq==0.28.0`, already in the `api` image. **Granting HALT-08 would not have unblocked this
row.** Do not re-file OPS-04 behind OPS-03.

**Three of the five alerts have nothing to watch.**

- *Provider error-rate* — no provider failure is persisted anywhere (the degraded path logs
  and returns) and there is no log sink in §20.1. `usage_records.provider` is NULL on
  **22/22** rows because the sole `record_usage` call omits `provider=` and only runs on the
  answered path (`service.py:676`; degraded returns at `654`).
- *Budget exhaustion* — **a dead branch in every deployment, and a product defect separate
  from this row.** `units = (prompt_tokens or 0) + (completion_tokens or 0)` and its only
  caller passes `None`/`None` (`service.py:681-682`), so units is structurally 0: measured
  22 rows with `sum(units)=0`, zero rows with `units>0`, and `check_budget` on a live tenant
  returns `used=0 budget=1000000` raising nothing. R31's ledger is zero, CONV-14 cannot trip,
  and `/analytics/summary`'s token totals are 0. The providers *do* parse real counts
  (`providers._usage`); they are dropped at the call site. **Nobody has filed a row for
  this** — it is reported in PLAN §8.33 and is the thing to fix first if metrics or budgets
  matter.
- *Backup failure* — watches a job that does not exist; no script, cron, service, volume or
  marker. OPS-09 owns creating one, so this alert is a dependency inversion.

**No threshold exists for any of the five**, and the blueprint supplies none ("spike" has no
window/ratio, "disk usage" no percentage, "backlog" no depth). **No monitored address exists
either**: `eva-smtp-sink` is an ad-hoc container (`python sink.py 2525`, absent from
`compose.yaml`) that writes `inbox/msg-NNN.eml` and returns `250` — mail is captured, never
delivered. There is no `ALERT_*`/operator key in code or `.env`, `SMTP_*` are not declared in
`.env.example`, and `settings.handover.email` (17 tenants) is the **tenant owner's** mailbox —
alerting there would ship platform topology to customers. Reading A (ship disk+queue alerts
now) would deliver 2/5, invent both thresholds, and still leave §27's "alerts firing to a
monitored address" **false** while the row looked done.

**A gap measured while halting, outside this row**: `/health` and `/ready` answer **200
inside `api`** but **404 through Caddy** — `caddy/Caddyfile` routes only `/api/*`, `/widget/*`
and `/demo/*` to the API. Compose's container probe calls `127.0.0.1:8000` directly so
nothing is broken today, but an external uptime monitor on the public origin would sit on a
permanent 404. Recorded, not patched: it needs an unauthenticated endpoint on the public
origin that §17 does not list.

**M6 still open**: `OPS-03` (HALT-08), `OPS-04` (this halt), `OPS-05`, `OPS-08`, `OPS-09`,
and the rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-18, M6 / WP-31) — OPS-03 HALTED, not implemented

**OPS-03 did not land, by design.** It halted as **HALT-08** (PLAN §8.1) before any code was
written, because the row cannot be built without a ruling and building part of it would have
been the worse outcome. Registry row moved `backlog` → `blocked (HALT-08)`. No source file
touched; the suite was run first to confirm the baseline (**748 passed / 2 skipped**, matching
WP-30's record) so a later "it was already red" claim cannot be made about this turn.

**CI: one green, then the flake three times on docs-only pushes.** The code+docs commit
`41d1e98` was green on **all three** jobs (`test`, `security`, `widget-browser`), run
44226045173. All three follow-ups were **red on `widget-browser` alone**, with `test` and
`security` green every time:

| sha | run | `test` | `security` | `widget-browser` |
|---|---|---|---|---|
| `41d1e98` | 44226045173 | success | success | **success** |
| `807018c` | 44226665632 | success | success | failure |
| `c74e62b` | 44227200421 | success | success | failure |
| `012682c` | 44229744189 | success | success | failure |

Run 44226665632's check-run annotation names the defect in full: *"Escape closes the panel and
focus returns to the launcher (WIDG-11) ... expanded=["false@591","true@591"]"* — the
§8.26/8.27 race closing and reopening one tick later. Run 44229744189's annotation is explicit again — WIDG-11 in **chromium *and* firefox**
(`expanded=["false@578","true@580"]` and `["false@624","true@640"]`) — so the race is not
engine-specific. Run 44227200421's annotation was only `exit code 1`, but its failing step was
**10 "Browser matrix"** (install, build and migrate all passed), and job logs need admin (HTTP
403), so the step conclusion is the evidence available.
All four shas carry widget subtree `7ed153c`, and the three red ones touch **Markdown only**
— two files each and no code at all. Three of four pushes red on a docs-only diff is the ~50% rate §8.27
already recorded.

Not re-run to chase a green — the charter says not to re-diagnose this defect, and each
further push only samples the same coin. **The defect is still unfixed** and still belongs to the widget
row. Treat `widget-browser` red on a Markdown-only diff as this flake, and read the step name
before suspecting the change.

**The two measurements that decided it.** (1) `prometheus_client` is absent from the `api`,
`worker` *and* `embed` images, absent from the local venv, and **no requirement in the whole
installed closure names a Prometheus client** (all 64 distributions re-walked; zero matches).
(2) The free-looking alternative does not work: `opentelemetry-api` 1.45.2 ships as a hard
`Requires-Dist` of `fastapi==0.163.1`, but with no SDK `get_meter()` → `_ProxyMeter`,
`create_counter()` → `_ProxyCounter`, and `hasattr(counter, "collect")` is `False`. Measured,
not inferred from `pip list`. That is OPS-02's "instrumentation with no transport" gap in a
second place, and it means **OPS-02's spans and OPS-03's metrics are blocked by the same
missing ruling**, not by two independent ones.

**Why this was a halt rather than a scoping call.** Four G3 classes at once: G3.1 a new
runtime dependency; G3.2/3 two container images absent from §20.1's topology plus a `GET
/metrics` route §17 does not list; G3.4 the spec says these metrics exist but never how the
non-HTTP ones reach a scraper nor what "light" Grafana means; G3.6 a Grafana admin secret is
operator provisioning. The precedent that settles the dependency question: `litellm` was
named *in the locked stack table* and still needed HALT-05 (rule: option B, hand-written
client). `prometheus_client` is not even in that table, so it has strictly weaker standing.

**The trap in Reading B.** Hand-writing the exposition format looks like the dependency-free
escape and is not: the format is the easy half, aggregation is the hard half. §20.1 specifies
**2 uvicorn workers** (the container runs 1 today, so this bug is latent rather than live), and
per-process counters would make each scrape report ~1/N of the traffic. A metric that is
silently wrong under the documented topology is worse than no metric — which is why
`prometheus_client`'s multiprocess collector exists. Reading B also does **not** avoid the
Prometheus and Grafana containers, so it discharges only one of the four classes.

**A note for whoever builds this.** `/metrics` should stay off `caddy/Caddyfile` and be
scraped container-to-container. §22's "access control on observability requires owner role"
cannot be satisfied by a public unauthenticated scrape endpoint, and a Prometheus-internal
target needs no app-level auth at all. Also: label on the **route template**
(`/api/v1/conversations/{id}`), never the raw path, and never a conversation id — §20.2's
ceiling (≤50 tenants) makes a tenant label fine and a conversation label unbounded.

**M6 still open**: `OPS-03` (this halt), `OPS-04`, `OPS-05`, `OPS-08`, `OPS-09`, and the rest
of WP-25's §27 sweep. `OPS-04` inherits whichever metrics transport the ruling picks, so it is
blocked behind HALT-08 in practice.

## Loop state (updated 2026-11-18, M6 / WP-30) — OPS-02, turn spans

**OPS-02 is done as instrumentation, not as visible traces** (code `f67af87`,
docs `67fe43b`; CI green on **all three** jobs — `test`, `security` *and*
`widget-browser`, run 44213899059, which is the first green `widget-browser` in a while
and does not mean the WIDG-11 Escape defect is fixed). Reasoning in `docs/PLAN.md` §8.32.
`app/observability/tracing.py` is imported by the turn loop and the fallback chain;
`tests/test_tracing.py` is 10 tests. Backend **748 passed / 2 skipped**, eval gate 6
passed, security scan clean (9 baselined, 0 open).

**Nothing is exported, and that is the headline.** No SDK, no exporter, and no
collector in §20.1's topology, so `is_recording()` is `False` and every span is
discarded. A collector container plus `opentelemetry-sdk` is a new dependency *and* a
topology change, so it is a ruling, not an install. Do not read this row as "traces in
Grafana"; that is OPS-03's stack plus a ruling.

**`span()` must not use `start_as_current_span`.** Spans are opened inside an async
generator that yields while open, so the attach/detach pair straddles a suspension
point. When the consuming task is cancelled — every SSE disconnect — the generator is
closed from a different context and the detach raises
`ValueError: Token was created in a different Context`, logged as
`ERROR opentelemetry.context: Failed to detach context`. `span()` uses `start_span`
with an explicit parent and one `ContextVar` with a guarded reset. Reverting it turns
**2** tests red.

**That fix's test could not fail at first.** The cancellation test cancelled the same
task that owned the generator, so no detach error was possible and it passed against the
broken code. It now cancels a separate consumer task and `aclose()`s from the test's own
task. Before trusting any red or green here, run the mutation.

**A fixture in one file changed 36 other tests.** The recording `Span` raised from
`record_exception`, but FastAPI 0.163.1 instruments its own requests through the same
global provider, so the raise landed in FastAPI's error handling and
`test_widget_tokens.py` went 45 passed → 36 failed — green alone, red in the full suite.
Baseline `744e285` was measured (**738 passed / 2 skipped**) before blaming the change.
The provider now returns `NoOpTracer()` for any scope but `eva.conversation`. Any
session-scoped OTel provider in this repo needs that filter.

**Content never reaches a trace.** A canary through the real turn loop must be absent
from every span name/attribute/event while the conversation id is present (mutation
checked), and `error_event` records `type(exc).__name__` only — a provider message is
built from up to 300 chars of the provider's body (`providers.py:212`) and a trace leaves
the box, which is a stronger rule than OPS-01's logging guard needed.

**M6 still open**: `OPS-03`, `OPS-04`, `OPS-05`, `OPS-08`, `OPS-09`, and the rest of
WP-25's §27 sweep. `SEC-12`, `SEC-13`, `OPS-01` and `OPS-02` are done.

## Loop state (updated 2026-11-17, M6 / WP-29) — OPS-01, structured JSON logs

**OPS-01 is done** (code `2e91cc1`; CI green on **all three** jobs, run 44196944177).
Docs `616f336` was green on `test` + `security` and **red on `widget-browser`** with the
widget subtree byte-identical (`7ed153c`) and only Markdown differing: the WIDG-11 Escape
case failed in chromium *and* firefox. That is the §8.26/§8.27 flake again, not this WP —
but it is now confirmed on a docs-only commit (`616f336`) and again on another
(`c5147c1`), which is the cleanest proof yet that it follows the tree, not the change.
Reasoning in
`docs/PLAN.md` §8.31. One module, `app/observability/logs.py`, imported by the API routers,
the webhook sender and the worker; `tests/test_logging.py` is 22 tests.

**The one design decision that makes the ids real.** FastAPI runs sync dependencies and
sync endpoints in a worker thread with a **copy** of the context, so a `ContextVar.set()`
in `current_user` is invisible to the endpoint it authenticated. The module keeps one
mutable dict in a single `ContextVar` and mutates it — copies share the object. Rewriting
`bind` as a per-field `set()` turns 2 tests red. Do not "simplify" it back to a
`ContextVar` per field; the tests that would catch it are the access-line and real-request
tests, not everything.

**Uvicorn's access line cannot be fixed, only replaced.** It is text, and it is emitted
after the response completes, when the context has already been reset. `configure()`
clears `uvicorn.access`'s handlers and the middleware writes the one line per request.
The middleware is pure ASGI, not `BaseHTTPMiddleware`, which would interpose a queue
around the SSE stream.

**The worker needed `--custom-log-dict`, not `on_startup`.** arq's CLI calls
`dictConfig(default_log_config())` before the lifespan, so the first version left the
banner (`Starting worker for 5 functions`, `redis_version=...`) as plain text. The worker
`CMD` now passes `--custom-log-dict worker.settings.LOG_CONFIG`. This is the only way to
get JSON from the worker's *first* line; do not remove the flag and rely on `configure()`.

**Content is guarded, not merely absent.** A canary through a real turn plus an AST guard
over `backend/api/app` and `backend/worker/worker` that fails if an info-or-above call is
handed a content-like name. The guard skips `len(...)` so counts stay loggable, and a
sibling argument in the same call is still inspected (both directions tested). It asserts
a **floor** on calls read, because zero findings from zero files is the same green as a
clean tree (§8.25, §8.30). A first mutation of the access line silently did not apply —
string anchor absent — and looked green; re-run it properly before believing any "red".

**Bounded gap, documented, not hidden**: two lines at API container start are still
uvicorn's text because they precede the lifespan. Fixing them means configuring logging at
import time, which would also reconfigure pytest. The worker does not have the gap.

**M6 still open**: `OPS-02..05`, `OPS-08`, `OPS-09`, and the rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-17, M6 / WP-28) — SEC-13, dependency scanning + SAST

**SEC-13 is done** (code `0a038c1`, docs `fe021a3`; run 44186223745 is green on all
three jobs, and `test` + `security` were green on `0a038c1` too, run 44184927224).
Reasoning in `docs/PLAN.md` §8.30. `scripts/security_scan.py` is stdlib-only and runs as
one command; `scripts/security_baseline.json` carries what was reviewed;
`tests/test_security_scan.py` is 61 tests.

**A "keep the number from growing" gate needs three things, and each one was missing from
a plausible first draft.**

1. **Resolve, do not inspect the host.** `pip install --dry-run --report` per requirements
   file, so the scan sees the image's closure. A package importable on the scanning host
   but absent from the image must not appear, and an unpinned transitive one must not hide.
2. **A safe-interpolant list, not "not an f-string".** The first SQL rule treated a bare
   `ast.Name` as a module constant, so `f"SELECT * FROM {table_name}"` passed — the exact
   injection shape. Only literals, attributes, ALL-CAPS names and numeric casts count now.
   Tightening it found two sites the loose rule had missed in `conversation/archive.py`.
3. **A key that names the statement, not the file.** SAST keys end in a digest of the
   offending segment. Keyed by file, baselining one reviewed interpolation would silently
   accept the next one added to the same file — a per-file exemption wearing a per-finding
   costume. Mutation-checked both directions.

**Exit 2 is a first-class outcome.** 0 clean, 1 findings, 2 could not complete. An
unreachable OSV must never render as "no vulnerabilities" — that is the worst failure mode
a security gate has. Same reason the scan asserts floors on what it read: zero findings
from zero input is indistinguishable from a clean tree (§8.25's lesson, again).

**The baseline expires.** Every entry carries `why`, `scope`, `review_by`, and the gate
also fails when an entry matches nothing — so fixing a finding *forces* deleting its entry.
An exemption that cannot be outlived is a permanent permission.

**Carried, not fixed**: 6 npm advisories (Preact 10.27.2 → 10.27.3+, four PostCSS → Next
16, Playwright 1.49.1 dev-only), all with a 2026-12-17 review date. The preact bump was
applied, built and tested clean, then **reverted**: a shipped bundle change moves
`sizes.json` and belongs to the widget rows. Do not redo that experiment to "fix" the
baseline; the blocker is the widget row, not the scanner. Container rebuilds on base-image
updates are the *other* half of §19.1 and are not in this row.

**`widget-browser` failed on `0a038c1` and passed on `fe021a3`** with the widget subtree
byte-identical (`7ed153c` on `16b39e2`, `0a038c1` and `fe021a3`) — the two commits differ
only in Markdown, so this is the WIDG-11 Escape race firing, which `1e72bc5` and `f72a2a9`
also hit on that subtree. Job logs need admin (HTTP 403), so the step conclusion is the
evidence available. The defect is still unfixed and still owned by the widget row.

**M6 still open** (as of WP-28): `OPS-01..05`, `OPS-08`, and the rest of WP-25's §27 sweep.

## Loop state (updated 2026-11-17, M6 / WP-27) — SEC-12, the US notice

**SEC-12 is done** (code `5dbed5a`, registry `db436d1`; CI green on both jobs, runs
44168173910 and 44170165977). Reasoning in `docs/PLAN.md` §8.29. Third document in the
SEC-11 pattern: `docs/compliance/us-state-privacy-notice-template.md`, rendered from a new
`app/compliance/us_state.py`, guarded by `tests/test_us_state_notice.py` (28 tests).

**The sentence that was false for one read.** The draft notice said the widget "writes
only session-scoped values". `shared/identity.ts` has a `DURABLE_KEY` in `localStorage`
and `app.tsx` a "Remember this conversation on this device" checkbox — R42's opt-in,
shipped and undocumented in the notice. It is now described, its label is quoted from the
source, and a test asserts `remember()` is the only writer of that key. A claim about code
in another language is the claim most likely to be wrong; generate it or test it, do not
word it.

**A mutation caught a vacuous test.** `opt_in_keys` first scanned *string literals* only,
so `train_on_conversations: bool = False` on `RetentionSettings` — an annotated field —
passed. It now collects `ast.AnnAssign` targets too and the same mutation is red. When a
guard reads source with `ast`, remember that a field may be a key, an assignment target,
or an annotation, and a scan that sees one shape proves nothing about the others.

**Statutes are not derivable.** The 45-day window is `CCPA_RESPONSE_DAYS`, a constant
citing §1798.130(a)(2), because the repository cannot be the source of truth for a
statute. Everything else in the notice is derived: categories from the live catalog,
sale/share from asset refs and both `package.json` manifests, no-training from
`providers._payload`'s keys and the endpoints called, deletion from the erasure route and
`retention.policy`.

**Category K is "not collected", deliberately.** A confidence score describes the answer's
support, not the consumer; calling it an inference would put a profile in a document where
none exists.

**What it refuses to claim**: deletion propagation to a model provider is unimplemented,
and DPA §5.2 plus the notice both say so. Five `[[FILL: ...]]` markers remain, and no
lawyer has read either document.

**M6 still open**: `SEC-13`, `OPS-01..05`, `OPS-08`, and the rest of WP-25's §27 sweep.
`widget-browser` was **green** on both shas above — the §8.26/§8.27 Escape flake did not
fire, which changes nothing about the defect being unfixed.

## Loop state (updated 2026-11-17, M1 / WP-26) — KNOW-10, the last M1 row

**KNOW-10 is done** (`1b7c984`, registry row cites it; reasoning in `docs/PLAN.md` §8.28).
HALT-03 had blocked it since 2026-11-16 and was closed by the **§8.24 RULINGS** of
2026-11-17 as **option B, weighted boost** — uploads ×1.15, faq ×1.10, crawl ×1.00,
weights in tenant config. The ruling text was already in PLAN when this WP started, so
this was not a scope call by the agent.

**There are two sections numbered 8.24.** `## 8.24 RULINGS` (line ~1356) and
`### 8.24 WP-22` (line ~1464). Citing "PLAN 8.24" without the word RULINGS is ambiguous,
and the first draft of this WP did exactly that. Cite `§8.24 RULINGS`.

**The boost multiplies `rrf_score`, never `score`.** `score` is the dense similarity the
confidence floor is defined against; boosting it would let source kind lift a chunk over
the floor and turn a ranking preference into a confidence claim. A test asserts `score` is
unchanged. Stage order is RRF → priority → rerank → floor, and with reranking on (the
default) priority only decides the top-20 window — the reranker still owns final order.
Do not describe it as the final ranking.

**Mutation-checked, unlike some earlier guards.** Deleting the `apply_source_priority`
call fails the end-to-end ordering test 3/3; swapping the sort for hard tiering fails the
strong-relevance test. Both were run, not assumed.

**The eval gate cannot see this feature.** `tests/eval/run_eval.py:62` seeds every source
as `'upload'`, so every candidate gets the same weight and the gate is structurally blind
to a priority regression. The ruling calls the eval harness "the proof" that relevance
beats tier; today that proof is a unit test, not the gate. Making the golden set
multi-kind is the follow-up the ruling implies, and it is recorded as a gap in §8.28
rather than claimed.

**M1 is now clear of open rows.** M2/M3/M5/M6 remain as recorded in the blocks below;
G4 still blocks M3 work until the operator calls the M2 gate, and the M5/M6 blocks are
unchanged by this WP.

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
