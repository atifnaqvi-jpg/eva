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
