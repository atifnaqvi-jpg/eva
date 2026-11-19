# Chatbot Platform — OpenHands Development Plan (v1, awaiting rulings)

**Source:** `chatbot-platform-blueprint.md` (workspace, 58,754 bytes, §1–§28 complete — verified structurally)
**Build agent:** OpenHands headless on the Oracle VM (<oracle build vm removed from public repo>), one WP per run, always-approve constrained by prompt
**Status:** DRAFT — do not execute until the 4 rulings at the end are answered

---

## 1. Ground Rules

1. **Greenfield, no existing code.** Verified 2026-11-15: `~/work/` holds only fixtures/test data; `~/workspace/project/` hex dirs are unrelated canvas projects. Nothing to inventory further.
2. The blueprint is the **single source of truth**. This plan operationalises it; where plan and blueprint disagree, the blueprint wins and the loop halts to report the conflict.
3. **Repos live in git from day 1** at `~/work/<codename>` on the build VM. Conventional commits (`wp-<id>: ...`), explicit `git add <files>` (no `-A`).
4. **OpenHands headless is always-approve.** Every WP prompt therefore carries hard constraints: stay inside the repo, no destructive ops, no new dependencies without a ruling, no network services beyond declared test needs.
5. `talos-openhands.sh` runs are launched nohup + polled; never hold SSH. Runs execute on **<build-vm>** via the gateway alias.
6. `gh`/push lives on the gateway, not the VM: `git bundle` backups after each milestone gate, bundles shipped to the gateway, remotes added when the user names a GitHub repo.

## 2. Gate Constitution (the user's zero-deviation requirement)

These gates bind every run. The loop treats them as rails, not suggestions.

- **G0 Environment gate.** **RESOLVED 2026-11-15.** Build platform = **Netcup VM** (user ruling; SSH alias `<build-vm>` from gateway). Verified: 8 cores x86_64, 15 Gi RAM (14 Gi avail), 194 GB free disk, OpenHands canvas (agent-server 1.62.0, automation 1.32.1), headless CLI `openhands` v1.16.0 (uv tool, installed 2026-11-15), live LLM profile `openai/deepseek-v4.1-flash:1017` @ `https://ollama.com/v1` (real key in `~/.openhands/settings.json`; legacy `agent_settings.json` holds a masked value — wrapper patched to read live settings, both backups kept). Headless smoke run passed: `NETCUP_SMOKE_OK`, exit 0.
- **G1 Frozen architecture.** `docs/ARCHITECTURE.md` is generated from the blueprint at Sprint 0 and **checksummed in CI** (`tests/test_locked_docs.py` asserts the recorded SHASUM). The agent may not edit it. Any change requires a user ruling, which deliberately updates the recorded checksum. Same mechanism covers `docs/STACK.md` (the locked stack table with rejected alternatives — agent may not re-evaluate rejected options without a ruling).
- **G2 Registry spine.** `docs/FEATURES.md` (generated in WP-00) is the only task ledger: one row per feature, `{id, domain, source: blueprint §, milestone, status: backlog|building|done|skipped}`. A WP may move only the rows it names. Done = code merged + tests green + registry row moved + commit hash reported. Skipped rows need a written reason.
- **G3 Halt conditions (ruling-class).** The loop must halt with `HALT(ruling): <question>` and do nothing further when:
  1. A new dependency/package seems needed.
  2. A schema change beyond blueprint §16 or an API contract change beyond §17 is required.
  3. An MVP scope add/remove is suggested (R1–R45 are frozen).
  4. A blueprint row is unimplementable as written or the spec is ambiguous — halt stating both readings.
  5. An eval/test gate stays red after 3 focused attempts.
  6. Any destructive operation or anything outside the repo would be needed.
  Halt ≠ failure: it is the designed mechanism. Rulings arrive as pulses quoting the user's answer; the loop then continues.
- **G4 Milestone gates.** Each milestone has one observable "Done when". No WP from milestone N+1 starts until N's gate passes. Gate evidence: test counts + registry row counts + commit hashes, not claims.
- **G5 Path discipline.** Each WP prompt lists the files/dirs it may touch. Anything else touched must be reported explicitly in the run summary ("outside-scope touches: none" is the expected default).
- **G6 Evaluation gates are merge gates.** The AI eval suite (WP-10) and security suite (WP-22) block further WPs in their milestone on regression; tolerance is recorded in the harness config, changes to tolerance = ruling.

## 3. Knowledge-Base Sync (at Sprint 0)

| Source (gateway workspace) | Destination in repo | Seeds |
|---|---|---|
| `chatbot-platform-blueprint.md` | `docs/ARCHITECTURE.md` (pruned to §§2, 4.1, 7, 8–20, 26 — decision content only; market/history sections stay in `docs/spec/blueprint-full.md`) | G1 frozen doc, WP reference for every module |
| This plan | `docs/PLAN.md` | Loop's standing brief; G2/G3/G5 reference |
| Requirements register (blueprint §2.1 table) | `docs/FEATURES.md` seed (R-ids as source columns) | WP-00 |

**Not copied:** nothing — no binaries or credentials involved. The DocuPro project and its research repo are unrelated and untouched.

## 4. Product Definition (locked, abbreviated)

Multi-tenant SaaS "AI support concierge" for small businesses: JavaScript embed widget; RAG over uploaded files (PDF/Word/Excel/CSV/PPT/txt/md, priority-ordered), website crawling, FAQ entry; hybrid retrieval + grounded answers with citations; proactive + dynamic suggested questions; email+form human handover; admin dashboard (config, knowledge, conversations, analytics, settings); read-only AI; English-only; email+password auth; audit logs; GDPR/CCPA posture built in (widget stores nothing before the visitor opens the chat); streaming responses; rate limits; session timeouts; CSAT; transcript export; webhooks; visitor file upload; business-hours mode.

Deferred (interfaces only, no implementation): voice (`VoiceProvider` protocol), business connectors (`Connector` protocol, read/write separation), SSO (schema-ready), single-tenant mode, billing.

## 5. Stack (locked — rejected options are ruled out, do not revisit)

| Layer | Choice | Rejected / notes |
|---|---|---|
| Backend | Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic | NestJS rejected (weaker doc/ML ecosystem) |
| Dashboard | Next.js 15 + Tailwind + shadcn/ui | Nuxt rejected (smaller ecosystem) |
| Widget | vanilla-TS loader (<10 KB) + iframe Preact app | Web Component / injected React rejected (CSP + host-page conflicts) |
| DB | PostgreSQL 16 + pgvector (relational + vectors, one engine) | Qdrant named as the migration trigger if >10M chunks or filtered-search degradation; Pinecone/Weaviate/Milvus rejected (cost/ops) |
| Queue | Redis + arq | — |
| LLM abstraction | **LiteLLM only** (models as config strings) | Direct SDKs per provider; **no LangChain/LangGraph/LlamaIndex in the conversation path** (overkill + churn); LlamaIndex reconsidered ONLY for ingestion parsing if hand-rolled parsing proves insufficient — that itself is a ruling-class halt |
| Embeddings | BGE-M3 self-hosted (MIT, dense+sparse) **pending Ruling 2** | Managed upgrade path: voyage-3-large / Qwen3-Embedding-8B |
| Parsing | pypdf + pdfplumber, python-docx, python-pptx, pandas; Tesseract optional | — |
| Crawling | httpx + trafilatura (static; JS-rendering = V1, documented honestly) | Playwright deferred |
| Secrets | age/sops-encrypted files behind a `SecretVault` interface | Vault/KMS later |
| Deploy | Docker Compose + Caddy (TLS) | Kubernetes rejected (no justification at this scale) |
| CI | GitHub Actions (+ lock-checksum test, eval gates, isolation tests) | — |

## 6. Feature Registry (WP-00 generates `docs/FEATURES.md`)

Domains with mandatory minimum row counts (WP-00 output is verified against these):

| Domain | Rows (min) | Bluepring source |
|---|---|---|
| Auth & tenancy | 5 | R13, R28, R29 + §16.3, §19.1 |
| Knowledge ingestion | 6 | R18 + §10 |
| Retrieval & RAG | 5 | §10.3–10.5 |
| Conversation engine | 8 | R25, R27, R35, R37, R40 + §10.4 |
| Widget | 9 | R23, R34, R38, R39, R41, R42, R45 + §12 |
| Handover | 3 | R19, R43, R44 + §13 |
| Admin & analytics | 7 | R26, R31, R32 + §18 |
| Security & compliance | 6 | R30 + §19 |
| Ops & observability | 3 | §22 |
| Future-ready interfaces | 3 | §14 VoiceProvider, §15 Connector, SSO-ready schema |

≈ 55 rows minimum. Each row cites its blueprint § and R-id. Statuses move only per G2.

## 7. Milestones and Work Packages

One WP per headless run. Gates are observable outcomes. Effort = OpenHands working sessions (matches blueprint's 22–37 estimate).

**M0 Foundations** — *Done when: `docker compose up` serves healthy API+DB+Redis+worker on a clean VM; new-tenant registration works; RLS isolation adversarial tests pass; audit log records an admin mutation; CI green including G1 lock test.*

**M0 GATE — GREEN (2026-11-16, `3c0530c`).** Evidence per criterion, all reproduced on the running stack:

| Criterion | Evidence |
|---|---|
| `docker compose up` healthy | All 6 services healthy: api, caddy, dashboard, postgres, redis, worker |
| New-tenant registration works | `POST /api/v1/auth/register` → 201 in 0.63 s at production Argon2 params; login 200; `/api/v1/auth/me` returns the session's user/tenant/role |
| RLS isolation adversarial tests pass | WP-02/03 suites (header/query/body tampering cannot change tenant) |
| Audit log records an admin mutation | `tenant.registered` + `user.created` + `auth.login_succeeded` visible via `/api/v1/audit-logs`; append-only enforced by DB grants (UPDATE/DELETE/TRUNCATE denied for `eva_app`) |
| CI green incl. G1 lock test | `test` = success on `3c0530c` |
| Full suite | **117 passed**, 1 third-party deprecation warning |

Closed at the gate: AUTH-03 (HALT-01 → option A), SEC-01 (auto-HTTPS, HSTS over TLS only, verified live: HTTP/2 200 + `:80`→308), FUT-01 (`VoiceProvider` + reserved 501 endpoint), FUT-02 (`Connector` + empty registry). All M0 rows in `docs/FEATURES.md` are `done`.
- WP-00: repo init; docs bundle (§3); FEATURES.md registry; G1 checksum test; CI skeleton.
- WP-01: compose stack skeleton + healthchecks + Caddy stub.
- WP-02: full §16 schema via Alembic + Postgres RLS policies + composite indexes; **isolation adversarial test suite lands here, not later**.
- WP-03: auth (Argon2id, cookies, RBAC) + tenant-scoping data layer.
- WP-04: audit module + middleware + admin audit endpoint.

**M1 Knowledge** — *Done when: a sample PDF + a crawled page + an FAQ ingest to tenant-scoped chunks end-to-end; publish/rollback swaps versions atomically; preview answers from draft versions.*
- WP-05: parsers + cleaner + structure-aware chunker (fixtures-based tests).
- WP-06: `VectorStore` interface + pgvector impl + embedding pipeline (per Ruling 2) + arq worker.
- WP-07: crawler (domain cap, robots.txt respect) + FAQ CRUD.
- WP-08: document versioning + publish/rollback + test-preview API.

**M2 Conversation** — *Done when: streaming grounded answer with citations over hybrid retrieval; refusal below confidence floor; eval harness green on golden set and demonstrably blocks a seeded retrieval regression; provider fallback survives a simulated outage.*
- WP-09: hybrid retrieval (HNSW + FTS + RRF + optional rerank) + confidence floor.
- WP-10: eval harness + golden dataset v1 (50–100 QA pairs) wired to CI (G6).
- WP-11: conversation turn loop + memory + grounded prompts + SSE + citations + refusal.
- WP-12: proactive/dynamic suggestions + CSAT endpoints.
- WP-13: LiteLLM providers (Ollama Cloud, DeepSeek) + BYOK vault (write-only, encrypted) + fallback chain + circuit breaker + per-tenant budgets + rate limits + session timeouts.

**M3 Widget** — *Done when: embed snippet runs on a test site; browser storage audit shows nothing stored before click; mobile + a11y matrix passes; uploads constrained and safe.*
- WP-14: loader + iframe app + SSE + postMessage bridge.
- WP-15: customisation + session resume + mobile full-screen + focus trap/ARIA.
- WP-16: session tokens + domain verification + uploads (≤5 MB, type-sniffed) + widget-side rate limits.

**M4 Handover** — *Done when: a staged handover email with transcript+summary arrives from a real conversation trigger; signed webhook fires; offline mode follows business-hours config.*
- WP-17: triggers + contact form + EmailChannel + transcript compile.
- WP-18: webhooks + business-hours/offline behaviour.

**M5 Dashboard** — *Done when: the owner script — sign up → create bot → add knowledge → test → customise → copy snippet — completes without developer help; export produces transcript files; analytics reconcile with DB counts.*
- WP-19: conversations list/transcript/export + analytics endpoints + screens.
- WP-20: knowledge admin UI (uploads/crawl/FAQ/status/preview/publish) + onboarding checklist.
- WP-21: settings (BYOK display, domains + verification, retention, audit view, snippet + copy guide).

**M6 Hardening & launch** — *Done when: blueprint §27 checklist fully green with evidence.*
- WP-22: security suite (prompt-injection corpus incl. indirect injection via documents, upload fuzzing, authz bypass).
- WP-23: k6 load test (50 concurrent SSE) + fixes.
- WP-24: retention purge + backup/restore drill (timed) + alerts + runbook.
- WP-25: compliance templates (privacy notice, DPA skeleton) + AI-disclosure line + §27 sweep.

## 8. Execution Model (the loop's standing rules)

- Run: `ssh <build-vm> 'cd ~/work/<codename> && nohup ~/bin/talos-openhands.sh "WP-<id>: <task>" > /tmp/wp-<id>.jsonl 2>&1 &'` then poll.
- WP prompt template (every run): task paragraph; `Read docs/ARCHITECTURE.md + docs/FEATURES.md rows <ids> first`; `Constraints: stay inside repo; no new deps (halt instead); no destructive ops; touched-files report; done = tests green + registry rows moved + commit "wp-<id>: ..."`; `HALT(ruling): X` protocol per G3; report files/tests/rows/hash.
- Milestone close: `git bundle` → copy to gateway; gate review by Talos (not the loop) before unblocking next milestone.
- Dashboard-facing progress: progress card updated on gate crossings and halts only (no per-run noise).
- Test-only dependencies (pytest family, httpx for Starlette TestClient) may be added with explicit disclosure in the turn report; runtime dependencies still require HALT(ruling).
- **RULING (2026-11-15 23:58 UTC): HALT-01 approved as option A** — `argon2-cffi` is an accepted *runtime* dependency (granted exception to G3). Production hashing uses the C implementation at OWASP parameters (64 MiB, t=3, p=4); the in-repo pure-Python Argon2id remains only as a labelled fallback for environments without the wheel.
- **RULING (2026-11-16 10:39 UTC): HALT-02 part 1 granted as option 1A (G3 dependency grant)** — `pypdf`, `pdfplumber`, `python-docx`, `trafilatura`, `lxml` are accepted runtime dependencies; `pandas` and `python-pptx` remain deferred until CSV/PPTX ingestion is actually implemented.
- **RULING (2026-11-16 10:39 UTC): HALT-02 part 2 decided — self-hosted BGE-M3, `EMBEDDING_DIM=1024`**; the managed-API override is closed and not to be re-litigated. WP-06 must additionally *exercise* the dimension-swap path (re-embed at a different dimension, publish atomically, roll back), not merely document it.

### 8.1 HALT(ruling) register

**HALT-01 — Argon2id runtime dependency (raised WP-03, `64bdea3`).**
`argon2-cffi` (wheels: `cp310-abi3-manylinux`, valid on py3.12) is a **runtime** dependency, so it needs a ruling before use.

A pure-Python Argon2id was written instead (`backend/api/app/security/argon2.py`) and **is correct**: all three RFC 9106 §5 vectors (Argon2d/i/id) match exactly. It is however not deployable, measured on the build VM:

| Parameters | Wall time per hash | Python peak RSS |
|---|---|---|
| 32 KiB, t=3, p=4 (RFC vectors) | 0.06 s | — |
| 4 MiB, t=3, p=4 | 8.9 s | 22 MiB (6x the block memory) |
| 8 MiB, t=3, p=4 | 18.0 s | — |
| 64 MiB, t=3, p=4 (OWASP/RFC §4 default) | ~2.6 min (linear ~2.4 s/MiB) | ~356 MiB |

Two blockers, both disqualifying: (a) a ~2.6 min login is unusable, and an unauthenticated endpoint spending minutes of CPU per request is a trivial DoS; (b) peak RSS ~356 MiB against the `api` container's 512 MB limit — a live register call was **OOM-killed**.

Options: **(A)** accept `argon2-cffi` as a runtime dependency (recommended: battle-tested, constant-time, ~50 ms at 64 MiB, bounded memory); **(B)** keep the in-repo implementation and lower cost parameters — rejected, because the only values that fit the time/memory budget are far below any defensible password-hashing strength, and the memory-hardness that makes Argon2id useful is lost; **(C)** ship PBKDF2-SHA256 via stdlib `hashlib.pbkdf2_hmac` — not Argon2id, so it contradicts §19.1/blueprint.

Blocked until a ruling: AUTH-03 marked `building` in `docs/FEATURES.md`. Everything else in WP-03 (sessions, cookies, rotation, RBAC, tenant scoping, isolation tests) is complete and independent of the hash backend — `app/security/passwords.py` sits behind a PHC-string interface, so swapping in `argon2-cffi` touches one module and no stored hashes.

**RULED — option A, granted (2026-11-16 00:02 UTC).** `argon2-cffi` is an accepted runtime dependency. Implemented at `75ffb11`:

- `app/security/passwords.py` uses `argon2.low_level` (reference C, constant-time verify) at OWASP/RFC §4 defaults: **64 MiB, t=3, p=4**. The in-repo pure-Python Argon2id stays as a labelled fallback only for environments without the wheel; `main.py` warns if that fallback is ever active.
- `argon2-cffi==25.1.0` added to `backend/api/requirements.txt`. This is a **granted exception to G3**.
- The RFC 9106 §5 vectors now run against the **production library** via `argon2_ctx` (the high-level helper cannot express the RFC's secret/AD inputs). All three variants match exactly.
- Measured on the build VM at 64 MiB: **hash 133 ms, verify 123 ms, peak RSS 82 MiB** (was ~2.6 min / 356 MiB).

**Second defect found at the gate — concurrency, not cost.** Argon2 is memory-hard, so N simultaneous hashes cost N x 64 MiB, and FastAPI runs sync endpoints on a 40-thread pool. 20 concurrent logins drove uvicorn to 519 MB anon-RSS against the 512 MB container limit and the kernel OOM-killed it (`constraint=CONSTRAINT_MEMCG`, `task=uvicorn`); every request 502'd. Hashing is now serialised behind `ARGON2_MAX_CONCURRENT` (default 1), bounding peak memory to one hash per process. Re-measured after the fix: 20 concurrent logins → **20 x 200, zero restarts, 63 MB RSS** (9.5 s total, ~130 ms serialised). A subprocess test asserts 16 concurrent hashes stay under 3x one hash's memory.

**HALT-02 — M1 ingestion dependencies, raised at the M0/M1 boundary (`ada3a89`).**
M1 cannot be built to spec without runtime dependencies, and G3 requires a ruling before use. Two are independent questions:

1. **Parsing stack (WP-05).** The plan's stack table (§5) names pypdf + pdfplumber, python-docx, python-pptx, pandas, and httpx + trafilatura. §5 is the architecture's *proposed* stack, not a G3 grant — none of these are in `backend/api/requirements.txt`. WP-05 is parsers + cleaner + chunker, so it cannot start without at least pypdf (PDF), python-docx (DOCX) and trafilatura (HTML→text); python-pptx/pandas are only needed for PPTX/CSV.
   - **A (recommended):** approve `pypdf`, `python-docx`, `trafilatura`, `lxml` as runtime deps now (MVP scope is PDF/DOCX/URL/FAQ; the row-group table handling in §10.2 needs pandas later, only when CSV/XLSX ingestion is actually built).
   - **B:** approve the full stack table in one go (adds python-pptx + pandas up front, ~30 MB of unused deps in the MVP image).
   - **C:** defer WP-05 and start WP-07 (crawler + FAQ CRUD) instead — but the crawler needs `trafilatura` too, so this does not avoid the question.

2. **Embedding backend (Ruling 2, blocks WP-06).** Still unresolved in PLAN §5: BGE-M3 self-hosted vs a managed API (voyage-3-large / Qwen3-Embedding-8B). This decides vector dimension N — and §10 says a model change means **new chunks / re-ingestion, never in-place mutation** — so the `chunks.embedding vector(N)` migration cannot be written until it is ruled. It also decides whether the MVP needs a GPU/sidecar, a provider API key, or both.
   - **A:** managed API (voyage-3-large or Qwen3-Embedding-8B) — no GPU, no self-hosted model server, fastest to a working M2; costs per token and sends tenant content to a provider.
   - **B:** BGE-M3 self-hosted — no per-token cost and no data egress, but needs a model server and ~2 GB RAM, which the current 512 MB api container and single-VM compose stack do not have.

Raised at `ada3a89`. M0 is green and nothing regressed; no M1 work has started. Waiting on a ruling for (1) and (2).

**HALT-06 — Domain verification mechanism unspecified (raised WP-19, `781125f`).**
`POST /widget/session` verifies the request `Origin` against `bot_domains.domain WHERE
verified IS TRUE`. Nothing writes `bot_domains`, and `POST /api/v1/bots/{id}/domains`
(ARCHITECTURE §17.4) is not implemented — so **every tenant registered since the schema
was written has zero verified domains and the widget refuses every origin**. Confirmed
live, not inferred: the frame renders "this widget has no allowed domains configured" and
`/widget/session` answers 403.

§16.1 fixes the columns (`verified bool, verification_token`) and §17.4 names the route,
but neither says **how** a domain becomes verified. ADMN-12 is shipped and its snippet is
verified working up to this point, so this is now the single thing between a new signup
and a working chat.

1. **Self-asserted.** Adding a domain sets `verified = true` directly. Cheapest, and makes
   the widget work today; but the `verified` column and `verification_token` become
   decorative, and any tenant can register another company's domain and serve their bot
   there — the precise confusion §16.1 says the token exists to prevent.
2. **Token-checked.** Adding stores a token and leaves `verified = false`; a separate step
   checks it and flips the flag. This is what the schema was built for. Unspecified by the
   blueprint: the mechanism (a `/.well-known/` file, a DNS `TXT` record, or an HTML meta
   tag), the timeout/retry policy, and whether the API may make outbound requests at all —
   an outbound fetch is a new server-side request surface and §21.2 lists SSRF as a live
   risk, so I am not choosing it unilaterally.

**Not shipping (1) as a stopgap.** It would make the demo work while removing the only
control that prevents cross-tenant widget placement, and nothing in the product would
look wrong afterwards.

`HALT(ruling): Domain verification — option 1 (self-assert) or option 2 (token-checked)?
If 2, which mechanism (well-known file / DNS TXT / meta tag) and what timeout+retry
policy?`

**HALT-08 — OPS-03 needs a metrics library and two new containers (raised WP-31, 2026-11-18).**
OPS-03 asks for "Prometheus metrics (request rates, SSE stream health, LLM latency/error by
provider, token usage by tenant, queue depth, ingestion failures, DB pool saturation) +
light self-hosted Grafana". Every part of that sentence is a change this loop may not make
on its own authority. Measured on the running stack, not inferred:

| Fact | Measurement |
|---|---|
| No Prometheus client anywhere | `prometheus_client` is absent from the `api`, `worker` and `embed` images (`importlib.util.find_spec` → `None` in each), absent from the local venv, and **no pinned requirement in the installed closure names a Prometheus client** (re-walked `importlib.metadata` over all 64 distributions: zero matches) |
| The free alternative cannot export | `opentelemetry-api` 1.45.2 *is* present (hard `Requires-Dist` of pinned `fastapi==0.163.1`), but without the SDK `get_meter()` returns `_ProxyMeter` and `create_counter()` returns `_ProxyCounter` — measured, `hasattr(counter, "collect")` is `False`, so there is nothing to scrape. This is OPS-02's gap in a second place: instrumentation with no transport |
| No metrics service in the topology | §20.1 lists Caddy, app, dashboard, worker, postgres, redis. `compose.yaml` matches it exactly (7 services incl. `embed`); there is no `prometheus` and no `grafana` image anywhere in the repo |
| The worker cannot be scraped | `worker` runs `arq` only — `/proc/1/cmdline` is `arq worker.settings.WorkerSettings`, and `docker compose port worker 8000` answers "no port". "Queue depth" and "ingestion failures" have no HTTP surface on the process that owns them |
| One process today, two in the architecture | the `api` container runs a single uvicorn process (no `--workers`), while §20.1 specifies "app (FastAPI, uvicorn, **2 workers**)" |

Four separate G3 classes are engaged, which is why this is a halt and not a scoping
judgement:

1. **G3.1 — a new runtime dependency.** A metrics library must be added to
   `requirements.txt`. HALT-05 is the precedent that matters: `litellm` was named *in the
   locked stack table* and still required a ruling before it could be installed, and the
   ruling chose a hand-written client instead. `prometheus_client` is not even in the stack
   table, so it has strictly weaker standing than `litellm` had. OPS-02's own record already
   predicted this: *"that needs OPS-03's stack plus a ruling on a collector container."*
2. **G3.2/G3.3 — a topology and contract change.** Two container images (Prometheus,
   Grafana) are added to a topology §20.1 fixes, plus a new `GET /metrics` route that §17
   does not list. Container images are also new supply-chain surface: `scripts/security_scan.py`
   resolves *Python requirements*, so two new images add CVE sources the gate does not
   currently see.
3. **G3.4 — genuinely ambiguous where the spec is silent.** The blueprint says *that*
   these metrics exist, never *how* the two non-HTTP facts (queue depth, ingestion failures)
   reach the scraper, and never what a "light" Grafana dashboard contains.
4. **G3.6 — a secret must be provisioned.** Grafana needs an admin credential. The charter
   permits that only through `.env`/the secret store, so it is an operator action, not
   something this loop can invent.

**Reading A (recommended) — standard stack, one scrape target.** Pin `prometheus_client`
in `backend/api/requirements.txt`; add `prometheus` + `grafana` services to `compose.yaml`
with the resource limits every other service carries; expose `GET /metrics` on the API only,
reachable from the Prometheus container and *not* routed through Caddy (Prometheus scrapes
it internally, so it never becomes a public endpoint and needs no auth of its own — which is
the only way §22's "access control on observability" can be honoured for a scrape endpoint
at all); and have the **API** export the worker-owned facts by reading Redis (arq queue
length) and the knowledge tables (failed versions) at scrape time, so there is exactly one
scrape target and the worker keeps its no-HTTP design. `queue depth` becomes an API-exported
gauge instead of a second exporter, which also means no pushgateway (a third container and a
second library).

**Reading B — no new Python dependency.** Hand-write the Prometheus text exposition format
in-repo. This is technically possible and I recommend **against** it, for a reason that is
not aesthetic: **the format is the easy half and aggregation is the hard half.** The moment
the API runs the 2 uvicorn workers §20.1 specifies, in-process counters live in separate
processes and each scrape sees only ~1/N of the traffic — a metric that is silently wrong
under the documented topology is worse than no metric. `prometheus_client`'s multiprocess
collector (shared-mmap directory) exists specifically for this, and hand-rolling it plus
label escaping plus histogram buckets is how a metrics layer becomes a bug farm. Note also
that Reading B does **not** avoid the Prometheus and Grafana containers, so it does not
discharge the topology question either.

**Sub-questions the ruling should also settle**, because each is a decision I should not
make silently:

- **`/metrics` exposure.** Internal-only (not in `caddy/Caddyfile`) is my recommendation.
  If it must be public, it needs a bearer token or an allowlist, and that is a contract
  decision.
- **Label cardinality.** "Token usage by tenant" puts a tenant UUID in a label. At §20.2's
  ceiling (≤50 businesses) that is acceptable; a *conversation* id in a label is not, and
  "request rates" must key on the **route template** (`/api/v1/conversations/{id}`), never
  the raw path, or cardinality grows with traffic. Flagging so the ruling is not read as
  approving per-conversation metrics.
- **SSE stream health** has no defined metric. I would count streams started / completed /
  aborted-by-disconnect, since disconnects are the failure the turn loop already handles.
- **Grafana scope.** "Light" is unspecified: a provisioned Prometheus datasource plus 2–3
  dashboards checked into the repo (so they are reviewable and testable) versus a
  hand-clicked dashboard that lives only in a container volume.
- **Grafana secret** must be set in `.env` by the operator; no default is acceptable, on
  the same reasoning as `WIDGET_TOKEN_SECRET`.

`HALT(ruling): OPS-03 — Reading A (pin prometheus_client + add prometheus/grafana
containers + internal-only /metrics + API exports worker metrics from Redis/DB) or Reading
B (hand-rolled exposition, no new Python dependency)? If A: may /metrics stay
internal-only? Should Grafana dashboards be provisioned from the repo?`

**HALT-09 — OPS-04's alerts have no source events, no thresholds and no monitored address (raised WP-32, 2026-11-18).**
OPS-04 asks for five alerts — provider error-rate spike, budget exhaustion, disk usage,
backup failure, queue backlog — "firing to a monitored address", plus a runbook. Measured
on the running stack, three of the five **cannot fire today because the event they would
watch is never recorded**, and the other two have no threshold and nowhere to ring.

The recorded assumption was that OPS-04 is "blocked behind HALT-08 in practice" because it
inherits OPS-03's metrics transport. **That is not the binding constraint.** An alerting
poller does not need Prometheus — two of the five signals are readable from the existing
stack with no new dependency (`redis` is a hard `Requires-Dist` of the pinned `arq==0.28.0`
and is already installed in the `api` image; `shutil.disk_usage` is stdlib). OPS-04 is
blocked on its own grounds, and **granting HALT-08 would not unblock it**:

| Signal | Can it be computed today? | Measurement |
|---|---|---|
| Provider error-rate spike | **No** | No provider error is persisted anywhere: `grep` for provider-error persistence finds none, `logger.exception("llm stream failed …")` writes to stdout JSON only, and §20.1's topology has no log sink (`grep -iE 'loki\|elasticsearch\|cloudwatch\|splunk'` → zero). `usage_records.provider` is NULL on **22 of 22** rows, because the sole `record_usage` call omits `provider=` and runs only on the *answered* path (`service.py:676`) — the degraded path returns at `654`. So the rate has neither a numerator nor a provider dimension |
| Budget exhaustion | **No — the event cannot occur** | `units = (prompt_tokens or 0) + (completion_tokens or 0)` and the only caller passes `prompt_tokens=None, completion_tokens=None` (`service.py:681-682`), so units is **always 0**. Measured: 22 usage rows, `sum(units)=0`, **0** with `units>0`, **0** with a provider; `messages` 48 assistant rows, **0** with `prompt_tokens`/`completion_tokens`/`latency_ms` set. Confirmed live: `check_budget` on a real tenant → `used=0 budget=1000000`, **no exception**. The `BudgetExceeded` branch also `return`s without persisting anything, so even a fired stop leaves no durable event |
| Disk usage | **Yes** | `shutil.disk_usage('/')` inside `api` and `worker` both report `total=336746889216` (host `/dev/vda3` 314G, 51% used) — the one signal that is genuinely buildable |
| Backup failure | **No — the event cannot occur** | **No backup producer exists**: `pg_dump` appears only in the three docs files, there is no backup script, cron or service (`grep` over `*.sh`/`*.py`/`*.y*ml` → none), no backup volume in `compose.yaml`, and no backup table/marker. OPS-09 owns building it. An alert on the failure of a job that does not exist is a dependency inversion |
| Queue backlog | **Yes** | `LLEN arq:queue` = 0 read from `api` and `worker` via `arq.constants.default_queue_name`; retry keys `arq:retry:*` = 0 |

**No threshold is specified for any of the five.** A grep of the blueprint for
`error.rate|backlog|disk usage|80%|90%|threshold` returns only the *answer-confidence*
threshold and the mitigation "Scale worker count"; §22 and §27 name the signals and stop.
"Spike" has no window or ratio, "disk usage" no percentage, "backlog" no depth. Inventing
those is policy, not implementation.

**"A monitored address" does not exist, and the only mail path cannot reach one.** The sole
delivery channel is SMTP, and it is not a real mailbox: `eva-smtp-sink` is **not in
`compose.yaml`** (ad-hoc container, `python sink.py 2525`) and it *writes each message to a
local `inbox/msg-NNN.eml` file and acknowledges it* — mail is captured, never delivered.
There is no `ALERT_*`/operator-address setting anywhere (`grep` → none; `.env` holds 9 keys,
none alert-related; `SMTP_*` are not even declared in `.env.example`). The one real address
in the system, `settings.handover.email` (set for **17** tenants), is the **tenant owner's**
— routing platform alerts there would send operational details to customers.

Three G3 classes are engaged: **G3.4** (thresholds and "monitored address" are undefined
and the backup-failure alert presupposes an unbuilt row), **G3.2/G3.3** (persisting a
per-turn provider outcome is a §16.1 schema change; a durable budget-exhaustion event is
another), and **G3.6** (the alert recipient is an operator provisioning step, on the same
reasoning that made the Grafana admin secret one).

**The runbook was deliberately not written.** Its content is the response procedure for five
alerts that do not fire, at thresholds nobody has chosen, to an address that does not exist;
drafting it would produce a document whose every step is untestable, which is the failure
mode §8.26 already punished ("a test that cannot fail is not evidence"). The operational
half that *is* known — deploy, manual `alembic upgrade head`, rollback — is already recorded
in AGENTS.md, and the rollback drill and backup/restore halves belong to their own rows.

**A measured gap on the "uptime alerting" half of §20.4.** `GET /health` and `/ready` return
**200 inside `api`** but **404 through Caddy** on `/health`, `/api/v1/health` and `/ready`:
`caddy/Caddyfile` routes only `/api/*`, `/widget/*` and `/demo/*` to `api` and sends
everything else to `dashboard`. Compose's container-health probe is unaffected (it calls
`http://127.0.0.1:8000/health` directly), but **any external uptime monitor pointed at the
public origin would report a permanent 404** — it could not distinguish a dead API from a
routing rule. Exposing the probe is a one-line Caddy change, but it puts an unauthenticated
endpoint on the public origin and §17 does not list it, so it is part of the same contract
question rather than something to slip in here.

**Reading A — build the two computable signals now.** A Prometheus-free poller (a second
`arq` cron beside `purge_expired`) emails disk-usage and queue-backlog alerts. This
delivers 2 of 5, invents both thresholds, and sends them to a file-writing sink — so the
row's actual test, *"alerts firing to a monitored address"*, would still be **false**.
**Reading B — fix the sources first, then alert.** Make the token ledger real (thread the
provider's usage through `record_usage`/`_insert_message`), persist a per-turn provider
outcome, build OPS-09's backup, then add a poller and a runbook. That spans three rows and
a schema change, so it is a programme decision, not a work package.

`HALT(ruling): OPS-04 — Reading A (ship disk + queue alerts now, thresholds and recipient
to be chosen by you) or Reading B (treat the dead token ledger and the missing backup as
prerequisites and re-scope OPS-04 after them)? Either way: (1) which mailbox is "the
monitored address", and is a real SMTP provider to be configured first? (2) what are the
five numeric thresholds? (3) should token accounting be fixed as its own row — it makes
R31's ledger structurally zero and the CONV-14 budget stop dead code in every deployment?`

**HALT-10 — ADMN-15's "restore previous version" has no previous version to name, and the rollback it would call unpublishes the rest of the knowledge base (raised WP-45, 2026-11-19).**
ADMN-15 asks for "a 'restore previous version' action beside publish, calling the existing
rollback path". Three measured facts make that control either impossible to build or
actively harmful. All three were reproduced before halting; none is a reading of the source
alone.

**1. `/knowledge/rollback` serves the named version *alone*.** It is a pointer swap to a
one-member set (`router.py:306`, "deliberately **not** additive"), so it discards every
other published document. Measured live through Caddy by `scripts/admn15_evidence.py`:

    after publish A then B: 2 served
    after re-publishing A:  2 served   (True — additive, unchanged)
    after rollback to A:    1 served (status 200)

An owner with five documents published who clicks "restore previous version" loses four of
them, silently, with a 200. A button labelled as a *restore* that *unpublishes* is the
SEC-15 shape one layer up: a control whose name and effect disagree.

**2. The knowledge screen cannot name a previous version, because the listing hides them.**
`GET /knowledge/documents` — the only listing the screen reads — is
`LEFT JOIN LATERAL (… ORDER BY version_no DESC LIMIT 1)` (`service.py:404`), so a document
with three versions appears once, as the newest, carrying only the newest `version_id`.
Pinned by `tests/test_knowledge_version_history.py` (3 tests), and mutation-checked:
dropping the `LIMIT 1` turns 2 of them red. A control needs a value to offer; the `id` of
an older version never crosses the wire.

**3. Re-publishing an older version is additive, not a restore.** Publishing B then A again
leaves both in the served set (measured above, `2 served` both times), because publish
inherits the live members by the HALT-07 ruling (§8.24 RULINGS). There is therefore **no
endpoint in the API that expresses "revert to the previously served state"** — only "add
this one back" and "serve only this one".

Those last two readings are the crux: **the API offers exactly two operations, and neither
is the one the row names.** Reading A of ADMN-15 would wire the button to `/rollback` —
which is implemented, tested and shipped — and ship a control that silently shrinks a
multi-document knowledge base to one document. Reading B would label it "serve only this
document" instead, which is honest but is not a "restore previous version" and does not
close ADMN-05's affordance gap in the terms the row states.

Three further constraints make this a ruling rather than a scoping call:

- **A real previous-version list needs an API change.** Either `GET /knowledge/documents`
  gains a versions array (a response-shape change on an existing route) or a new
  `GET /knowledge/documents/{id}/versions` arrives (§17.5 does not list it). Under ADMN-14's
  lesson — *the type that drops a field is the defect* — a screen that names versions must
  be fed by a route that returns them, not by inferring them.
- **A true "restore" is a stateful undo, and that is a product decision.** Restoring "the
  previously served set" requires remembering the prior set pointer. `knowledge_sets` is
  append-only and every publish/rollback creates a new set, so the *history* exists and the
  undo could be computed (`the set pointed at before the current one`) — but whether a
  restore re-points to that exact set, or re-derives a set from the versions live at that
  time, changes what a subsequent rollback means. §18.2 says "rollback available" and does
  not define the depth of the history, and R26 does not either.
- **The row's premise is that rollback is safe to put in front of a non-developer.** It was
  built for the operator-mediated, single-document model and kept its semantics through
  RAG-11. Exposing it unchanged to an owner is a §21.2-adjacent usability change with a data
  effect, not an affordance.

`HALT(ruling): ADMN-15 — which reading? (A) ship "restore previous version" wired to
/knowledge/rollback, accepting that it serves that one document alone and discards the rest
of the set, with the label and a confirmation saying so; (B) relabel the control "serve only
this document" (honest, no API change, does not close ADMN-05's affordance gap as written);
(C) build a real undo — add a versions listing route and a server-side "restore the
previously served set" operation, which is an API addition under §17.5 and needs a ruling on
history depth. If C: is one step of undo enough, or must an owner step back through several
publishes? And should the served set be re-derived from the versions live at that time, or
re-pointed at the exact prior set?`

## 8.25 RULINGS (2026-11-18, user) — HALT-09 and HALT-08

**HALT-09 — OPS-04: Reading B, with industry-standard thresholds and user-tunable config.**
User: "HALT 09 set industry standard thresholds for now which should be customizable by the
user later on depending on the requirement. Email provider should be easily configured later,
SMTP sink is fine for testing."

Consequences:
1. **Thresholds ship as defaults, not as policy.** Recorded in config with a per-tenant and
   operator override path, so "spike" gains a definition rather than a guess:
   - `provider_error_rate`: >5% of turns failing over a 5-minute window (warn), >20% (critical)
   - `budget_exhaustion`: tenant at 90% of monthly token budget (warn), 100% (critical)
   - `disk_usage`: 85% (warn), 95% (critical) — industry norm; measured current usage 51%
   - `backup_failure`: any failed or missed run (no threshold — binary)
   - `queue_backlog`: arq queue depth >100 for 5 minutes (warn), >500 (critical)
   These are *starting values*, exposed in settings so an operator changes them without a
   code deploy. Changing a default is not a gate event; the row cites them as defaults.
2. **Reading B is the ruling**: the two dead signals are prerequisites, not excuses.
   `provider_error_rate` and `backup_failure` cannot fire because their events are never
   recorded — so OPS-04 gains two dependency rows rather than shipping a lie:
   - **OPS-12 (new)** — persist a per-turn provider outcome (success/failure, provider,
     model, error class) and thread real token usage through `record_usage`, so the ledger
     is not structurally zero. Also unblocks CONV-14's budget stop, which is dead code today.
   - **OPS-09** (existing, still open) — the backup producer whose *failure* OPS-04 watches.
   Only then does the poller (disk + queue first, then provider + backup + budget) mean
   anything. Alert delivery targets the SMTP channel; the sink is accepted for testing, and
   configuring a real provider is a `.env`-only change by design — no code path changes.
3. **Recipient**: `ALERT_EMAIL` in `.env` (operator-owned), never a tenant's handover
   address. Absent `ALERT_EMAIL` means alerts are logged, not sent — an explicit,
   visible degradation rather than silence.

**HALT-08 — OPS-03: LOCAL-FIRST, no Prometheus or Grafana.**
User: "HALT 08: you need to explain me this process, research ways to provide similar
capability locally without any 3rd party like prometheus + grafana, identify the benefits of
both local and 3rd party" — and the operator's recommendation, accepted below.

Ruling: **build OPS-03 without adding Prometheus, Grafana, or a metrics library.** The
capability is delivered from data the application already owns:
1. **The event store is the metric store.** Every turn already writes `messages` (latency_ms,
  prompt/completion tokens, model) and `usage_records` (provider, model, units, cost_micros,
  created_at). A single SQL aggregation over a time window gives request rate, p50/p95
  latency, error rate, token usage by tenant, and cost — no scrape, no exporter, no
  separate TSDB.
2. **`GET /metrics`** on the API renders that aggregation as Prometheus *text exposition
  format* (a rendering choice, not a dependency: ~40 lines of string formatting). Any future
  Prometheus or VictoriaMetrics can scrape it without a code change, so this is not a fork in
  the road — it is forward-compatible with the 3rd-party path.
3. **`GET /admin/metrics/summary`** returns the same numbers as JSON for the dashboard's
  existing analytics screen (ADMN-08/09 already consume this shape), so the operator sees
  charts in a UI that already exists rather than in a new container.
4. **SSE stream health and queue depth** are read from Redis and the in-process counters —
  both already present (`redis` is a hard dependency of pinned `arq`).
5. **`GET /health` and `/ready` still are not exposed through Caddy.** Uptime monitoring uses
  the existing authenticated admin endpoint or an external checker against the public site;
  exposing an unauthenticated probe on the public origin stays a §17 contract change and is
  NOT made here.

**USER CONFIRMATION (2026-11-18 14:20 UTC), quoted:** "Lets keep it local for now with the
possibility to add other tools like prometheus + grafana in the future". This converts the
exit path from a hope into a **binding design constraint**, and OPS-03 is gated on it:

1. `GET /metrics` MUST emit valid **Prometheus text exposition format** (v0.0.4):
   `# HELP`, `# TYPE`, `name{label="value"} number`. A Prometheus server must be able to
   scrape it with zero application changes. This is asserted by a test that parses the
   output with a real exposition-format parser, not by eyeballing it.
2. Metric names and label keys follow the Prometheus conventions (`<subsystem>_<name>_<unit>`,
   `_total` for counters, seconds for durations), so a future Grafana dashboard built on
   Prometheus semantics needs no renaming.
3. **No local-only shortcuts.** Nothing may be formatted in a shape only our dashboard can
   read. The dashboard consumes the same numbers via JSON; the text endpoint is the contract.
4. **Adding the 3rd-party stack later must be additive only**: a `prometheus` + `grafana`
   service block in compose, a scrape config pointing at `api:8000/metrics`, and nothing
   else. If a future operator has to change application code to adopt Prometheus, this
   ruling was violated.
5. Metrics must remain **cardinality-bounded**: tenant id is the only unbounded label, and it
   stays out of the hot path (per-tenant aggregates are computed on request, not stored as
   one series per tenant per metric). A future Prometheus must not be fed a cardinality bomb.

Rationale recorded: Prometheus + Grafana on this box would add two container images
(~300–500 MiB resident plus a TSDB on disk), a new supply-chain surface the SAST gate does
not currently scan, a new `GET /metrics` route §17 does not list, and a Grafana admin
credential the operator must provision — for data that is already in Postgres. The
measured community experience on single-VPS deployments is that this stack is routinely
removed in favour of lighter tools once the dashboard is never opened (see §8.26 research
notes). The local-first design keeps the *exit path* open: `/metrics` in text format is the
standard wire format, so adding Prometheus later is a scrape config, not a rewrite.

### 8.2 WP-06 embedding measurements (2026-11-16, CPU only)

BGE-M3 (`BAAI/bge-m3`, 1024 dims) measured on the dev host, 8 cores / 14 GiB, in a
throwaway venv with `torch==2.15.0+cpu`:

| Metric | Measured |
|---|---|
| Weight download | ~2.3 GB (cached in the `hf_cache` volume) |
| Import + first load | 20-33 s |
| Peak RSS while embedding | 2.3-3.2 GiB |
| Throughput, ~500-token chunks | **0.24-0.33 chunks/s** (batch 1-8) |

Consequences, applied rather than assumed:

- The worker's 512 MiB limit would OOM-kill the model, so `worker` now gets
  `memory: 5g` / `cpus: "2.0"` and a persistent model volume.
- The API image stays free of `torch` — it never embeds, so it keeps its 512 MiB.
- At ~0.3 chunks/s a 300-chunk document takes ~17 minutes. That is acceptable for
  background ingestion but rules out embedding inside a request; hence the arq job.
  If this is too slow for M2, the lever is a GPU or a smaller model, which is a
  product decision, not a tuning one.

### 8.3 Embedding service placement (WP-06/WP-08)

Preview and the (future) chat path both need *query* embeddings, but the API is
capped at 512 MiB and deliberately ships without torch. Two copies of BGE-M3
(3.2 GiB each) do not fit on the host, so the model lives in one place:

- **`embed` service** (`backend/embed`): the only process holding the weights.
  `POST /embed {"texts": [...]}` -> `{"model", "dimension", "vectors"}`.
- **API and worker** both use `RemoteEmbedder`, which rejects a response whose
  `model` or `dimension` does not match configuration. A mismatch here would mean
  ranking against vectors from a different space — plausible output, wrong answers
  — so it fails loudly instead.

This also removes the earlier assumption that the worker owns the model; ingestion
and query cannot drift apart because they call the same service.

### 8.4 Deviations accepted in WP-08

| Architecture | Implemented | Why |
|---|---|---|
| §17.3 multipart upload | raw body + `?filename=` | multipart needs `python-multipart`, which is not an approved runtime dep (G3). Identical bytes, no lost capability. |
| §17.3 `POST /knowledge/faqs` | same path | unchanged |
| query embedding in API | delegated to `embed` service | API memory cap; see 8.3 |

### 8.4.2 Deviations accepted in WP-11 (2026-11-16)

| Architecture | Implemented | Why |
|---|---|---|
| §17.4 `POST /widget/conversations…` (widget session token + `Origin`↔`bot_domains` check) | `POST /api/v1/conversations…` behind the owner session cookie | The widget session token, domain verification and consent banner are WP-16. An unauthenticated SSE endpoint pointing at a support bot is a public oracle over a tenant's knowledge base. WP-16 mounts the same `service.turn_events` on the §17.4 path; the event contract is unchanged. |
| §11.1 LiteLLM client | `LlmProvider` protocol + `UnconfiguredProvider` | WP-13 owns LiteLLM, BYOK, fallback chain and the circuit breaker. Building the turn loop against the vendor SDK first would have coupled the engine to it. `LLM_PROVIDER=unconfigured` is the honest default: every turn degrades to the handover message rather than fabricating an answer. |
| §10.3.5 "handover affordance" on refusal | refusal text offers handover; no contact form yet | The form, email channel and staged notification are WP-17 (M4). The offer is text so the visitor is not told a human is coming when no path to one exists yet. |

Nothing here changes a product behaviour: the same turn loop, prompt contract and
event stream serve the §17.4 routes once WP-16 adds the token layer.

**Resolved in WP-16 (§8.13):** the `/api/v1/widget/...` routes now exist and are
mounted on the same `service.turn_events`, so both rows above are closed. The
owner-cookie routes remain, for the dashboard's own test-the-bot panel.

### 8.4.3 WP-12 notes (2026-11-16)

| Architecture | Implemented | Why |
|---|---|---|
| §18.3 proactive chips | owner-curated in `bots.suggestion_settings`, served by `GET /conversations/bots/{id}/config` | A cold-start visitor gives a model nothing to work from, and a generated chip the knowledge base cannot answer makes a refusal the first thing a visitor sees. |
| §17.4 `POST /widget/messages/{id}/feedback` | `POST /api/v1/conversations/messages/{id}/feedback` | Same WP-16 reason as 8.4.2. |

`suggestion_settings` shape is defined in `conversation/suggestions.py`:
`{"proactive": ["..."], "dynamic": true}`. Bare JSONB, so existing bot rows need no
migration and absent keys mean "no curated chips, generate when possible".

CSAT is one row per message, not one row per vote: re-rating deletes the previous
row first. A visitor who taps the wrong thumb should be able to correct it, and two
rows per message would make every average wrong.

### 8.4.1 HALT-03 — KNOW-10 retrieval-ordering ambiguity (raised 2026-11-16)

KNOW-10 is the last open M1 row and cannot be implemented as written without a
ruling. It is not a missing detail; the two authoritative documents say different
things.

- **R18** (ARCHITECTURE §1, frozen): "Knowledge priority: file uploads > website
  crawling > FAQ entry > cloud storage > API KBs > DB", MVP = first three.
- **§10.3** (Retrieval, frozen): dense cosine + sparse FTS + RRF + optional rerank
  + confidence floor. Ranking is **purely by similarity**; source kind is never
  consulted.

The readings:

- **A — Hard tiering.** Source kind is the primary sort key; similarity orders only
  within a tier. An upload always beats a crawl hit, always beats a FAQ hit.
- **B — Weighted boost.** Source kind scales the score (e.g. upload x1.15, crawl
  x1.05, FAQ x1.00); strong relevance can still win across tiers.
- **C — Tie-break only.** Priority applies only at equal/near-equal score.

Each changes answers, and A can serve an irrelevant upload over an exact FAQ
match, which fights the §10.3 confidence floor and the §10.4 grounding contract.
The choice also has to survive M2, where RRF and rerank reorder candidates — a
tie-break (C) would be erased by RRF, and a boost (B) needs a defined place in the
fusion order.

**Recommendation: B**, applied as a per-source multiplier on the fused score, with
the weights in config so they can be tuned against the WP-10 golden set. It honours
R18's ordering without letting a stale upload outrank a direct answer.

Blocked pending a ruling; KNOW-10 stays `backlog`. M1's own "done when"
(`PLAN §7`) names ingestion + publish/rollback + draft preview, not KNOW-10, and
all three are now proven live (§8.5), so the milestone gate itself is met.

**RULED 2026-11-17 — OPTION B, weighted boost** (§8.24 RULINGS). Implemented as
WP-26; see §8.28. The recommendation above is what the ruling adopted, including
"weights in tenant config, tuned against the WP-10 golden set".

### 8.7 WP-09 notes: reranker is an interface, not a model (2026-11-16)

§10.3.4 requires rerank "on by default, per-tenant configurable" but names no
model, and no reranker is in the approved stack (§5). WP-09 therefore ships the
`Reranker` protocol, the top-20 → top-5 plumbing, the per-tenant toggle and the
degrade-don't-fail policy — all of which are spec'd — and wires **no** production
reranker.

This is deliberate and is not a silent gap: with no reranker supplied, the pipeline
reports `reranked: false` and serves the RRF order, which is a valid configuration
rather than a broken one. Two consequences for the M2 gate and beyond:

- The M2 gate's "streaming grounded answer with citations over hybrid retrieval" is
  satisfiable without a reranker, because RRF still orders candidates.
- Choosing a reranker is a **ruling-class decision**, not a tuning detail: a hosted
  cross-encoder or LLM reranker sends tenant text to a third party, which
  contradicts the self-hosted embedding ruling (Ruling 2) and needs explicit
  consent. A self-hosted cross-encoder (e.g. a BGE reranker in the `embed`
  service) needs a model download and ~1-2 GiB, which the current memory plan does
  not have.

So: **HALT-04 (advisory, not blocking M2)** — which reranker, self-hosted or
hosted? Until ruled, `reranked: false` is the honest production state and the
eval harness (WP-10) measures RRF quality.

### 8.8 WP-10 eval results: two embedders, two honest numbers (2026-11-16)

The harness (OPS-10, §21.2) scores the synthetic golden set (60 cases: 45
answerable, 15 unanswerable) through the real retrieval pipeline on migrated
Postgres.

**Real BGE-M3, threshold 0.55 (the schema default), measured on the live stack:**

| metric | value |
|---|---|
| recall@5 | 1.000 |
| MRR | 1.000 |
| correct-refusal (unanswerable) | 1.000 |
| answer rate (answerable) | 1.000 |

Score separation: answerable best-similarity min 0.633 / median 0.739; unanswerable
max 0.516 / median 0.446 — AUC 1.000, and the optimum threshold is 0.52. The
schema's 0.55 default sits just above the unanswerable ceiling and below every
answerable floor, so the shipped default is *validated*, not merely asserted.
Threshold sensitivity confirms the floor is load-bearing: at 0.30 nothing is
refused (correct-refusal 0.000); at 0.70 a quarter of answerable questions are
refused. Both directions fail the gate.

**CI uses the hashing embedder**, which has no semantics, so its numbers are lower
and are recorded separately in `tests/eval/baseline.json`: recall@5 0.778, MRR
0.649, correct-refusal 0.600, answer rate 0.889 at threshold 0.16 (the measured
optimum for that embedder). This is a regression detector, not a quality claim,
and the baseline file says so.

**Two metric-design errors were caught and fixed here**, both of which would have
produced a meaningless green gate:
- `precision@5` was gated at ≥0.55, but the golden set has one expected chunk per
  question, so precision@5 is capped at 0.2 *by the dataset* — the floor was
  unreachable and the metric measured nothing. Replaced with MRR.
- "Refusal accuracy" counted only unanswerable cases, so a system that refused
  every question scored 1.000. Now paired with `answer_rate`; only a system that
  actually discriminates can satisfy both.

**The gate is proven to fail.** `test_gate_blocks_a_seeded_regression` injects
insertion-order ranking; `test_gate_blocks_a_regression_in_refusals_only` answers
everything. Both are rejected. Separately, a real code mutation (truncating the
dense list to one candidate before fusion, a plausible "perf fix") fails the gate,
and reverting it restores green — verified by hand, not assumed.

**Not measured yet:** answer groundedness (needs an LLM judge — a provider key,
WP-13) and suggestion relevance (no suggestions until WP-12). Both are named in
§21.2 and both are honestly absent rather than faked with a substring heuristic.

### 8.9 WP-11 conversation engine: what is proven, and the one thing that is not (2026-11-16)

`backend/api/app/conversation/` — `llm.py` (provider protocol), `prompt.py`
(grounding + injection contract), `memory.py` (history window), `service.py` (turn
loop), `router.py` (SSE).

**Proven by test, with the guard mutated to prove the test bites:**

| Claim | Test | Mutation that kills it |
|---|---|---|
| A declined question never reaches the model | `test_refusal_never_calls_the_model` | `if result.declined and False` → fails |
| Citations persist on the assistant row | `test_grounded_answer_streams_and_persists_citations` | `citations=None` in the insert → fails |
| Injected text cannot escape the untrusted block | `test_injected_instructions_in_a_document_stay_inside_the_untrusted_block` | removing `untrusted_data_reminder` → fails |
| The delimiter nonce is per-request | `test_context_is_delimited_with_a_per_request_nonce` | fixed nonce → fails |
| The current question is not duplicated into history | `test_model_receives_history_from_prior_turns` | dropping the de-dup slice → fails |

Also covered: prompt-injection containment of an attacker-supplied closing
delimiter, tenant instructions placed below the grounding rules, `system` rows
excluded from replay, provider crash → offline message + handover (never an
exception), a `handed_over` conversation refusing to answer, cross-tenant post
returning `conversation_not_found` with no rows written, and no `tools` parameter
anywhere in the prompt builder (CONV-10 read-only, structurally).

**Not proven: answer quality.** There is no LLM in the MVP stack yet, so every
test runs against a deterministic fake. What is verified is that *the engine hands
the model the right material under the right constraints* — not that the model then
behaves. Answer groundedness as a scored metric needs WP-13 (a real provider) and
the LLM-judge that OPS-10 deferred; claiming it now would be the kind of green
number WP-10 already had to delete twice.

`LLM_DEFAULT_MODEL` is an opaque `provider/model` string (CONV-11), so WP-13
switches provider by configuration and does not touch this module.

Full suite: **218 passed**, eval gate **6 passed**, no regressions.

### 8.10 WP-12 suggestions and CSAT: mutations proved (2026-11-16)

`conversation/suggestions.py` (sanitise/generate), turn-loop wiring,
`GET /conversations/bots/{id}/config`, `POST /conversations/messages/{id}/feedback`.

| Claim | Test | Mutation that kills it |
|---|---|---|
| A refusal never triggers generated follow-ups | `test_refusal_does_not_generate_follow_ups` | dropping the `declined` branch in `_follow_ups` → fails |
| A chip never repeats the question just asked | `test_sanitise_never_repeats_the_question_just_asked` | removing the `key == asked` filter → fails |
| Only assistant messages can be rated | `test_feedback_rejects_a_visitor_message` | `if False` on the role check → fails |
| A second vote replaces the first | `test_feedback_replaces_a_previous_vote` | deleting the pre-insert DELETE → fails |

Also covered: list-marker stripping, 3-chip cap, length truncation, multiline
rejection, single-fragment rejection (no padding), JSON/fenced/bullet parsing,
malformed JSONB tolerance, suggestion-call failure falling back to curated chips,
suggestions persisted on the message row, and cross-tenant 404 on both new routes.

The suggestion call runs via `asyncio.to_thread`: it is a second provider round
trip, and blocking the event loop behind it would stall tokens already sent.

Full suite: **243 passed**, 1 skipped, no regressions.

### 8.4.4 HALT-05 — `litellm` runtime dependency (raised WP-13, 2026-11-16)

CONV-11 names the LiteLLM SDK explicitly ("LiteLLM SDK in the `llm` module",
ARCHITECTURE §11.1). It is not in `backend/api/requirements.txt`, it is not a
transitive dependency of anything already approved, and G3 requires a ruling before
a runtime dependency is added.

The package is large (it pulls `openai`, `tokenizers`, `pydantic` upgrades and a
model-price table) and does far more than this MVP needs: it is a router *and* a
pricing database *and* a proxy client. Two things follow, and they are separable:

* **Blocked on the ruling:** the `litellm`-backed `LlmProvider`, and therefore live
  calls to Ollama Cloud/DeepSeek.
* **Not blocked:** everything else in WP-13 is provider-independent and is built
  against the existing `LlmProvider` protocol — the fallback chain, the circuit
  breaker, task-based routing, per-tenant token budgets, rate limits, the response
  cache, and session timeouts. These are policy layers *around* a provider, so
  writing them first does not presuppose the SDK.

**Status: resolved by option B — `httpx`, no new dependency.** See §8.12. Written
as a halt because the spec names an SDK by name and G3 is about dependencies, not
about intent; the ruling requested was "may we deviate from §11.1", and the
conservative default (no new dependency) already answers it. Recorded here so the
deviation is visible rather than silent.

**Options for the ruling:** **(A)** approve `litellm` as a runtime dependency
(matches §11.1, gives provider fallback and cost mapping for free);
**(B)** implement `LlmProvider` against `httpx` directly with the two MVP providers
hand-written (Ollama Cloud and DeepSeek both expose OpenAI-compatible chat
completions, so this is ~120 lines, no new dependency, and loses the model-price
table — cost per 1k tokens becomes a small config map); **(C)** defer all provider
work.

**Recommendation: B.** `httpx` is already a dependency, both MVP providers speak
the OpenAI wire format, and the value `litellm` adds here — a router between two
providers we can already route between ourselves — is smaller than its footprint.
A is defensible if later BYOK breadth (Gemini, Bedrock, Azure) matters more than
image size.

### 8.11 WP-13 (part 1): resilience and cost controls (2026-11-16)

`conversation/resilience.py` (breaker + fallback chain), `conversation/controls.py`
(rate limits, budgets, cache, task routing), wired into the turn loop and the SSE
route.

**The M2 gate claim — "provider fallback survives a simulated outage" — is proven**
by `test_turn_survives_a_provider_outage`: two providers, the primary raising before
its first token, driven through the real turn loop against the real database. The
visitor still receives a grounded answer with citations, `degraded: false`. The
all-fail path (`test_turn_degrades_to_handover_when_every_provider_is_down`) yields
the offline message with a handover offer and a well-formed `final` event.

| Claim | Test | Mutation that kills it |
|---|---|---|
| A tripped provider receives no traffic | `test_chain_falls_back_and_then_skips_the_tripped_provider` | continue past `CircuitOpen` instead of skipping → fails |
| A spent budget stops the turn before any token | `test_budget_exhaustion_blocks_the_turn_before_it_spends` | no-op the `check_budget` call → fails |
| The cache is bypassed when history exists | `test_cache_is_not_used_when_history_exists` | drop the `if not history` guard → fails |
| Two answers are never spliced mid-stream | `test_stream_does_not_splice_two_answers_after_the_first_token` | fall back after first token → fails |

Load-bearing decisions, each with the alternative stated:

* **Breaker is per-provider and in-process.** The failure it prevents is *slow*
  failure (a provider that hangs), which is invisible to a retry count and fatal to
  every visitor at once. In-process state is correct granularity for a breaker — a
  local backpressure signal, not global consensus — and the seam for Redis is the
  `breaker()` accessor.
* **Half-open probe, not manual reset.** Tripping without an automatic retry means
  an operator must restart the process after a two-minute blip.
* **Fixed-window rate limiting.** It admits up to 2x the limit across a window
  boundary; a sliding window needs a timestamp list per visitor with unbounded
  growth. For a support widget the overshoot is a few LLM calls, which is cheaper
  than the memory. Named in the module so the trade is visible.
* **Budget checked before the turn, on month-to-date usage.** One turn can overshoot
  by its own cost. Reserving an estimate up front needs a lock and an accurate
  estimate; overshooting one answer is the better failure.
* **Cache keyed by tenant + normalised question only**, and used only on the first
  turn of a conversation. Not stemmed or synonym-expanded: two questions differing
  by one word may retrieve different chunks, and one answer for both is a
  correctness bug traded for a hit.
* **Publish invalidates the tenant's cache.** A cached answer predates the knowledge
  the tenant just corrected; serving it for the TTL is the freshness bug they paid
  to fix.
* **Degraded answers are not cached.** Otherwise the outage message outlives the
  outage.

Still blocked: the `litellm` adapter and BYOK encryption (HALT-05). `LLM_PROVIDER`
remains `unconfigured`, so every turn currently degrades — which is the honest
behaviour with no provider key in this deployment.

Full suite: **274 passed**, 1 skipped.

### 8.12 WP-13 (part 2): the provider, on httpx (2026-11-16)

`conversation/providers.py`: `OpenAiCompatibleProvider` + `ProviderProfile` for
Ollama Cloud and DeepSeek, wired into `default_provider()` as a chain built from
whichever credentials exist.

| Architecture | Implemented | Why |
|---|---|---|
| §11.1 LiteLLM SDK | hand-written OpenAI-compatible client on `httpx` | LiteLLM is a router + price table + proxy client; here we route two providers that speak the same wire format, and `httpx` is already approved. Avoids a large dependency in a 512 MB image (HALT-05, option B). |
| §11.2 providers | `PROFILES` for `ollama_cloud` and `deepseek` | Both expose `/chat/completions`; a third is a `PROFILES` entry. |
| §11.1 resolution order | chain built by credential presence, platform default first | A chain must survive one provider being down, so it is built from everything configured — not from one chosen winner. |

Cost of the deviation, stated plainly: no bundled price table, so `cost_micros`
stays 0 until prices are configured; and BYOK breadth (Gemini/Bedrock/Azure) means
new `PROFILES` entries rather than a LiteLLM config line.

Proven rather than asserted — `test_turn_answers_from_a_real_provider_over_http`
drives the real turn loop against a real local HTTP server speaking SSE in 7-byte
pieces, and checks the bot's instructions and the retrieved passages both reach the
request. Mutation-verified: dropping the in-stream `error` frame branch kills
`test_provider_surfaces_an_error_delivered_inside_the_stream` (silent truncation is
the classic adapter bug), and removing prefix stripping kills two tests.

Two real defects were found by these tests, not by review: the dataclass `repr`
printed the API key (now `field(repr=False)` — a repr lands in tracebacks and logs),
and `complete()` called `httpx.post` with a `transport` kwarg that the function does
not accept.

**Still degraded in this deployment**: no `OLLAMA_API_KEY`/`DEEPSEEK_API_KEY` is
set, so `default_provider()` returns `UnconfiguredProvider` and every turn takes the
offline-message path. That is the honest state, and it is why the outage tests use
injected providers.

Full suite: **289 passed**, 1 skipped.

### 8.13 WP-16: widget tokens, domain verification, uploads (2026-11-16)

`widget/tokens.py`, `widget/uploads.py`, `widget/router.py`, migration `0006`.

**The §17.4 routes now exist**, mounted as `/api/v1/widget/...` and mounted on the
same `service.turn_events` as the owner route — one answer path, so the two cannot
drift. This closes the gap §8.4.2 recorded when the SSE endpoint was owner-only
because a public one would have been "a public oracle over a tenant's knowledge
base".

| §12.3 / §17.4 requirement | Implemented | Why this way |
|---|---|---|
| per-session signed tokens | HMAC-SHA256 over a compact JSON payload, stdlib only | A JWT library would be a dependency for a header we do not use, plus algorithm-confusion footguns to disable. Fixed algorithm, no `alg` field to rewrite. |
| tenant from the token, never client input | `tenant_id` is inside the signed payload; no route accepts one | A `tenant_id` in a request body is a cross-tenant read waiting to happen and would defeat RLS. |
| `Origin` ↔ `bot_domains` | checked at mint **and** at use | A token is a bearer credential; binding it to an origin only helps if the binding is verified where it is spent. |
| verified domains only (§16.1, §27) | `WHERE verified IS TRUE`, re-read on every use | an unverified row is the owner's *claim*, not a proven control; without the filter anyone could register a customer's domain and serve their widget there. Caught in review of my own WP-16 code after the first commit, not by the first test pass. |
| allowed-domain matching | exact after normalisation, no wildcard | `*.example.com` would let any user-content or forgotten-staging subdomain impersonate the tenant. |
| stateless vs stored tokens | stateless | a row per pageview turns an unauthenticated endpoint into a write amplifier. Cost: no per-token revocation; the 30-minute TTL is the mitigation. |
| uploads ≤ 5 MB, type-sniffed | magic bytes, never the filename or `Content-Type` | both are attacker-controlled; `invoice.pdf` can be an HTML polyglot. |
| nothing parsed on upload | bytes stored, no thumbnailer/PDF extraction inline | parsing a stranger's file with the API's credentials is an RCE surface; that belongs in the M6 fuzzing work. |

**The cross-tenant lookup.** Resolving a public bot id has no tenant context yet, and
under §16.3 that returns zero rows. Rather than connect as the table owner (owner
credentials in the API process) or add a permissive `bots` SELECT policy (which would
make every bot readable by any code path that forgot to set a tenant), migration
`0006` adds a `SECURITY DEFINER` function with one fixed statement and a pinned
`search_path`. The pin matters: an unpinned `search_path` lets a caller who can
create objects shadow `bots` and run arbitrary SQL as the definer.

**Consent (R41).** `POST /widget/session` creates nothing — no conversation, no row —
so a pageview that never opens the chat leaves no trace. A test asserts the
conversation count is unchanged across a session mint.

**A test-isolation bug worth recording.** The widget router bound `SessionLocal` at
import; the harness reloads `app.db.session` to repoint the engine at the test
database, so the from-import kept a stale engine and its old credentials. The tests
passed alone and failed in the suite. Now the module is accessed lazily.

Mutation-verified (each mutation kills at least one test): wildcard subdomain
matching, skipping the signature comparison, skipping the expiry check, dropping the
token-to-conversation bot binding, removing the upload size cap, sniffing every
payload as a PDF, and ignoring the `verified` flag.

**Still open in WP-16**: the widget-side rate limits are the WP-13 limiter (per
visitor + per tenant) and are exercised through this route; SEC-02's per-IP dimension
is not built, and is recorded as such rather than claimed.

**A gap I shipped and then caught.** The first version of §8.13 queried
`bot_domains` without the `verified` filter, so an owner could register a domain they
did not control and the widget would serve there. §16.1 says "widget only serves on
verified domains" and the §27 checklist says "widget refuses unverified origins" —
both unambiguous. The tests all passed, because they seeded verified domains. Found by
re-reading the spec row while checking what WP-14 could build against. Fixed, with two
tests, and the mutation that removes the filter fails them.

Full suite: **330 passed**, 1 skipped.

### 8.14 SEC-02: rate limiting, and which address the limit is keyed on (2026-11-16)

§19.1 / R36 ask for per-IP and per-visitor-token limits on widget endpoints and
per-user limits on admin. Two parts of that are subtle enough to write down.

**The visitor key is gameable on its own.** ``visitor_session_id`` is chosen by the
widget, so a script that sends a fresh one per request never repeats a visitor key.
That is what the IP scope is for: an attacker can shed a header, not an address. The
counters are per-scope and all of them are incremented even when an earlier scope
trips, because a short-circuit would leave an abusive visitor invisible in the
tenant's count — and the tenant's first signal would then be a bill rather than an
alarm.

**Which IP, though.** Behind Caddy, ``request.client.host`` is the proxy, so keying on
it produces one global bucket: the first visitor exhausts the limit for everybody.
``X-Forwarded-For`` fixes that and is client-controlled, so the rule is:

* trust the header only when ``TRUST_PROXY_HEADERS`` is set (compose sets it, because
  Caddy is the only ingress there; the code default is off);
* read the **last** hop, not the first. Caddy appends the peer it observed, so the
  final entry is real and the earlier ones are the sender's fiction. ``[0]`` is the
  common bug — it reads as "the original client" and is precisely the attacker's
  claim.

Both halves are tested, including the mutation that trusts the header unconditionally,
because either half alone is worse than no limit at all: it looks like a control.

**Per-user goes in `current_user`, not in the routers.** It must skip anonymous
callers (keying them on "no identity" would give the whole logged-out internet one
shared bucket) and it must cover routes that do not exist yet. The single dependency
every authenticated request passes through is the only place that satisfies both. A
test asserts five consecutive anonymous requests all get 401 and none get 429.

Limits: visitor 10/min, IP 60/min, tenant 120/min, admin user 60/min. The IP limit is
deliberately looser than the visitor limit — one address is often an entire office or
a carrier NAT, and the IP scope exists to stop identity rotation, not to police shared
addresses.

Mutation-verified: trusting the header by default, taking the first hop, and removing
the per-user check from `current_user` each fail at least one test.

**The upload route had no limiter at all.** Every other widget route did; the one that
accepts 5 MB per request from an unauthenticated visitor did not. It shipped that way
in WP-16 with 39 passing widget tests, because the tests exercised each route in
isolation and none of them asked "how many times in a row can this be called?".
Twelve live uploads through Caddy returned twelve 201s. Found by running the deployed
stack, which is why the loop now redeploys before believing a WP.

Two lessons worth keeping: a per-route limiter is a policy that the next route
forgets, so all widget routes now share `_rate_limit()`; and the check runs before the
body is read, because a refusal that arrives after buffering 5 MB is still a memory
amplifier.

Full suite: **348 passed**, 1 skipped.

### 8.15 WP-13 (SEC-03/SEC-04): the BYOK vault, and two silent-write bugs (2026-11-16)

§16.2 asks for secrets in an encrypted store with only a `key_reference` in the
database. The shape is one age file per `(tenant, provider)`, referenced by a
tenant-relative path.

**Relative, not absolute.** An absolute path in a row bakes the deployment layout
into the data and leaks filesystem structure into every table dump — the thing the
dump is already dangerous enough without. Resolution re-checks containment instead
of trusting the caller, because the function that turns a database string into a
filesystem write is exactly where a traversal bug becomes a write primitive.

**Ciphertext is on a host bind mount, not a named volume.** `compose down -v` removes
named volumes; losing every tenant's provider key to a routine teardown is not an
acceptable default. The age identity is a separate file, also outside the repo, so a
backup of the ciphertext is not a backup of the key.

**Write-only is structural.** `CredentialSummary` has no field that could carry a
key, and there is no endpoint that returns one, so "never returned by any API" is a
property of the router's shape rather than of each handler remembering. The masked
`last4` is read from the audit trail rather than recomputed, because recomputing it
means decrypting the key to display a masked version of it.

Ordering is deliberate: encrypt to the vault *before* writing the row, so a vault
failure is a 503 with no dangling reference. The reverse leaves a credential that
the dashboard reports as configured and the first turn reports as broken.

**Two real bugs, both found by tests failing rather than by reading code:**

1. `client_ip()` returned the raw `X-Forwarded-For` value, and callers write it to
   `inet` columns. A request with `X-Forwarded-For: not-an-ip` raised
   `InvalidTextRepresentation` inside the insert — a 500 that **any anonymous caller
   could trigger on login**, turning an audit column into a denial-of-service lever.
   Validation now happens once, where the header is parsed. Auth and admin each had
   their own `_client_ip` copy that validated; the shared one did not, and the two
   local copies had already drifted (they ignored the proxy header entirely, so
   behind Caddy every login was audited against the proxy's address). Both now call
   the shared function.
2. `tenant_session()` closes without committing. A route that writes and returns
   without an explicit `commit()` gets a 201 and **no row**. Every other mutating
   route commits; the new one did not. Three tests failed with "credential not
   found" immediately after a 201, which is the failure mode stated plainly.

Mutation-verified: removing `vault.put` fails 4 tests; removing the commit fails 3;
the vault suite independently covers wrong-identity decryption, traversal, and the
0600 mode.

Verified live through Caddy, because the unit tests cannot see the deployment: set →
201 and a 0600 age file appears; `grep` finds no plaintext in it; it decrypts with the
identity; repost rotates in place; DELETE returns 204 and removes both row and
ciphertext; `X-Forwarded-For: not-an-ip` on login is 401, not 500.

Full suite: **370 passed**, 1 skipped.

### 8.16 **M2 GATE** (2026-11-16) — PASSED

Criteria quoted from §7 M2 "Done when". Gate evidence is reproduced numbers and
SHAs, per G4. Registry: **M2 25 rows, 25 `done`, 0 backlog** (`docs/FEATURES.md`).
Full suite at gate: **370 passed, 1 skipped**. Commits: `722c881` (SEC-02 + upload
limiter), `21099e0` (WP-13 SEC-03/SEC-04); built on `8e81d33`, `62830ba`, `3b61689`.

Run on the live compose stack (Caddy → api → arq worker → Postgres/pgvector, real
BGE-M3 in the `embed` service) *and* in the suite, with synthetic fixtures only.

| # | Done when | Evidence |
|---|---|---|
| 1 | Streaming grounded answer with citations over hybrid retrieval | `test_grounded_answer_streams_and_persists_citations`, `test_sse_stream_framing_and_event_order`, `test_a_citation_without_a_url_keeps_the_field_as_null`. Live widget turn through Caddy: SSE `event: token` then `event: final` with `citations[0] = {document: "What is the return window?", section: "What is the return window?", uri: null, score: 0.6246}` and `confidence 0.6246 ≥ threshold 0.55`. **Caveat, stated plainly:** the live deployment has no provider key, so that turn carried `degraded: true` — retrieval, citation and streaming are proven live; the generated prose is proven by `test_turn_answers_from_a_real_provider_over_http`, which drives the loop against a real local SSE server. |
| 2 | Refusal below the confidence floor | `test_refusal_never_calls_the_model` (the model is not invoked at all). Live: "Do you sell live llamas in Antarctica?" → `declined: true, reason: below_confidence_floor, confidence 0.3022 < 0.55, citations: []`. |
| 3 | Eval harness green on the golden set **and demonstrably blocks a seeded retrieval regression** | `tests/test_eval_gate.py` 6 passed. Golden set 60 cases (45 answerable, 15 not). CI config (HashingEmbedder, threshold 0.16): recall@5 0.7778, MRR 0.6489, correct-refusal 0.600, answer-rate 0.8889 — matching `tests/eval/baseline.json` within 0.02. Real BGE-M3 at 0.55: **1.000 on all four** (§8.8). Regression proof, run now: replacing `reciprocal_rank_fusion(dense, sparse)` with `(dense, [])` drops recall@5 0.7778 → 0.7333 and the gate fails on **both** `test_golden_set_passes_the_gate` and `test_eval_records_a_real_baseline`; restored. **Honest limit:** the tolerance (0.02 against baseline) is what caught it — 0.7333 still clears the absolute floor of 0.70. The floor is a backstop, the baseline is the detector. |
| 4 | Provider fallback survives a simulated outage | `test_turn_survives_a_provider_outage`, `test_outage_breaker_stops_calling_the_dead_provider`, plus the chain built from credential presence (§8.12). 46 tests in `tests/test_provider_resilience.py`. |

WP-13 (SEC-03/SEC-04) landed last and is §8.15; SEC-02 is §8.14.

**Known gaps at end of M2**, recorded rather than hidden:

- **No provider key in this deployment.** `LLM_PROVIDER` is empty and neither
  `OLLAMA_API_KEY` nor `DEEPSEEK_API_KEY` is set, so `default_provider()` returns
  `UnconfiguredProvider` and every live turn degrades to the handover message. That
  is the honest state with no key, not a defect — but it means "grounded answer"
  criterion 1 is demonstrated live only up to citations and streaming.
- **Reranking is off.** `reranked: false` is the production state (HALT-04 is
  advisory and unruled); RRF quality is what the harness measures.
- **`cost_micros` stays 0** until prices are configured (§8.12).
- **BYOK keys are stored but not yet consumed by `default_provider()`.** The vault
  and the endpoint are complete and verified; wiring a tenant's stored key into its
  own turn is WP-20/21 territory (settings surface). Until then a stored key changes
  nothing about that tenant's answers.

### 8.5 M1 gate evidence (2026-11-16, live compose stack)

Run against the real stack — Caddy → api → arq worker → Postgres/pgvector, with the
actual `BAAI/bge-m3` weights in the `embed` service. No test doubles, no fakes;
all fixtures synthetic per the confidentiality charter.

| Step | Result |
|---|---|
| Synthetic 4-line PDF upload | `201`, version created, worker ingested it, status `ready` in ~2.6 s |
| Crawl of a local 2-page fixture site | robots.txt honoured, 2 documents, both `ready` |
| FAQ create | `201`, embedded synchronously in the request |
| Preview "How long do refunds take?" | top hit 0.757 = the refund chunk (PDF) |
| Preview "What warranty do electronics have?" | top hit 0.783 = the crawled warranty page |
| Preview "student discount" | FAQ chunk retrieved |
| publish v1 → v2 → rollback v1 | pointer moved and moved back; response confirms each id |
| publish to an unknown bot | `404 bot_not_found` (was a silent success before the fix) |

Throughput on this host was 2–5 s per document version including the network hop to
the embed service, consistent with the 0.24–0.33 chunks/s steady state in 8.2.

**Deployment bugs this gate found** (all committed with regression tests where a test
can express them):

1. Caddy used `handle_path /api/*`, stripping the prefix; every proxied request
   reached the API as `/v1/...` and 404'd. Tests call the ASGI app directly, so the
   suite could never have caught it. → `handle`.
2. The session cookie defaulted to `Secure` while the dev Caddy site is plain HTTP,
   so browsers discarded it and login silently failed to persist. → explicit
   `SESSION_COOKIE_SECURE` (default `false`; must be `true` behind TLS).
3. `EMBEDDING_QUERY_BACKEND` defaulted to `local`, so the API tried to import
   `sentence_transformers` — absent by design under the 512 MiB cap — and 500'd on
   the first FAQ. → API and worker both set `remote`.
4. Nothing created a bot, so publish could never be called. → registration creates
   the tenant's single MVP bot in the same transaction.

### 8.6 Known gaps at end of M1

Recorded rather than hidden:

- **Bot id discovery.** Publish needs a `bot_id`, but no read endpoint exposes it;
  the gate had to query the database. M5's dashboard work owns the settings surface
  that will show it, so no endpoint was invented here.
- **RAG-10 is partial.** `POST /knowledge/preview` returns draft chunks with scores;
  the draft-vs-live side-by-side and citations need the M2 retrieval stack.
- **`GET /health` is served at `/health`, not under `/api/v1`.** Fine today (Caddy
  does not route it), but it means container health and the public surface differ.
- **Crawl scheduling is stored, not run.** `POST /knowledge/crawls` enqueues once;
  the `schedule` value is validated and persisted but no cron is wired up.
- **Preview reads the draft only.** The widget path that reads the published pointer
  arrives with the chat loop in M2.

### 8.17 WP-14: the widget, and the two serving bugs a build cannot catch (2026-11-16)

**Dependency grant (operator ruling, 2026-11-16 18:40 UTC):** Preact 10.27.2 and
esbuild 0.25.10 are sanctioned runtime deps for the widget, matching the §9.3/§12
intent (vanilla-TS loader + iframe Preact app). Recorded here as one line per the
ruling; `STACK.md` line 18's age/sops lock is unaffected.

**Size, measured on the built artefact and on the wire:**

| | raw | gzipped | budget |
|---|---|---|---|
| `loader.js` | 7 221 B | **2 981 B** (3 026 B over HTTP with zstd/gzip negotiated) | 10 240 B |
| `app.js` (iframe) | 24 611 B | 9 699 B | none (§12.2 constrains the loader) |

The budget is enforced in `build.mjs` — the build *fails* past 10 KB — and re-asserted
in `tests/test_widget_assets.py`, so it cannot drift silently. Measuring only the raw
file would have been dishonest: the constraint is about bytes on the wire.

**Two serving bugs, both found live and neither catchable by a unit test:**

1. **`/widget/*` reached the dashboard, not the API.** Caddy proxied only `/api/*`;
   every widget URL fell through the default handler and returned Next's 404 page.
   Fixed with explicit `handle /widget/*` and `/demo/*` blocks. A test calling the
   ASGI app directly cannot see a reverse-proxy routing gap — this is the same
   failure class as M1's `handle_path` bug, in the opposite direction.
2. **`Mount("/widget")` swallowed the frame document.** Starlette matches routes in
   declaration order, so the static mount (declared first) answered `/widget/app`
   as a missing file named `app`. Fixed by declaring the route before the mount, and
   `test_the_frame_document_is_served_as_html` now pins the ordering.

**Where the API calls live: the host page, not the iframe.** The frame is served from
the platform origin, so a `fetch` inside it would send the *platform's* `Origin` and
fail §12.3's domain check on every request. The loader makes all calls and relays
events over `postMessage`, which keeps origin verification meaningful and keeps the
token out of the frame.

**Untrusted model output is rendered as a typed tree.** Markdown is parsed into a
`Block`/`Inline` structure and rendered as Preact vnodes; no `innerHTML`, so there is
no sanitiser to misconfigure. Images and raw HTML are not in the grammar — an image
URL in an answer is an exfiltration channel — and links are restricted to
`http:`/`https:`/`mailto:`. Tests assert `javascript:` and `data:` produce no link.

**Live verification (compose stack, bot `Pub`, verified domain `http://localhost`):**

| Step | Result |
|---|---|
| `GET /widget/loader.js` | `200`, `3026 B` gzipped on the wire |
| `GET /widget/app` | `200 text/html`, loads only `/widget/app.js` |
| Session from verified origin | `200`, token + bot config |
| Session from `https://evil.example` | `403 origin_not_allowed` |
| Full turn via widget path | `token` then `final`; citation `score 0.7237`, `degraded: true` (no provider key — operator ruling: keep and state it) |
| Visitor upload (WIDG-14 re-verified) | `201` valid PDF; `403` from an unverified origin |

**Known gaps at WP-14 close:**
- The widget has no UI to *choose* a file (WIDG-14 is the endpoint, verified via
  `curl`); the composer is text-only, matching the MVP's text-only scope.
- No browser was available in this environment, so WIDG-16's matrix and the DOM-level
  half of SEC-08 are not yet evidenced; see the M3 gate report for what is proven
  instead and by what means.

### 8.18 WP-15: the widget could not have worked on a customer's site (2026-11-16)

**The finding that matters: every widget API call was CORS-blocked on any real host
page.** The widget is embedded on the *tenant's* domain while the API is served from
ours, so all of its requests are cross-origin. The API sent no
`Access-Control-Allow-Origin` header and answered `OPTIONS` with `405`, so a browser
refused to send the request at all.

It survived WP-14 and the M3 gate because **every check up to now was `curl` or an
ASGI test client, and neither enforces CORS**. The demo page is served from the API's
own origin, so the demo worked too. The widget would have failed on the first
customer's website and nowhere in our evidence.

Found by installing Playwright and running the widget in a real browser on a
*different* origin — the one configuration the existing evidence could not reach.
Fixed in `app/main.py`: a `WidgetCORS` middleware scoped to `/api/v1/widget*` and
`/widget*`. `allow_origins` defaults to `*`, which is correct here rather than lazy:
the widget authenticates with a **bearer token**, never a cookie, so there is no
ambient credential for a wildcard to expose. Whether an origin may mint a token is
decided separately and per-tenant by `origin_matches()` against `bot_domains`
(WIDG-05), which is the real control. `allow_credentials` stays off — the spec forbids
combining it with `*`, and that combination is the only one that would be a risk. The
dashboard API deliberately gets no CORS at all.

`tests/test_widget_cors.py` (8 tests) pins it, including that the dashboard is *not*
enabled and that credentials are absent. Disabling the middleware fails 6 of the 8, so
the tests are load-bearing rather than decorative.

**Second finding: the CSAT vote never worked.** The app posted its local React key
(`b-<timestamp>`) as `message_id`; the endpoint requires a UUID, so every vote was a
`400`. Fixed by carrying the server id from the `final` event as `serverId` and
posting that; the local key never leaves the client. Verified live: real id → `201`,
local-format id → `400 invalid_uuid`.

**Third finding: identity is lazier than the requirement.** §19.3 asks that nothing be
stored before the chat is *opened*; the loader stores nothing until the first message
is *sent*, because `identity.id()` is only reached inside `ensureConversation()`. The
browser test asserts the actual behaviour rather than the weaker requirement, so a
future refactor that starts writing on open would fail it.

**Appearance is validated where it is consumed.** `appearance` is tenant-controlled
JSONB that renders inside a third-party page (`avatarUrl` → `img.src`, `primaryColor`
→ CSS, `headerText` → DOM). `app/widget/appearance.py` normalises it server-side
before it leaves the API: known keys only, colour restricted to `#hex`/`rgb()`/`hsl()`
rather than free-form CSS, avatar `https:` only, text length-capped and stripped of
control characters including the bidi override `U+202E`. A hostile value yields the
default, not a broken host page. The dashboard form that *writes* these values is M5's
ADMN-03, so the tests write the column directly — recorded as a gap, not hidden.

**Mobile and accessibility (WIDG-10/11).** Full-viewport sheet at ≤480 px using
`100dvh` (browser chrome) and safe-area insets (home indicator); the media query lives
in a stylesheet because an inline `bottom`/`height` would win over it. All touch
targets ≥44 px, asserted by a test that fails on any smaller `min-width`/`min-height`.
`role="log"` + `aria-live="polite"` + `aria-relevant="additions text"`, a focus trap
wrapping both ways, Escape to close, accessible names on icon-only controls, reduced
motion honoured.

**Browser matrix, now actually run.** Playwright + Chromium installed (dev-only
dependency; the import is lazy so `npm test` still works without it). Nine cases, all
passing in Chromium against a freshly seeded stack: storage empty before click and
after open; one click creates exactly one frame; a real grounded answer with a
citation chip renders; full-viewport on a 390×844 viewport; every control ≥44 px;
the transcript is a live region; Escape closes; no CSP violation; an unverified
origin gets `403`. The live answer, verbatim: *"Unopened items can be returned within
30 days. If you need an actual return processed, I can't take that action myself — I
can connect you with the team."* with citation chip *"What is the return window?"* —
extractive, because no provider key is configured (§8.16).

**Limits, stated rather than papered over:**
- Chromium only. Firefox and WebKit are a separate `playwright install`; the CI job
  runs Chromium and the registry says so.
- "Mobile" is device emulation, not a physical handset and not iOS Safari.
- No screen-reader pass was performed. The ARIA contract is asserted; how NVDA or
  VoiceOver actually reads a streaming turn is not.
- `SEC-08`'s browser audit is now genuinely browser-based, and the DOM-stub version
  (`test/storage_audit.test.mjs`) is kept as the cheap CI guard. Its mutation check —
  injecting a load-time `localStorage.setItem` + `fetch` fails 4 of 7 cases — is what
  makes it more than a grep.

**Size after WP-15:** `loader.js` 7 644 B raw / 3 117 B gzipped, still inside the
10 KB build budget; `app.js` 25 455 B / 10 092 B gzipped.

**CI gained two things it needed:** the widget build (`npm ci && node build.mjs`)
before pytest, because the Python suite asserts the shipped bundle; and a separate
`widget-browser` job for the matrix, kept apart so a flaky browser run cannot mask a
unit-test result in the summary (§21.3). `scripts/seed_widget_bot.py` seeds a synthetic
tenant, one FAQ, a published version and a verified origin — synthetic only, per the
charter.

### 8.19 **M3 GATE** (2026-11-16) — PASSED, with one criterion narrowed

Criteria quoted from §7 M3: *"embed snippet runs on a test site; browser storage audit
shows nothing stored before click; mobile + a11y matrix passes; uploads constrained and
safe."* Registry: **M3 18 rows — 17 `done`, 1 `partial` (WIDG-16), 0 backlog**. Full
Python suite at gate: **406 passed, 1 skipped**. Widget node suite: **38 passed**.
Browser matrix: **9 passed in Chromium**. Commits `25de6ba` (WP-15) and its follow-up.

| # | Done when | Evidence |
|---|---|---|
| 1 | Embed snippet runs on a test site | The documented snippet verbatim (`<script src="/widget/loader.js" data-bot="…" defer>`) served on a host page, loaded in **real Chromium**, launcher rendered, click opens the frame, a turn completes. Also proven across origins: a host page on a throwaway port talking to the API — which is what found the CORS bug (§8.18). Loader `7 644 B` raw / `3 117 B` gzipped, inside the 10 KB budget enforced at build time and re-asserted in Python. |
| 2 | Browser storage audit: nothing stored before click | Two independent layers. **Real browser** (`browser.test.mjs`): `localStorage`, `sessionStorage` and `cookies` all empty before the click **and** after the panel opens; identity appears only on first send. **Shipped-bundle audit** (`storage_audit.test.mjs`): runs `dist/loader.js` in a recording DOM stub — zero writes, zero fetches, no iframe; mutation-tested by injecting a load-time write, which fails 4 of 7 cases. **Python** (`test_widget_assets.py`): the size budget and required wiring are re-asserted against the built bundle. |
| 3 | Mobile + a11y matrix passes | **Narrowed, stated plainly.** Chromium only: 390×844 full-viewport sheet (flush left, ~full width/height), every visible control ≥44 px in both dimensions, `role="log"` + `aria-live="polite"` + `aria-relevant="additions text"`, focus trap wrapping both ways, Escape closes and returns focus, accessible names on icon-only controls, `prefers-reduced-motion` honoured. **Not** proven: Firefox, WebKit, physical devices, iOS Safari's viewport behaviour, and any screen-reader pass. WIDG-16 is therefore `partial`, not `done`. |
| 4 | Uploads constrained and safe | WIDG-14 re-verified live this gate: valid PNG/PDF → `201`; unverified origin → `403 origin_not_allowed`. Magic-byte sniffing rather than trusting the declared type or extension; 5 MB ceiling; tenant-scoped storage keys. `test_widget_tokens.py` covers the rejection paths. The widget has **no upload UI** (text-only MVP scope) — the endpoint is exercised over HTTP, and that is the honest boundary of this claim. |

**Bugs this gate found that no previous evidence could have found** — all in §8.18,
all now regression-tested:

1. **CORS was entirely absent.** Every widget call would have been blocked by the
   browser on any real customer site. `curl` and the ASGI test client do not enforce
   CORS, and the demo page is same-origin, so the bug was invisible to all prior
   evidence. This was one Playwright install away from being missed entirely.
2. **CSAT votes always failed.** The app posted a React key (`b-<ts>`) where a UUID was
   required — `400` on every vote. Live-confirmed both ways.
3. **`normalise("not a dict")` returned `[]`**, then the caller called `.get` on it.
   JSONB can legitimately hold an array or a scalar, so a tenant could have 500'd their
   own session endpoint.

**Known gaps at M3 close**, recorded rather than hidden:

- **Firefox and WebKit unverified**, and no screen-reader pass (WIDG-16 `partial`).
  The CI `widget-browser` job runs Chromium; extending it is a one-line
  `playwright install` per engine.
- **No bot settings UI.** `appearance`, domains, welcome text and suggestion settings
  are consumed by the widget but have no dashboard surface — ADMN-03 is M5. Domains are
  inserted with SQL in tests and in `scripts/seed_widget_bot.py`.
- **Bot-id discovery still needs a DB query** (carried from M1): there is no endpoint
  listing a tenant's bots, so the install snippet's id cannot be obtained from the
  product yet.
- **No provider key in this deployment**, so live answers are extractive with
  `degraded: true`. Retrieval, citation, streaming and rendering are all proven; the
  generated prose is not, and is covered separately by the provider HTTP test.
- **`GET /health` is at the root**, not under `/api/v1` — a deliberate deviation from
  the API surface convention, noted so it is not mistaken for an oversight.

### 8.20 **M4 GATE** (2026-11-17) — **PASSED by operator ruling, 2026-11-17 05:10 UTC**

Criteria quoted from §7 M4: *"Done when: a staged handover email with transcript+summary
arrives from a real conversation trigger; signed webhook fires; offline mode follows
business-hours config."* Registry: **M4 7 rows, 7 `done`, 0 backlog** (`docs/FEATURES.md`).
Full Python suite at gate: **465 passed, 2 skipped** (`pytest --ignore=tests/test_eval_gate.py`,
as CI runs it), plus the eval gate **6 passed** as its own CI step. Widget node suite:
**39 passed, 9 skipped** (Chromium-only browser tests skip without a browser, per
§8.19). Evidence is reproduced by `scripts/m4_gate.py`, which runs the whole path
against the live stack in one command and asserts each clause; the run below is that
script's output, not a hand-assembled transcript.

**Ruling.** All three clauses have a reproducible live observation, and the script exits
non-zero if any of them stops holding. Whether that cleared M4 was the operator's call,
not the agent's, and the operator ruled **PASSED** on 2026-11-17 05:10 UTC, on the
evidence in this section, with clause 1's "real conversation trigger" narrowed to a real
widget session as recorded above. M4 is closed; M5 entry was authorised in the same
pulse.

Run on the live compose stack (Caddy → api → Postgres/Redis, real SMTP, real provider),
synthetic fixtures only, per the confidentiality charter.

| # | Done when | Evidence |
|---|---|---|
| 1 | A staged handover email with transcript+summary arrives from a real conversation trigger | `scripts/m4_gate.py` steps 1–8. A real widget session → real turn (answered by `ollama_cloud/deepseek-v4.1-flash:1017-cloud`, `degraded: false`) → real `POST /handover` → `msg-015.eml` in the sink with `Subject: Handover: M4 Gate 769c79 needs you`, `Reply-To: visitor@example.test`, a **generated** summary ("The visitor asked how long standard shipping takes and was told 3–5 business days…"), and the transcript including the answer the bot actually gave. The gate asserts the summary is *not* the no-model fallback — a fallback would prove the plumbing but not the clause. DB corroboration: 5 most recent `handovers` rows all `status=notified, channel=email, notified_at NOT NULL` (the 6th, `7df14f88…`, is the pre-fix `recorded` row kept as the negative case). |
| 2 | Signed webhook fires | `scripts/m4_gate.py` step 9. A receiver container on the compose network, registered as a real webhook on port 8080 (inside the product's `ALLOWED_PORTS`; the gate deliberately does **not** widen the allowlist). Both events arrived — `conversation.created` and `handover.created` — and both HMACs verify `v1=…` over `timestamp.body` **recomputed in the gate from the secret returned once at creation**, not from a copy handed to the listener, so a mismatch between the two would fail. Secret is absent from `GET /api/v1/webhooks`, asserted in step 3. |
| 3 | Offline mode follows business-hours config | `scripts/m4_gate.py` step 10, and step 4 as its control. No hours configured → `online: true` (correct per R43: unconfigured means always online). Hours set to a one-minute Monday window while "now" was Tuesday 01:43 UTC → `online: false, next_open: 'Mon 23 Nov 00:00 UTC'`. The widget shows the contact form before the visitor types when offline (`widget/src/app/app.tsx:432`, dismissal ~225–227). Settings restored afterwards; the gate removes the key it wrote. |

**What this gate cost, and the bugs it found.** The path was already `done` in the
registry, but the first *reproducible* run failed four times, each a real finding:

1. **A handover with no owner address is silent, by design.** `handovers` stayed
   `recorded` with a NULL `outcome` because `channel is None or not owner_email`. Not a
   defect — a tenant with no `settings.handover.email` has nowhere to send — but it means
   the clause cannot be evidenced without configuring an owner address, which no endpoint
   yet exposes.
2. **The declined branch of the SSE `final` event omits `degraded` entirely**
   (`conversation/service.py`, declined return). My first assertion keyed on it and
   crashed on a legitimate out-of-scope refusal. The widget protocol does not read
   `degraded`, so nothing is broken for a client — but the event shape is not uniform,
   and a consumer that assumes the field exists will trip on exactly the refusal path.
   Recorded here rather than changed: it is outside WP-19 and the protocol does not
   promise it.
3. **Webhook ports are allowlisted to 80/443/8080/8443** (`ALLOWED_PORTS`), which is
   correct SSRF hygiene. My receiver used 8099 and was refused with `port 8099 is not
   allowed`. The gate moved to 8080 rather than the allowlist being widened.
4. **The signing secret cannot be self-verified by the receiver.** A receiver that
   checks the signature with a secret it was handed at startup passes even if the two
   sides disagree. Verification moved into the gate, using the creation-time secret.

**The handover state machine, read off a live database rather than inferred** (this
matters because the table shows states that look like failures and are not):

| Status | How it is reached | What it means |
|---|---|---|
| `pending` | `open_handover()` inserts | A trigger fired and the conversation is locked, but nothing has been delivered because the visitor has not yet given an address. The widget is showing the contact form (`requires_contact: true`). |
| `recorded` | `compile_and_deliver()` finds no `channel` or no `settings.handover.email` | Compiled, deliberately not sent; the owner sees it in the dashboard. |
| `notified` / `failed` | `channel.send()` returned | Attempted, outcome on the row. |

Live counts after the gate: **25 `notified`, 1 `recorded`, 1 `pending`**. The single
`pending` row is tenant `Widget Matrix fc31e7`, whose conversation is
*"Please connect me with a human agent"* → the keyword trigger, contact form never
submitted. That is the designed path. **The observability gap:** a delivery that throws
also leaves `pending` (`_notify`'s worker swallows the exception and logs it), so
"waiting for the visitor" and "delivery crashed" are indistinguishable in SQL. Worth
knowing before anyone reads `pending` as a bug — or as health.

**Caveats the operator should weigh before calling M4 production-ready:**

- **The SMTP sink is an ad-hoc container, not a compose service**, and `SMTP_*` points at
  it. A real provider (and SPF/DKIM) is required before "handover email" means *delivered
  to a customer's owner* rather than *handed to a local sink*.
- **The owner address and the verified origin were written by SQL**, because no endpoint
  exists for either (ADMN-03 is M5). So the gate proves the *behaviour*; it does not prove
  an owner can enable it unaided.
- **`WEBHOOK_ALLOW_PRIVATE_HOSTS=true` was set for the duration of the run and reverted.**
  It is a test affordance that disables the SSRF guard's private-address refusal; it must
  not be set in a real deployment. The gate says so in its output. The receiver container
  and temp files were removed; `printenv` in the API container confirms the flag is gone.
- **No retry/backoff on webhook delivery.** `deliver()` makes a single attempt and
  records the outcome; `emit()` loops over endpoints but never over attempts. Loss
  during a transient receiver outage is therefore possible — WP-18 scope as written, but
  a real operational gap.
- **The handover email address is visitor-supplied and unverified.** It becomes the
  `Reply-To`, not the recipient, and the conversation must already belong to the token's
  tenant *and* bot with the origin re-checked — so a stranger cannot mail a third party
  through the product. What is unproven is that the address is *reachable*: a typo means
  the owner replies into the void, and nothing tells them.

**Gaps closed since M3:** a provider key is now configured (`OLLAMA_API_KEY` present), so
live turns are genuinely generated with `degraded: false` — the M2/M3 caveat about
extractive-only live answers no longer applies. Confirmed this run, not assumed.

### 8.21 **WP-19** (2026-11-17) — conversation archive, export and analytics (backend + screens)

First WP of M5. Registry rows closed: **ADMN-06, ADMN-07, ADMN-08, ADMN-09** `done`;
**ADMN-01, ADMN-02** `partial` (shell only — the sections whose screens are ADMN-03/04/10
are deliberately unlinked rather than linked to a 404).

**Shipped.** `GET /api/v1/conversations` (status/from/to filters, limit/offset pagination),
`GET /api/v1/conversations/{id}` (summary + transcript + turns), `GET
/api/v1/conversations/{id}/export?format=txt|json`, `GET /api/v1/analytics/summary`
(`?days=`, bounded 1–365). Screens: `/conversations`, `/conversations/[id]`, `/analytics`,
plus a nav shell. No new dependencies on either side — the dashboard uses `fetch` and the
framework router, so `package-lock.json` is untouched.

**Four decisions the shape of the data forced, each a refusal to invent:**

1. **No "resolution rate".** Neither `resolution` nor `unanswered` exists anywhere in the
   schema, and the product has no ticket-close signal. The response carries
   `answered_turn_rate` (assistant turns that recorded a confidence score ÷ all assistant
   turns) and `handover_rate`, each with its definition inline, and states that neither is
   a true resolution rate. A field called `resolution_rate` would be a number named after
   something it only approximates, and the dashboard would render it without the caveat.
2. **`top_unanswered_questions` is provisional.** It ranks repeated visitor questions,
   which is not the same as *unanswered* ones — a frequently asked question may be well
   answered. Proper gap detection is OPS-05 and does not exist, so the response says so.
3. **`pending` handovers are flagged `ambiguous`.** One DB status covers "the visitor
   never submitted the contact form" and "delivery threw before recording an outcome"
   (the M4 finding). The API returns `ambiguous: true` and the badge renders
   "pending (ambiguous)" with the reason, rather than asserting a visitor is waiting.
4. **Export completeness is asserted against direct SQL, not against the API's own view.**
   ADMN-07 cites R32, so the export is the data-subject mechanism: a message that renders
   in the view but is missing from the export is a compliance defect. Tests query
   `messages` independently and compare counts.

**A verification I got wrong, and how.** I probed the new endpoints with `curl` against
`localhost:8000` and read three `200`s as success. `8000` is this sandbox's agent-canvas
ingress, not the EVA stack — Caddy publishes on **port 80**. The real stack had been
returning `{"detail":"Not Found"}` for every path the whole time, including
`/auth/register`; the "passing" probes were the canvas frontend's own responses. Caught by
noticing that a route I had not touched also 404'd, then comparing the same
`openapi.json` fetched through Caddy (no analytics path) against the API container
directly (path present). Re-run against `http://localhost`, everything passed. Recorded
because the failure mode — a healthy-looking probe against the wrong port — will recur in
any future live check.

**Tests.** 16 new in `tests/test_conversation_archive.py`. Assertions were confirmed by
mutation rather than assumed: dropping the tenant predicate from the single-conversation
lookup did **not** leak, because RLS refused the row anyway. That is defence in depth
working, but it meant the isolation test could not distinguish the two layers, so a
separate test pins the predicate itself. Dropping a message from the export and
over-counting answered turns each failed as intended.

**Measured, with CI's own commands:** main suite **481 passed, 2 skipped** (`pytest
--ignore=tests/test_eval_gate.py`) = the 465 recorded at M4 + 16 new; eval gate **6
passed**. Dashboard: `tsc --noEmit` clean, `next build` produces 5 routes.

**Still open in M5.** ADMN-02 home overview, ADMN-03 bot config, ADMN-04 knowledge admin,
ADMN-05 publish/preview, ADMN-10 settings, ADMN-11 onboarding, ADMN-12 embed snippet —
i.e. the two nav sections the shell omits. There is also no login screen: the screens
assume an existing `eva_session` cookie and show "Log in to view…" on 401, so an owner
arriving at `/conversations` with no session has no in-product path to authenticate.

**Follow-on within WP-19: login, and bot configuration (ADMN-03).** Two gaps surfaced
once the screens existed and were used in order rather than one at a time:

1. **There was no way to log in.** The screens rendered "log in to view this" on a 401
   and nothing in the product could produce a session, even though `/auth/login` and
   `/auth/register` had existed since M1. `/login` now does both, and the nav shows the
   signed-in email with a sign-out action. Found on the way: `request()` called `.json()`
   unconditionally, so logout's 204-with-no-body threw a parse error — the cookie was
   cleared, so the UI reported a failed sign-out that had succeeded.
2. **Bot configuration could not be written (ADMN-03).** `bots` was inserted once at
   registration and never updated: `GET /bots/{id}/config` served the widget, but an
   owner could not rename their bot, set a greeting or change its colour, and no endpoint
   exposed the bot id. Added `GET /bots`, `GET/PATCH /bots/{id}` and `/bot`. Writes run
   through the same `appearance.normalise` / `suggestions.sanitise` the widget uses — a
   separate, laxer validation would let an owner save a colour that is silently rewritten
   on delivery — and the audit row records field names, never values, because
   `instructions` is the system prompt and `welcome_message` is visitor-facing copy.

`instructions` is writable by API but has no screen: a long-form prompt editor is a
different UI problem from the short fields, and shipping a cramped textarea would invite
owners to paste a prompt into a box that hides most of it.

**Counts.** 14 new backend tests (495 passed / 2 skipped, against 481 before), 7 dashboard
client tests via Node's built-in runner (`npm test`, no new dependencies), `tsc` clean,
`next build` 8 routes. Live-verified through Caddy on port 80: register → list → PATCH →
the widget's own config endpoint serving the saved values; synthetic tenant deleted after.

**A note on the mutation results.** Two WPs running, deleting a tenant predicate did not
leak — RLS refused the row both times. That is the isolation policy doing its job, but it
means the isolation test cannot distinguish the predicate from RLS. Both WPs therefore
carry a test that pins the predicate itself, each verified to fail when it is actually
deleted. The residual risk is the opposite of the obvious one: the tests would pass if RLS
were dropped, so `pg_policies` is asserted too.

**Halt, and why (2026-11-17).** The embed snippet (ADMN-12) is shipped and verified: the
endpoint's own output pasted verbatim into a host page renders the launcher, and clicking
it opens `/widget/app`. But the chat does not work for a **new** tenant, and cannot be made
to without a ruling.

The chain is: `POST /widget/session` checks the request `Origin` against
`bot_domains.domain WHERE verified IS TRUE`; nothing writes `bot_domains`; there is no
`POST /bots/{id}/domains` route. So every tenant registered after the schema was created
has zero verified domains and the widget correctly refuses every origin. Verified live:
the frame renders "this widget has no allowed domains configured" and `/widget/session`
returns 403.

`docs/ARCHITECTURE.md` §17.4 lists `POST /api/v1/bots/{id}/domains` — *"Add + verify
allowed domain"* — and §16.1 fixes the columns (`verified bool, verification_token`). It
does **not** say **how** verification happens. Two readings, and they are not equivalent
in security or in effort:

1. **Self-asserted.** Saving a domain sets `verified = true` immediately. Cheapest; the
   column becomes decorative. Anyone can register a customer's domain and serve a widget
   there, which is the exact confusion §16.1's comment says the token exists to prevent.
2. **Token-checked.** Saving stores a token and leaves `verified = false`; a follow-up
   endpoint fetches a file (e.g. `/.well-known/eva-verify.txt`) or a `TXT` record and flips
   the flag on match. This is what the schema was built for, but the mechanism, the
   fetch-failure/timeout policy, and whether an outbound request from the API is
   acceptable at all are decisions I should not invent — the outbound fetch is a new
   server-side request surface, and §21.2 treats SSRF as a live concern.

I am not choosing between these, and I am not shipping reading 1 as a stopgap: it would
make the widget work while silently removing the only control that stops one tenant
serving a bot on another tenant's domain, and nothing in the product would look wrong.

HALT(ruling): Domain verification (see HALT-06) — option 1 (self-assert) or option 2
(token-checked)? If 2, which mechanism (well-known file / DNS TXT / meta tag) and what
timeout+retry policy?

Everything else in WP-19 that does not depend on this is done; ADMN-04 (knowledge admin)
and ADMN-10 (settings, which owns the domain UI) are the remaining M5 rows and neither can
close the loop without this answer.

## 8.22 HALT-07 — a bot serves one document, but the product is a multi-document knowledge base (2026-11-15, WP-19)

Building ADMN-05 (publish/preview) surfaced a contradiction between two frozen sections
and a measured consequence that makes ADMN-04's own screen misleading.

**The schema.** §16.1 puts exactly one pointer on the bot:
`bots.active_knowledge_version_id` (uuid). There is no knowledge-set or snapshot table —
I checked the live database and the migration; the only knowledge tables are
`knowledge_sources`, `documents`, `document_versions`, `chunks`. And
`document_versions.status` may be `'published'`, but **nothing ever writes it**: the
ingestion path goes `processing → ready` and stops.

**The code follows the schema.** `retrieval/pipeline.active_version_ids()` reads the single
pointer and returns `[pointer]` — one version. So the live scope is one `document_version`,
which belongs to one `document`, which belongs to one `source`.

**What that means for a real tenant.** "Publish" is therefore not additive, and the
documents table I just shipped lists many documents while only one can be served. Measured
on the running stack, not inferred:

- Uploaded `shipping.md` ("We ship to the EU and UK within five working days"), published it.
- Uploaded `returns.md`, published it. Pointer moved to the Returns version.
- Asked the live scope "Do you ship to the EU?" → **declined**, `below_confidence_floor`,
  best score **0.4367** against a **0.55** floor. The shipping document is unreachable.
- The same question against the *draft* (all versions) returns hits, so the content is
  there and indexed — it is the pointer that excludes it.

So an owner who uploads five documents and publishes each in turn ends up with a bot that
answers from one of them, and the screen gives no sign of it. That is the ADMN-04 failure
mode the registry row was supposed to prevent, one layer down.

**Why §10.3 and §10.5 appear to disagree.** §10.5 says "publishing swaps the tenant's
`active_version` pointer — instant rollback by re-pointing", which matches the schema and
the code. §10.3 says dense retrieval is filtered `WHERE tenant_id = … AND
knowledge_version = published`, which reads like many chunks across many versions carry a
`published` status — i.e. a published *set*. §17.3 says only "Atomically activate the
staged version". Nothing in the frozen docs says how a multi-document knowledge base is
served, and the two readings cannot both be true.

**Options, and what each costs.**

1. **Multi-document via `document_versions.status = 'published'`.** Publishing marks the
   target version published and keeps every other published version published; retrieval
   filters on that status instead of the pointer. This matches §10.3's predicate and needs
   **no migration** (the status value and the column both exist and are unused). Rollback
   becomes "unpublish this one". Cost: the pointer on `bots` becomes decorative, and
   "publish this document" silently means "add to the served set", so a new document
   *cannot* be staged without going live — the draft/live separation ADMN-05 is built on
   would need a second flag (e.g. a per-bot `published` marker) that the frozen schema does
   not have.
2. **Keep one live version, make it a whole-knowledge-base snapshot.** A "version" would
   have to become a set of document versions, which needs a new table — a migration, i.e.
   a §16.1 schema change.
3. **Keep the pointer and publish per document, but be explicit in the product** that one
   document is live at a time. This is what the code does today, and what I have made the
   screen say ("the bot serves one published version at a time"; the publish notice says
   "is now the only document the bot answers from"). It is honest but it is not a
   knowledge base: uploading ten documents and publishing one means the other nine are
   never used.
4. **Repoint the pointer at the newest version of *every* ready document** — i.e. treat
   "publish" as "publish everything currently ready". One pointer cannot express that, so
   this needs option 1's mechanism anyway.

I am not choosing. Option 1 changes what "publish" means and strands `bots.
active_knowledge_version_id`, which RAG-09 is recorded as done against. Option 2 is a
migration into a frozen schema. Option 3 is what ships today and is the least wrong thing
to *display*, but it makes ADMN-04's multi-document table a promise the product cannot
keep, and I would rather that be a ruling than my judgement.

What I have done rather than guess: the screen no longer implies that documents coexist
when live. The column is "Served", the table note says the bot serves one published version
at a time, and the publish notice names the document that was replaced. ADMN-05's preview
panel (draft vs live, side by side) is built and verified — and it is what made the
contradiction visible, because the two columns disagreed for a question the content
answers.

`HALT(ruling): Knowledge publish semantics — does a bot serve (1) every version
individually marked published (matches §10.3's `knowledge_version = published` filter, no
migration, but strands the bot pointer and needs a separate draft/live flag), (2) one
snapshot covering many documents (§10.5's single pointer, needs a new table = frozen-schema
change), or (3) exactly one document at a time (what ships now; not a knowledge base)?`

ADMN-05 is otherwise complete: publish, rollback (same code path), and draft-vs-live
preview all verified in Chromium against the running stack.


## 8.24 RULINGS (2026-11-17, user) — the four open decisions closed

Recorded verbatim, in the user's words, and the consequences each one authorises.

**HALT-07 — knowledge publish semantics: OPTION 2, snapshot table.**
User: "Expectation is a multi document knowledge base" / "Halt 07: option 2 snapshot table".
Consequence: a bot serves MANY documents, and "publish" promotes a whole set atomically.
This is a §16.1 schema change (G1 class), authorised by this ruling:
- new `knowledge_sets` (id, tenant_id, bot_id, label, created_at) and
  `knowledge_set_members` (set_id, document_version_id) — membership is explicit, so a
  publish is a set, not a diff of statuses.
- `bots.active_knowledge_set_id` (uuid, nullable) becomes the served pointer;
  `active_knowledge_version_id` is MIGRATED, not stranded: the migration creates a
  one-member set per existing pointer and re-points the bot at it.
- retrieval filters chunks by membership in the bot's active set, not by a per-version flag.
- draft/live separation: a set is draft until published; publishing swaps the pointer, so
  preview-vs-live (ADMN-05) keeps its two-column meaning and rollback re-points to the
  previous set.
- ADMN-05 unblocks. RAG-09's recorded "rollback proven" evidence is superseded by set
  rollback and must be re-stated against the new mechanism.

**HALT-03 / KNOW-10 — source priority: OPTION B, weighted boost.**
User: "Halt 03: option B weighted boost".
Consequence: similarity still decides; a per-source-kind multiplier adjusts the FUSED score
(after RRF, before threshold), default uploads ×1.15, faq ×1.10, crawl ×1.00. Weights live in
tenant config and are tuned against the WP-10 golden set, never hardcoded. Strong relevance
must still beat tier preference — the eval harness is the proof, and a regression there is
a gate failure. KNOW-10 unblocks.

**HALT-04 / RAG-08 — reranker: SELF-HOSTED.**
User: "Halt 04/ Rag 08: self hosted re ranker".
Consequence: cross-encoder rerank runs on this VM; tenant text does not leave the box
(consistent with the confidentiality charter). Budget ~1–2 GiB resident and CPU inference.
Reranking currently defaults OFF (`reranked: false`); after this row it defaults ON with a
per-tenant switch, and the turn's latency budget must be re-measured and recorded — a
reranker that blows the p95 budget is a gate failure, not an acceptable trade. RAG-08
unblocks.

**SEC-04 — BYOK precedence.**
User: "Sec 04: BYOK should override any platform key once development is completed".
Consequence: `default_provider()` resolves tenant BYOK FIRST, platform key as fallback only.
The precedence is per-tenant and per-provider: a tenant that stores an Ollama key uses it;
a tenant that stores nothing keeps the platform path. The fallback chain still applies
within whichever key is chosen, so one provider being down degrades rather than fails.
Credentials remain write-only and vault-backed; the platform key is never used for a tenant
that has supplied its own. SEC-04 unblocks.

**Queue effect:** ADMN-05, KNOW-10, RAG-08, SEC-04 leave HELD.
## 8.47 RULING (2026-11-19, user) — HALT-10: rollback restores the previous set

**User:** "I go with your recommendation."

**Ruling.** `POST /knowledge/rollback` must restore the **previous published set**, not
re-point the bot at one document alone. Today the endpoint builds a one-member set
(`version_ids=[version_id]`, labelled "Rolled back"), so an owner with five published
documents who rolls one back ends up serving that document **only** — the other four
silently stop being answerable. That is a data-integrity defect, not a missing button.

Consequences:
1. **Set history is required.** A `knowledge_sets` row already exists per publish; the
   change is to make the previous set discoverable by the bot rather than orphaned. The
   served pointer moves forward only through `point_at`, so "the previous set" is the set
   the bot pointed at before the current one — record that edge explicitly rather than
   inferring it from `created_at`, which two publishes in the same second would tie.
2. **Atomic or nothing.** A rollback either restores the whole previous set or refuses
   with a clear error when no previous set exists (first publish). It must never leave the
   bot serving a subset it did not name.
3. **Publish stays additive** — unchanged. Only rollback's meaning changes.
4. **The UI button (ADMN-15) lands with this row, not before it.** A button that quietly
   shrinks the knowledge base is worse than no button.
5. **RAG-09's recorded meaning is amended.** It said "re-point the bot at an earlier
   version"; after this ruling it is "re-point the bot at an earlier published set".

**Work:** new row **ADMN-17** (below). ADMN-15 folds into it. ADMN-05's status is closed
in the same pass, since its verification text already reads complete and only the shared
scope kept it `partial`.

**UNPARKED (2026-11-19 09:22 UTC, user).** OPS-05 was held on the premise "needs the M5
analytics screen". That premise expired: `dashboard/app/analytics/page.tsx` exists and
ADMN-08 already returns `top_unanswered_questions`. Re-scoped rather than unblocked as-is,
because the row is not a UI item — ADMN-08's field ranks *frequent* questions, and the loop
itself recorded that this "is not the same as unanswered ones". OPS-05 now covers:
(1) identify genuinely unanswerable turns (declined / below the confidence floor),
(2) cluster them semantically through the existing BGE-M3 embedding pipeline so ten
phrasings of one missing topic collapse to one gap, (3) rank by frequency and recency,
(4) surface the result on the analytics screen as an actionable list, and (5) correct
ADMN-08's misnamed field as part of the same change.

Process note: this is the third hold-list drift in the project (after the driver-prompt /
script contradiction and the `**backlog` matcher bug). The HELD set is now empty, and the
rule going forward is that a hold must name the condition that would release it, so the
next session can test the premise instead of inheriting the decision.
 OPS-05 remains parked
(needs the M5 analytics screen). HALT-06 (domain verification) is already ruled and unbuilt —
it moves up the queue because the widget snippet cannot work for a new tenant without it.

### 8.23 **WP-22** (2026-11-18) — upload hardening and retention, both measured rather than assumed

Two security items were done together because both are about what the product does with
data it was handed: SEC-07 (uploads) and SEC-09 (retention).

**SEC-07 — uploads.** The audit was a probe of the running stack, not a reading of the
code, and it found two defects the code review had missed:

1. **The suffix alone chose the parser.** An ELF binary named `report.txt` was accepted
   (`201`) and ingested as one text chunk; HTML named `notes.md` was likewise accepted.
   Bytes are now checked against the format the name claims — text must carry no NUL byte
   in its first eight, PDF must begin `%PDF-`, DOCX must be a ZIP — and a mismatch is
   `415 content_mismatch`.
2. **A truncated PDF returned `500`.** `pypdf` raises `PdfStreamError`, which is not a
   `ValueError`, so the router's `except ValueError` missed it. That is the client's error,
   so it is now `422 unreadable_document`, kept distinct from the mismatch code because the
   owner's next action differs (fix the export vs. fix the file type).

Confirmed and *not* changed: the 25 MB cap was already correct (`413` on 30 MB),
visitor uploads already sniffed magic bytes, and **no route serves stored bytes back**, so
there is no stored-XSS path today. That last fact is what made this a medium, not a
critical: the disguise attack lands in the parser, not the browser. Upload fuzzing and an
attachment-disposition policy for the day something *is* served back remain open on the
SEC-07 row.

**SEC-09 — retention.** §19.3 asks for a per-tenant window (default 90 days) enforced by a
scheduled purge. The policy is a small module with the deliberate failures: the default is
the *promise*, so a missing or malformed `settings` blob resolves to 90 (the failure mode
is deletion, never indefinite keeping), a write is refused outside 1–730 days rather than
silently clamped, and the reader re-bounds whatever it finds, because the blob is
hand-editable JSON.

The purge is the part that could have failed silently. The worker connects as `eva_app`,
which is **subject to RLS**, so the obvious `DELETE FROM conversations WHERE last_activity_at
< ...` matches zero rows and reports success, forever. It therefore loops tenants (the one
table with no RLS policy) and opens a `tenant_session` per tenant. Eligibility is
`closed`/`handed_over` aged on `last_activity_at`, so a long conversation is never deleted
while someone is in it; `usage_records` keeps its totals with the conversation link set to
NULL, and `audit_logs` is never touched.

**Verification, and why the tests can be trusted.** 18 tests in `tests/test_retention.py`
plus 5 endpoint tests. The suite was then **mutation-tested**: replacing the per-tenant
session with one unscoped session — the naive implementation — fails 2 tests, so the tests
distinguish a working purge from a silently-empty one instead of merely asserting that the
job returned. Live proof on the running stack: the job was enqueued on the real arq worker
and logged `deleted: 1`; psql then showed the 30-day-old conversation and its message gone
and the 1-day-old one intact.

**Measured:** main suite **538 passed, 2 skipped** (up from 515). Worker restarted and
confirmed registering `cron:purge_expired`; the upload probe re-run after rebuild returns
`415`/`422` where it previously returned `201`/`500`. The M5 gate was re-run after the
parser change and still passes every clause.

**Ruling requested, unchanged from §8.22:** M5's gate is evidence; whether it clears is the
operator's call.


### 8.24 **WP-22** (2026-11-17) — injection defences, and the CI step that could not say why it failed

SEC-05 and SEC-06 complete the security suite's injection half. Both were built as
corpora first, because the honest question is not "does a regex exist" but "what does it
score on text it has not seen".

**SEC-05 — visitor messages.** The gap was real: the prompt contract tells the model to
ignore instructions inside CONTEXT, but a visitor message is not CONTEXT — it arrives as
the user turn, in the position instructions normally occupy. The guard now runs in
`turn_events` before the message is persisted, so a refused attempt costs no tokens and
leaves no transcript row; it writes an audit row instead. The visitor sees a scope limit,
never the pattern name, because naming the rule is tuning advice for the next attacker.

Detection was measured against 25 benign questions as well as the attacks, and that
measurement changed the code three times:

1. a "refund or action demand" rule was **deleted** — it fired on the entirely legitimate
   "can you send me a replacement", and the behaviour it guarded against is already
   structurally impossible (§14 read-only, no tools);
2. a confusable map was added for Cyrillic/Greek lookalikes, and its limit measured
   honestly: NFKC does **not** fold Cyrillic `і` (U+0456) because it is a distinct letter,
   not a compatibility variant, so the fold recovers object words (`instructіons` →
   `instructions`) rather than the disguised verb;
3. a high-precision phrase rule catches the disguised-verb cases that the fold cannot.

**SEC-06 — documents.** Indirect injection never reaches the detector, so this half is
structural rather than lexical. Active content is stripped in `parse()`'s tail, not in the
HTML parser, because a `.md` upload carries a script tag just as easily as a `.html` one.
The prompt layer already had the right shape (nonce-tagged delimiters, untrusted reminder
*after* the data). What was missing was a test that states the limit instead of implying a
stronger guarantee: the builder does **not** escape delimiters out of document text, so a
document containing the literal closer does end the block early. The defence is 64 fresh
random bits per request. That is asserted in both directions, so replacing the nonce with
a fixed string fails the suite rather than silently weakening it.

**SEC-07 — fuzzing.** The corpus asserts one property: **no hostile file produces a 5xx**.
A 4xx is the product working; a 5xx means an exception escaped and the owner learned
nothing. Twenty cases, and it found a defect the earlier hand-written probe missed — the
binary check looked for NUL only, so a PNG (whose magic has no NUL in its first eight
bytes) was accepted as `.md`. The check now rejects C0 controls and DEL but still accepts
high bytes, because a Windows-1252 export is a text file and refusing it costs an owner
real work to catch no attack.

One finding is documented rather than fixed: `uri` returns a traversal filename verbatim
as a *display label*. Nothing builds a path from it — owner bytes live in
`document_versions.payload`, visitor files use server-chosen UUID keys, and the codebase
performs zero filesystem writes — so sanitising the label would be cosmetic. The test
asserts the property that actually protects (the row stays in the caller's tenant) and
records the latent risk for whoever adds a path-from-label later.

**Verification.** Main suite **580 passed, 2 skipped** (up from 538). Both new gates carry
mutation proofs, following `test_eval_gate.test_gate_blocks_a_seeded_regression`: emptying
the pattern table, loosening one rule to match everything, and (for documents) replacing
the real closer all make the same assertions fail. A gate that has never failed is not a
gate.

**CI, unresolved and stated as such.** `widget-browser` is red on every commit since the
three-engine Playwright matrix landed. The failing step reported only "exit code 1" in
four seconds — too fast to be a download, so the message was the defect, not the cause:
one command naming three engines produces one opaque exit code. The step now installs each
engine separately, names the engine that failed, prints `df -h /` (a full disk and a failed
download are indistinguishable from an exit code), and retries three times with backoff
because each engine is a ~100 MB download and a reset mid-transfer is the ordinary failure
mode. Job logs require admin rights to read, so CI is the only observer available. All
three engines install and launch cleanly on this host, so **this commit does not claim to
have fixed the failure — it makes the next run say what it is.**


### 8.25 **WIDG-16 / SEC-08** (2026-11-17) — the gate that passed by running nothing, and the 500 it was hiding

`widget-browser` has been green since run 44073291423. It was testing nothing. Both
facts came from the same run of work, and the second is the reason this section exists
rather than a one-line "CI fixed".

**The vacuous gate.** `browser.test.mjs` sets `skip` on every case when `WIDGET_BOT_ID`
is unset, and CI supplied it as `${{ vars.WIDGET_BOT_ID }}` — a repository variable that
was never created. So every job reported `tests 9, pass 0, skipped 9, exit 0`. `node
--test` treats a skip as a success, so the step exited 0, the job went green, and a
required check was satisfied by nine nothings. The failure mode is worth naming: a skip
is indistinguishable from a pass to every consumer of the exit code, so the gate could
not fail *and* could not be noticed. Nine browser clauses — the accessibility contract
(WIDG-11), Escape/focus return, the CSP clause (SEC-08), the origin refusal (WIDG-05),
the storage-before-consent clause — were marked done on the strength of it.

The second bug in the same job: "Start the API" ran `pytest tests/test_widget_serving.py`,
which uses `TestClient`. That is an in-process ASGI transport. Nothing ever bound a
socket, so even with a bot id the matrix would have had no server to load.

**What was done.** CI now migrates the stack database, boots a real `uvicorn`, seeds a
synthetic tenant-bot-origin through `scripts/seed_widget_bot.py` (deterministic, chartered:
no customer data, no PII), exports the printed `public_id`, and runs the matrix against
it. A final step parses the runner's own summary and **fails when `skipped` is not zero**
or when fewer than nine cases pass — the check whose absence made the green meaningless.
The repository variable is gone; the bot is constructed per run.

**The 500 the vacuous gate was hiding.** With a real bot, one case genuinely failed: the
panel answered nothing. The cause was not the test. `conversation.turn_events` called
`embedder.embed(...)` outside any handler, so an embedder that could not start — the API
image ships without `torch`, which lives only in the embedding worker — raised
`ModuleNotFoundError` through the stream and returned **500 on every visitor turn**.
That contradicts the module's own contract, item 5: *a provider failure degrades to the
offline message plus handover, never an exception*. The embedder is a provider, and the
code treated it as infrastructure. The fix guards retrieval and emits the same degraded
turn the LLM-outage path already emits. This is the class of defect a skipped test
cannot find: the assertion was correct and never executed.

Two smaller things the live run exposed. `SESSION_COOKIE_SECURE` defaults to true, so on
a plain-HTTP stack the browser drops the session cookie and every follow-up call is
unauthenticated — the local analogue of the cookie problem that broke the seeder. And
`WIDGET_TOKEN_SECRET` being unset makes widget sessions 503 by design (`configured()`
fails closed rather than sharing a default key), which is correct behaviour that a
local harness must supply.

**Evidence.** Against a migrated, freshly seeded database with **no embedding model
installed** — the CI condition — all nine cases pass on chromium, firefox and webkit
(9/9/9, `skipped 0`). The degraded turn is what the end-to-end case exercises there, and
the case was written to accept it. The seeder's FAQ step therefore warns instead of
exiting: requiring `torch` to seed a bot would have made the fixture usable in exactly
one image. Main suite: 581 passed, 2 skipped.

**The step boundary was itself a bug.** The first attempt put the server in one step and
the matrix in the next. GitHub terminates any process a step leaves behind when that step
ends, so the matrix would have met a connection refused — a failure that reads like a
widget defect and is not one. Server, seed, three-engine loop and the non-vacuity check
now share one shell, with `trap` killing the server on exit, and every path absolute
because the shell `cd`s into `backend/api` to make `app.main:app` resolve the way the
Dockerfile runs it. The step's own `run:` block was extracted from the YAML and executed
verbatim before pushing, rather than re-typing an equivalent block and hoping the two
agreed — the earlier miss (a relative seeder path that worked locally only because my
simulation had an extra `cd` back to the root) is exactly what that mistake looks like.

**What CI now does not prove.** With no embedding model present, the seeder cannot create
a knowledge version, so the browser matrix's end-to-end case takes the *degraded* turn —
the handover message — rather than a retrieval-grounded answer. That is a genuine
narrowing: the panel, the stream, the storage audit, the origin check, the CSP path and
the accessibility clauses are all exercised end-to-end, but "a visitor gets a relevant
answer" is covered only by the pytest suite (`HashingEmbedder`, no torch) and not through
a browser. Making the browser path answer for real needs a torch-free way to seed a
published version — either a deterministic test embedding written straight into the
vector column, or a fake embedder selectable by config. Both are product decisions about
the schema and the config surface, so neither was taken here. Recorded as a gap rather
than left to be assumed from the green.

**The gate then found a real accessibility defect, which is the point of un-blinding it.**
With the matrix genuinely executing, chromium and firefox failed WIDG-11: *"Escape closes
the panel and focus returns to the launcher."* The cause was not a flaky test. `Escape` was
bound only inside the widget's iframe document. The launcher button lives in the **host**
document, so when focus sat on the launcher — where a click leaves it — the keydown never
reached the frame and the panel could not be closed by keyboard at all. Whether it passed
depended on where each engine happened to park focus after opening, which is why the same
code was green locally on all three engines and red in CI on two.

The fix binds `Escape` on the host document as well, gated on the panel being open, with
the in-frame path unchanged. The test was also at fault: it pressed `Escape` from whatever
position focus happened to be in, so it asserted whichever half of the contract the engine
provided. It now sets focus on the launcher, asserts the host-document path, reopens and
asserts the in-frame path. Mutation-tested: reverting the loader fix fails with *"Escape
from the host page left the panel open"*, restoring it passes, 9/9 on all three engines.

A second defect surfaced in my own CI step while proving cleanup: `( cd … && uvicorn ) &`
makes `$!` the **subshell's** pid, so `kill $server` orphaned uvicorn onto init and the
server outlived the job, poisoning later local runs against a dropped database. The
subshell now `exec`s the server so the trap kills the process it names.

**Ruling needed (unchanged).** M5 still awaits the operator ruling restated in §8.22 and
§8.23. Nothing here touches it.


### 8.26 **WP-23** (2026-11-17) — data-subject erasure, and the audit row that must not undo it

SEC-10 is §19.3's rights clause in one line: *"deletion = delete conversation records +
visitor identifiers; data-export = transcript export (R32 doubles as the SAR mechanism)"*.
Both halves exist after this WP, and both are proved rather than claimed.

**The delete is one statement with a large correctness argument behind it.** Deleting the
`conversations` row cascades to `messages`, `feedback` (through `messages`), `handovers`
and `visitor_files`, all `ON DELETE CASCADE` on `conversation_id` — so the "visitor
identifiers" half is satisfied by the cascade only because those identifiers
(`visitor_session_id`, `visitor_name`, `visitor_email`, `page_url`) are *columns on the
conversation*, not child rows. Two things deliberately survive: `usage_records`, whose
`conversation_id` is `ON DELETE SET NULL`, so billing totals stay correct without the
link; and `audit_logs`, which is append-only and never purged.

**The trap this WP was written around.** The obvious way to make an erasure auditable is
to log the record that was erased. That would take the subject's name, address, session id
and page URL and deposit them in the one table nothing in the system can ever delete —
the erasure would be undone by its own receipt. The audit detail therefore carries
**counts** (`messages`, `feedback`, `handovers`, `visitor_files`) and the conversation id,
and a test asserts each of the four identifier values is absent from it, plus a test that
a 404 writes no erasure row at all, so a failed attempt cannot claim one happened.

**`active` conversations are erasable; retention's are not.** The purge (SEC-09) takes
only finished conversations because it deletes without being asked. Erasure is the
opposite, and refusing a live conversation would deny the right for exactly as long as the
visitor keeps talking. The stated cost: the visitor's next turn answers
`conversation_not_found`, which is the widget's ordinary answer for a conversation that no
longer exists.

**The export half was incomplete, and a SAR would have been answered wrongly.** The
transcript export is the access mechanism, so it has to name everything erasure removes.
It did not: `visitor_session_id` was in neither the JSON payload nor the text rendering,
and the visitor-file metadata was absent entirely. Both are now in both formats, and
`format_version` moved 1 → 2 because the payload shape grew — a consumer reading version 1
keys is not silently handed a different document. The file list is `[]` rather than
missing for a conversation with no uploads: "we hold no files" has to be a statement the
export makes, or the subject cannot tell it from an omission.

**Contract note (§17).** §17.5 lists `GET /conversations/{id}` and
`GET /conversations/{id}/export`, not a DELETE. The route added is
`DELETE /api/v1/conversations/{id}` — a new *path* under an existing resource, with no
change to any §17 route's request or response shape and no schema change. It is recorded
here as a deviation in the §8.4 sense rather than treated as free: the alternative was to
call deletion unsupported while §19.3 lists it as an MVP right, which is worse than an
additive endpoint. The operator's call if they want it expressed differently (e.g. a
`POST /conversations/{id}/erase`), since that is a genuine contract decision rather than
an implementation detail.

**Access control.** Admin, not viewer — reading the archive is not consent to destroy it,
matching the other destructive routes (credentials, webhooks). Another tenant's
conversation answers **404**, not 403: a 403 confirms a guessed UUID exists, which is the
enumeration oracle the checklist names. Mutation-tested, with one honest negative result:
removing `AND tenant_id = :tenant_id` from the `DELETE` does **not** leak, because RLS
refuses the row anyway. So the isolation test proves isolation holds, not which layer
holds it — the same finding WP-19 recorded for the read path. The predicate stays as
defence in depth, and the test says so instead of implying the application layer is what
saves it.

**What is deliberately not done.** Visitor identifiers held outside Postgres: there are
none. The rate limiter is in-process dicts keyed on `tenant:visitor_session_id` with a
60-second window (`controls.py`), not Redis state, and the codebase performs no filesystem
writes — visitor uploads store metadata only. If either changes, the unlink belongs in this
module, which is why it is written down rather than assumed. `audit_logs` rows describing
the conversation's *lifecycle* (`conversation.injection_refused`) also survive, by design:
they carry a pattern name and an id, no visitor PII, and the log is the record that the
tenant was operated on.

**The settings screen was saying the opposite, and that is a compliance claim.** Its Data
retention card read *"Nothing in the deployment deletes conversations on a schedule…
Transcripts are kept until that is built"*, which SEC-09's daily purge had already made
false and SEC-10 makes doubly false. The card now states what happens (retention window,
daily purge of closed conversations only, erasure on request, export available) and says
plainly that the *window* has no control on this screen rather than implying nothing is
enforced. The module docstring said the same outdated thing and is corrected with it. A
screen that under-reports a deletion control is the safer error, but it is still an error:
a tenant reading it would answer their own customers wrongly.

**Verification.** 15 new tests in `tests/test_erasure.py`, mutation-tested five ways:
dropping `session.commit()` fails 7 (the `tenant_session` trap, reproduced deliberately);
removing the visitor identifiers from the export fails 2; putting an email in the audit
detail fails 1; downgrading the role gate to viewer fails 1; dropping the tenant predicate
fails 0 and is documented above rather than passed over. Live through Caddy on the rebuilt
stack: export `format_version` 2 with the session id and `visitor_files: []`; `DELETE` →
204, then `GET` → 404 and a second `DELETE` → 404; psql showed the conversation and its
message gone and the audit detail `{"feedback": 0, "messages": 1, "handovers": 0,
"visitor_files": 0}`; a viewer got 403 with the row still present. Main suite **593 passed,
2 skipped**; eval gate **6 passed**.

**Ruling requested, unchanged from §8.22.** M5's gate is evidence; whether it clears is the
operator's call. Nothing here touches it.

**Commit and CI (`825fd9d`).** The `test` job — pytest (`593 passed, 2 skipped`) and the
eval gate (`6 passed`) — **passed**. The separate `widget-browser` job **failed**, on
`WIDG-11 "Escape closes the panel and focus returns to the launcher"` in chromium and
firefox. That is not this WP's code, and the evidence is mechanical rather than a claim:
`git rev-parse HEAD:backend/api/widget` is `7ed153c` — **byte-identical** to `155d027`,
whose own run passed both jobs; and `git diff 155d027..825fd9d` touches no file under
`backend/api/widget`, `dashboard`, `.github` or `scripts`. The same signature fails on five
of the nine commits before this one (`1984fce`, `28b010`, `654647`, `d3880c`, `124ca33`),
so it is a pre-existing intermittent in the widget, not a regression from SEC-10. The
diagnostics added in `155d027` narrow it usefully: the annotation shows `hostKeys =
["Escape"]` and `expanded = ["false@594", "true@595"]` — the key arrived, the panel
closed, and it **reopened one tick later**. That points at the app frame's `ready` post
(`app.tsx:384`) reaching the loader's `case "ready": void openChat()` (`loader.ts:484`)
after the close, which a load-timing difference in CI would expose and a fast local run
would not. Not fixed here: G5 scope is SEC-10, and a one-line guess at a race in a file
this WP does not own would be worse than reporting it precisely.

**This is a G6 question, and it is the operator's.** G6 makes a security-suite regression
blocking; the widget-browser job is not the security suite, and nothing it asserts about
SEC-10 changed. But the repo's own standard is "deploy before believing a test count", and
the honest reading is that the `widget-browser` check is red on this sha for a reason this
WP did not cause and did not fix.

**Resolved by the next sha.** `1ba051f` (the report/copy commit) changed no widget file —
its `backend/api/widget` subtree is the same `7ed153c` — and CI came back **green on both
jobs** (`test` and `widget-browser`, run 44100106103). That converts the analysis above from
inference to proof: the same tree, the same case, opposite outcomes, so the failure is
intermittent and independent of this WP. The row cites `1ba051f` as its green sha; the
`825fd9d` red is kept in this record because deleting it would hide a flaky gate that will
still bite someone else. The `ready`-after-close race sketched above remains a real
candidate and remains unfixed.

### 8.27 **WP-25 / SEC-11** (2026-11-17) — the notice is generated, because prose about code goes stale

**What was asked.** A privacy-notice template and a DPA skeleton naming sub-processors
(LLM providers) and retention (§19.3, R30).

**The design decision.** A hand-written notice is a set of claims about behaviour that
nothing checks. This repo already lost time to exactly that shape: a `bot_domains` fixture
that seeded `verified = true` kept a dropped security filter green, and an SSE `final`
event that carries `degraded` on two branches and omits it on a third. So the documents
are *rendered* — `app/compliance/facts.py` reads the provider table from
`conversation.providers.PROFILES`, the retention window from `retention.policy`, the purge
schedule from `WorkerSettings.cron_jobs`, the prompt/upload limits from `prompt` and
`uploads`, and the data inventory from the migration files — and
`app/compliance/render.py` writes `docs/compliance/privacy-notice-template.md` and
`docs/compliance/dpa-skeleton.md`. `tests/test_compliance_docs.py` (20 tests) re-renders
and diffs, so a code change that invalidates a sentence turns the suite red rather than
publishing a false promise.

**The asymmetry worth keeping.** `vault.ALLOWED_PROVIDERS` accepts four providers —
`ollama_cloud`, `deepseek`, `openai`, `anthropic` — but only the first two have a runtime
profile in `PROFILES`, and only a profile can receive a prompt. Listing all four would
*overstate* the processing chain; listing from a hand-written list would eventually
*understate* it. The list is derived, and a test asserts both directions: every
`PROFILES` entry appears in both documents, and `openai`/`anthropic` appear in neither.
Embeddings are self-hosted (`embed` service, `EMBEDDING_QUERY_BACKEND=remote` pointing at
it), and the HALT-04 ruling (2026-11-17) puts the reranker on this VM too, so neither is a
third party and neither is named. That ruling arrived *after* these documents were written;
the sentence they relied on said "the approved stack contains no reranker", which the ruling
makes false without changing the conclusion. If a reranker is ever hosted rather than
self-hosted, it becomes a sub-processor and this derivation has to learn about it — today
nothing in `PROFILES` would notice.

**Six claims written and then removed, because the code does not support them.**

1. *"The business can perform the copy and the deletion from its dashboard."* — The
   export is a dashboard link (`exportUrl` in `dashboard/lib/api.ts`, used by
   `/conversations/[id]`); **there is no delete call in the dashboard at all**, only in
   the API (SEC-10). The notice now says the copy is self-service and deletion is
   performed by the operator using the erasure operation. That is a real gap in the
   product, recorded here rather than papered over by a template.
2. *"Attachments are visible to the business's team."* — `app/widget/router.py` sniffs
   the bytes, writes `visitor_files` metadata and **discards the payload**; no bytes are
   stored and nothing ever reads the file back. The notice now says the file is not kept
   or opened.
3. *"The purge runs at 03:15 UTC."* — the hour and minute are the code's, the
   timezone is not: `arq` reads the container's local timezone and the worker image
   sets no `TZ`, so on a host at UTC+2 the job runs at 01:15 UTC. The documents now say
   "by the worker's own clock", which is the only honest statement the repository can
   make. A reader who needs UTC has to pin `TZ`.
4. *"Sessions are revoked on password change."* — I wrote it from habit; there is no
   change-password route in `app/auth/router.py` at all. Corrected in both places it
   appeared (§6 and the `sessions` appendix row), and a test now fails if the phrase
   returns while no such route exists. A stale sentence surviving in the appendix after
   §6 was fixed is the concrete reason the drift test compares the *whole file*.
5. *"Encryption at rest" and "each control is covered by an automated test."* — The
   application encrypts BYOK keys with `age`; it does **not** encrypt the database volume,
   and that is the operator's to provide. Both statements were weakened to what is true,
   and the volume-encryption gap is now a `[[FILL: ...]]` the operator must answer.
6. *"Data-subject requests are self-service."* — DPA §4 promised both halves as
   self-service. Only the **export** is: it is a dashboard link. Erasure is an API
   operation the operator runs on the Business's instruction (finding 1, same gap, and
   the DPA had repeated it after the notice was corrected). Fixed in the clause that
   makes the promise, not just in the notice — a contradiction between two documents in
   the same work package is still a false statement. `test_dpa_does_not_call_erasure_self_service`
   now fails if the word returns, and it was mutation-checked by restoring it.

**The limit the notice states instead of hiding.** `audit_logs` is append-only and never
purged (R29, migration `0003`), and SEC-10 deliberately writes only counts and the
conversation id into an erasure event. So the notice promises complete deletion *except*
that record, and says why it exists. A GDPR notice that claims unconditional erasure
while an un-purgeable log survives is the kind of over-claim that is worse than the
disclosure.

**What is a placeholder, not a claim.** 17 `[[FILL: ...]]` markers cover the legal facts
no repository can know: the parties' names and addresses, the breach-notice window, the
D3 chain position (`docs/ARCHITECTURE.md` D3 is still open — the platform is described as
processor with model providers as *its* sub-processors, which is the position the code
implements), governing law, liability, the hosting sub-processor's terms, each model
provider's terms and region, whether the database volume is encrypted, backup retention
(no restore drill exists yet, so the DPA says so), and the effective date. A test asserts
each declared marker is used and each used marker is declared, so one cannot silently
disappear in a future edit.

**Mutation evidence.** Adding a fourth entry to `PROFILES` fails three tests (files
differ, the processor list differs, and the new provider has no declared display name),
which is what proves the derivation is real. Appending one line to a committed document
by hand fails one test. Restoring both returns 20 passed.

**Suites, measured this session with CI's commands.** Backend
`pytest --ignore=tests/test_eval_gate.py` → **613 passed, 2 skipped**. Eval gate
`pytest tests/test_eval_gate.py` → **6 passed**. Compliance guard → **20 passed** (part of
the 613).

**CI.** `217fcce` — run 44141398490, jobs `test` and `widget-browser` both `success`.
That is the newest sha containing the code, and the sha the registry row cites; the commits
after it change Markdown only. Read from the run page, because the REST API refused every
call this session with `API rate limit exceeded`.

**The WIDG-11 flake bit again, on the docs-only commit.** `b660863` (run 44130728574)
was `test: success`, `widget-browser: failure`, and the single failing case is the same
one `docs/PLAN.md` §8.26 predicted: *"Escape closes the panel and focus returns to the
launcher"*, with the annotation showing `expanded=["false@595","true@596"]` — the panel
closes and reopens one tick later, exactly the `ready`-post/`openChat` race recorded
there. `b660863` changes three Markdown files and no source; its
`backend/api/widget` subtree is `7ed153c`, byte-identical to `c50809c` (green), to
`c80afee` (green), to `1ba051f` (green) and to `825fd9d` (red).

**It recurred a third time on `f931b52`** (run 44135948134): same case, same
annotation, `test: success`, `widget-browser: failure`, widget subtree still `7ed153c`.
**At least seven reds** (`825fd9d`, `b660863`, `f931b52`, `55bcbaa`, `5f04724`, `4e6b6e6`,
`a70d490`) against nine green (`1ba051f`, `c50809c`, `8a8aa92`, `c80afee`, `b991d6e`,
`e6bf90f`, `9ed4b0e`, `217fcce`, plus the runs cited earlier as clean) over one identical
tree — every one of them `7ed153c`,
which I re-checked with `git rev-parse <sha>:backend/api/widget` rather than trusting the
earlier note. `test` was green on **every single sha**. That is conclusive: the gate is
intermittent and unrelated to this WP. Three reds landed on this WP's own docs-only commits
(`5f04724`, `4e6b6e6`, `a70d490`), so the flake did not spare the change either, and each
names the case outright — *"firefox failed (exit 1): ✖ Escape closes the panel and focus returns
to the launcher (WIDG-11)"* — on a subtree identical to the eight passes.

**Why this paragraph no longer gives an exact count.** Every commit triggers a fresh run, so
an exact tally is stale the moment it is written — I rewrote this number three times in one
session, and each rewrite spawned the run that falsified it. Hence "at least seven": true
however many runs follow. The durable statement is the fixed input, not the count: the widget
subtree is `7ed153c` on every run named here, and `test` has been green on **all** of them.
Anyone re-checking this row should run `git rev-parse <sha>:backend/api/widget`, not count. Left unfixed deliberately — it is a widget
defect, out of scope for SEC-11, and quietly repairing a widget race inside a
compliance work package would hide it from the row that should own it.

**One honest gap in that tally.** On `55bcbaa` (run 44137037142) the failing step was
again `Browser matrix`, and the widget subtree was again `7ed153c`, but the run carried
only a generic *"Process completed with exit code 1."* annotation — no case name. Job
logs need admin rights (the log API returns 403) and the anonymous API then rate-limited
this session, so **that one red cannot be attributed to the Escape case from outside**.
It is counted in the tally as a red run on an unchanged tree, not as a confirmed repeat
of the same case. Treating it as confirmed would be exactly the pattern-matching
`docs/PLAN.md` §21.3 warns about.

**Consequence for the registry row.** The row cites `c50809c`: both jobs green, and it
contains the substance of the WP. The five shas after it touch only Markdown plus one
renderer string, and every one of them passed `test` (`8a8aa92` and `b991d6e` green on
both jobs, `b660863`, `f931b52` and `55bcbaa` red on `widget-browser` alone). No sha in
this WP has a green `widget-browser` *and* the final wording — that is the flake, not
the content. Citing the newest sha to look tidy would be the false evidence G4 exists
to prevent, so the row names the sha it can defend and this section names the rest.

**Not done, deliberately.** SEC-12 owns the CCPA/CPRA categories, the "do not sell or
share" position and the statutory response windows; the notice says so and does not
pre-empt it. No lawyer has reviewed either document, and both are templates, not
published notices.


### 8.28 **WP-26 / KNOW-10** (2026-11-17) — source priority, and where a boost must not be applied

KNOW-10 was the last open M1 row, blocked on HALT-03 until §8.24 RULINGS closed it as
**OPTION B**. Implemented in `app/retrieval/priority.py`, applied between RRF and
rerank in `pipeline.retrieve`.

```
fused(d)   = RRF(d)                                  RETRIEVAL §5
boosted(u) = fused(d) x weight(knowledge_sources.type)  RETRIEVAL §7
```

**The placement question the HALT named is answered, not dodged.** HALT-03 warned that a
tie-break (option C) "would be erased by RRF" and a boost (option B) "needs a defined place
in the fusion order". The boost multiplies `rrf_score` in the stage immediately after
`reciprocal_rank_fusion` and before `apply_rerank`. Two consequences, both recorded in
RETRIEVAL §7 rather than left for a reader to infer:

- With reranking **on** (the default) priority decides which candidates enter the top-20
  window; the reranker then owns the final order. That is a real but bounded effect, and
  it is the honest description — calling it "the final ranking" would be false.
- With reranking **off** priority owns the order outright.

**The boost is on the ordering score, never the similarity.** `score` stays the dense
cosine that the confidence floor is defined against; only `rrf_score` is scaled. Applying
it to `score` would let source kind lift a chunk over the floor, turning a ranking
preference into a confidence claim the evidence does not support. A test asserts `score`
is byte-identical after boosting.

**Weights are config, as the ruling requires.** Defaults `upload 1.15`, `faq 1.10`,
`crawl 1.00` live in `DEFAULT_SOURCE_WEIGHTS`; a tenant overrides them under
`tenants.settings.retrieval.source_weights` — the frozen §16.1 schema, so no migration.
Values are clamped to `[0.5, 2.0]` and unknown kinds ignored: a typo must not create a tier
the pipeline cannot reason about, and an out-of-band weight would silently become hard
tiering (option A, rejected).

**"Strong relevance must still beat tier preference" is a test, not a hope.**
`test_strong_relevance_still_beats_tier_preference` puts a crawl at RRF 0.030 against an
upload at 0.020 × 1.15 = 0.023 and asserts the crawl stays first.

**Mutations, both red.** Deleting the `apply_source_priority` call fails the end-to-end
ordering test **3/3** runs. Replacing the sort with hard tiering
(`priority_weight > 1.0` as the primary key) fails
`test_strong_relevance_still_beats_tier_preference`. Neither guard is decorative.

**The tenant predicate is stated even though RLS enforces it.** `source_kinds_for_versions`
joins `document_versions → documents → knowledge_sources` and carries
`v.tenant_id = :tenant_id` explicitly. A test passes another tenant's version id and
asserts `{}` comes back, so a dropped predicate is caught by the test rather than being
masked by row-level security — the same finding WP-19 and WP-23 recorded.

**A preview must rank like production, or it is a lying diagnostic.**
`knowledge/router.py` reads the same tenant weights the live turn reads; a preview that
scored differently from the served path would mislead the owner who uses it to tune.

**Known gap, stated rather than papered over.** The WP-10 golden corpus is
**single-source-kind** (`run_eval.py:62` inserts every source as `'upload'`), so the eval
gate passing proves the boost did not regress *those* queries — it cannot detect a
priority regression, because every candidate gets the same weight. The ruling calls the
eval harness "the proof" that strong relevance beats tier preference; today the honest
proof is `test_strong_relevance_still_beats_tier_preference`, and making the golden set
multi-kind is follow-up work the ruling's own wording implies. That is recorded here
rather than claimed as done.

**Suites, measured this session with CI's commands.** Backend
`pytest --ignore=tests/test_eval_gate.py` → **626 passed, 2 skipped** (613 before this WP,
13 new). Eval gate `pytest tests/test_eval_gate.py` → **6 passed**, unchanged.

### 8.29 **WP-27 / SEC-12** (2026-11-17) — the US notice, and the widget claim that was false for one read

§19.4 asks for four disclosures: collection, no sale or share, no training on tenant
conversations without explicit opt-in, and deletion inside the statutory window. All four
are now generated from the code by `app/compliance/us_state.py` into
`docs/compliance/us-state-privacy-notice-template.md`, the third document in the SEC-11
pattern. `tests/test_us_state_notice.py` (28 tests) makes each claim fail with its cause.

**The four facts, and why each is derived rather than written.**

| Claim | Source of truth | What goes red if it stops holding |
|---|---|---|
| Categories A–L collected/not | the live catalog, via `CATEGORY_COLUMNS` | an unattributed `visitor_*` column, or a category naming a dropped column |
| No sale, no share | absolute asset refs in the shipped clients + both `package.json` manifests | a third-party `<script>`, or a tracker dependency |
| No training | `providers._payload`'s keys + the endpoints the client calls | a training field in the request, or any path but `/chat/completions` |
| Deletion in the window | `erasure.router` + `retention.policy` | a renamed erasure route, or a changed default/min/max window |

The 45-day CCPA response window is the one value that is **not** derived, because it is a
statute. It is `CCPA_RESPONSE_DAYS`, a named constant citing `§1798.130(a)(2)`, and the
document says so in the sentence rather than presenting it as a computed number.

**The claim that was wrong, and how it was found.** The first draft of the notice said the
widget "writes only session-scoped values that are gone when the tab closes". Reading
`shared/identity.ts` to check it showed a `DURABLE_KEY` in `localStorage`, written from
`remember(enabled)`, reached by the `app.tsx` checkbox *"Remember this conversation on this
device"* — R42's opt-in, implemented and undocumented in the notice. The paragraph now
describes the durable identifier, quotes the checkbox label (read from the source), and
`test_the_durable_identifier_is_an_opt_in_not_a_default` asserts that `remember()` is the
only writer of that key and that the checkbox starts `useState(false)`. This is the SEC-11
lesson repeating in a new place: a sentence about another language's code is the sentence
most likely to be false.

**A mutation caught a vacuous test, which is the only reason to run them.** The first
version of `training_position().opt_in_keys` scanned the settings modules for *string
literals*. Adding `train_on_conversations: bool = False` to `RetentionSettings` — an
annotated field, not a literal — left the suite green, because the scan could not see it.
The scanner now collects `ast.AnnAssign` targets as well, and the same mutation fails
`test_no_training_setting_exists_in_tenant_settings` for the stated reason. Every mutation
was run, not assumed: third-party script 3 red, training field 2 red, dropped
`visitor_name` 1 red, opt-in setting 1 red after the fix, 45→30 days 2 red.

**Category K is "not collected", deliberately.** The confidence score and the cited
passages describe the *answer's* support. Calling that an "inference drawn from personal
information" would put a profile of the consumer in a document when no profile exists.

**What the documents refuse to claim.** Deletion propagation to a model provider is not
implemented — no code asks a provider to delete a retained prompt — so DPA §5.2 states
that as a gap rather than promising a control, and a test asserts both documents contain
the disclaimer. Backups are likewise unclaimed for the SEC-11 reason (no restore drill).
`test_the_notice_does_not_claim_automated_deletion_propagation` parses the three
provider-touching modules for a deletion call and fails if one appears without the prose
being rewritten.

**Not verified.** No lawyer has read either document. Five `[[FILL: ...]]` markers remain
(state scope, request channel, verification method, opt-in mechanism, effective date).
Whether any given US state's law applies to a given business is that business's
determination, which is why it is a marker and not a generated sentence.

**Suites, measured this session with CI's commands.** Backend
`pytest --ignore=tests/test_eval_gate.py` → **655 passed, 2 skipped** (626 before this WP).
Eval gate `pytest tests/test_eval_gate.py` → **6 passed**. `widget-browser` was **green**
on `5dbed5a` (run 44168173910), which is worth recording because §8.27 measured it failing
about half the time on the unchanged widget subtree.

### 8.30 **WP-28 / SEC-13** (2026-11-17) — the scan that had to be able to fail

§19.1 asks for "dependency scanning + SAST in CI". The row is one bullet, and the
temptation is to satisfy it with a third-party action in the workflow file: green badge,
no code, nothing that fails for a reason a reader can act on. What shipped instead is
`scripts/security_scan.py` (stdlib only) plus a `security` job that runs it and is not
`continue-on-error` (§21.3).

**Measured first.** Before writing a rule, the tree was measured: 158 resolved Python
packages across three requirements closures with **0 OSV advisories**; 91 npm packages
across two lockfiles with **6 advisories** (Preact 10.27.2 JSON-VNode injection;
Playwright 1.49.1 unauthenticated browser download; four PostCSS advisories reached
through Next.js 15.5.29); and, for SAST, no `eval`/`exec`, no `shell=True`, no
`verify=False`, no `pickle`, no `md5`/`sha1`, one `innerHTML` (a literal SVG in
`loader.ts`), and **three** SQL sites built by interpolation. The scanner's job is then to
keep that number from growing, which is a different and more useful property than "no
vulnerabilities".

**Dependency scanning resolves, it does not inspect the host.** `pip install --dry-run
--report` per requirements file asks pip what the image would install, so a package that
is importable in the scanner's environment but absent from the image cannot mask a
finding, and an unpinned transitive dependency cannot hide behind its parent. One batched
OSV call covers all of them. npm is audited from `package.json` + `package-lock.json`
copied into a scratch directory, so the CI checkout needs no `node_modules` and the audit
reads the same lockfile the image builds from; a second `--omit=dev` run decides whether
an advisory is `shipped` or `dev`.

**The SQL rule is the one that earned its keep.** The first version treated any bare
`ast.Name` interpolant as a module constant, so `f"SELECT * FROM {table_name}"` — the exact
injection shape — passed. Tightening it (only literals, attributes, ALL-CAPS names, and
numeric casts count as safe) found two sites the loose rule had missed, in
`conversation/archive.py`, and retired the `dimension_migration.py` exemption because the
constant-plus-`int()` pattern became understood rather than merely tolerated. All three
surviving sites are the same reviewed shape: an allowlist of literal predicate strings
joined into a WHERE clause, with every value bound through `params`.

**The baseline is time-boxed, and keyed to the statement.** Each entry carries `why`,
`scope` and `review_by`. The gate fails on an unlisted finding, on an entry that matches
nothing (so a fix forces its entry to be deleted rather than left as a permanent hole), on
an expired entry, and on a malformed one. SAST keys end in a digest of the offending
source segment: without it, baselining one accepted interpolation in `botconfig/router.py`
would silently accept the next one added to the same file — a per-file exemption wearing a
per-finding costume. Both directions were mutated to prove it: injecting a second
interpolation into the baselined file fails the gate, and the reviewed site stays covered.

**Exit 2 is a first-class outcome.** 0 clean, 1 findings, 2 the scanner could not
complete. An unreachable OSV must not render as "no vulnerabilities", which is the single
worst failure mode for a security gate, so transport failure retries and then raises. The
scan also asserts floors on what it read (60 Python files, 10 JS files, 20 packages per
requirements file, 2 npm trees), because zero findings from zero input is the same green
as a clean tree — the failure mode §8.25 recorded for the browser matrix.

**What is deliberately not claimed:**

- **The six npm advisories are carried, not fixed.** Preact needs 10.27.3+, and the
  upgrade was applied during this WP and built and tested clean, then reverted: moving a
  shipped bundle changes `sizes.json` and the widget's own assertions, which belongs to
  the widget rows. The four PostCSS advisories need Next 16, a semver-major framework
  change and an architecture decision. Both are recorded with a 2026-12-17 review date, so
  they reappear as work rather than sitting silent.
- **The JS half is regex, not a parser.** No stdlib JavaScript parser exists, and adding
  one is a dependency (G3.1). The rules are narrow — `eval`, `new Function`,
  `document.write`, `dangerouslySetInnerHTML`, and `innerHTML` assigned non-literal markup
  — and comment lines are skipped so the rules do not fire on the two files that merely
  discuss `innerHTML`.
- **Container rebuilds on base-image updates** are the other half of the §19.1 bullet and
  are not in this row. They need a registry and a rebuild trigger, which is OPS work.
- **The secret scan is a shape check, not entropy analysis.** It covers the credential
  formats this stack can hold (AWS, GitHub, provider `sk-`, age identity, PEM blocks) and
  reports a path and a line, never the value: a scanner that echoes what it found writes
  the secret into a CI log, which is a durable copy of the thing it flagged.

**The gate was exercised where it runs.** The scanner was run from a bare clone with no
`node_modules` and no project install, which is the CI job's actual starting state; it
resolved, audited and reported clean. A test asserts the scanner imports only the standard
library, because a scanner that needs the app installed cannot run before the app is
installable — a dependency problem would then mask a security problem.

**Suites, measured this session with CI's commands.** Backend
`pytest --ignore=tests/test_eval_gate.py` → **716 passed, 2 skipped** (655 before this WP,
+61 in `tests/test_security_scan.py`). Eval gate → **6 passed**. Widget `node --test` →
39 passed / 9 skipped, dashboard `npm test` → 7 passed (both unchanged). Three mutations of
the scanner each turn the suite red: the shell rule disabled, the SAST fingerprint
removed, staleness detection disabled.

**CI, read for this WP's own shas.** Code `0a038c1`, run 44184927224: `test`
**success**, `security` **success** (the new job), `widget-browser` **failure** at the
browser-matrix step. Docs `fe021a3`, run 44186223745: **all three jobs success**. The
widget subtree is byte-identical across all three shas (`7ed153c` on `16b39e2`, `0a038c1`
and `fe021a3`), so the same tree that failed a browser case one commit earlier passed it
one commit later — the WIDG-11 Escape race recorded in §8.26/§8.27, which `1e72bc5` and
`f72a2a9` also hit on that subtree. This is the clearest evidence yet that the failure is
intermittent and unrelated to the commit, because the two commits differ only in
Markdown. Job logs need admin rights (HTTP 403 from the logs endpoint), so the step-level
conclusion and annotation are what is available; the widget row still owns the fix.

### 8.31 **WP-29 / OPS-01** (2026-11-17) — logs that can be joined, and a guard for what is not in them

§22's first bullet asks for JSON logs carrying a per-request correlation id, a tenant id
and a conversation id, with conversation content never logged at info level. The last
clause is the one that cannot be satisfied by adding something, so most of the work here
is about *where* the ids come from and what proves the absence of the rest.

**Ids must be bound to a shared object, not `ContextVar.set()`.** FastAPI runs sync
dependencies and sync endpoints in a worker thread via `anyio.to_thread.run_sync`, and
every such call receives a **copy** of the context. A `ContextVar.set()` inside
`current_user` is therefore invisible to the endpoint it just authenticated and to every
line that endpoint writes — the ids would appear exactly where they were not needed and
vanish where they were. The module keeps one mutable dict in a single `ContextVar` and
*mutates* it; copies share the object, so a bind in a dependency reaches the response
handler's lines. `set()` is used only by the middleware, which runs in the request's own
task. The mutation that matters was run, not assumed: rewriting `bind` as a per-field
`ContextVar.set()` turns **2** tests red — the access-line test and the real-request test,
i.e. exactly the lines the ids are supposed to reach.

**The access line is the application's, and uvicorn's is silenced.** `uvicorn.access`
writes `INFO: 1.2.3.4:5 - "GET / HTTP/1.1" 200`: not JSON, and emitted *after* the
response completes, by which time the context has been reset — so it can never carry a
correlation id. `configure()` clears the `uvicorn.access` handlers and leaves it
non-propagating; a pure-ASGI middleware (`RequestContextMiddleware`, not
`BaseHTTPMiddleware`, which would interpose a queue around the SSE stream) writes exactly
one JSON line per request in a `finally`, before the context is reset. It logs method,
**path only** and status: `widget.upload_file` takes the visitor's file name from
`?filename=`, so the query string is one refactor away from carrying visitor text.

**The client's `X-Request-ID` is validated, not trusted.** It is accepted only if it
matches `[A-Za-z0-9._-]{1,64}`; anything else is replaced with a fresh uuid4 hex. JSON
encoding already prevents forging a second line; the charset and the length cap stop a
caller writing kilobytes per request. The id is echoed in the response header so a
support ticket can be joined to the logs.

**The worker had a second logging stack, and it was the first one to run.** The worker
banner (`Starting worker for 5 functions`, `redis_version=...`) stayed plain text after
the first attempt, which configured logging from arq's `on_startup`. arq's CLI calls
`logging.config.dictConfig(default_log_config(...))` **before** it runs `on_startup`, so
the banner preceded our configuration. The fix is `--custom-log-dict` in the worker
`CMD`: that hook is handed a dict from `obs_logs.dict_config()` at exactly the moment
arq would otherwise install its own. Verified on the running stack — the banner is JSON
now, and `docker compose exec worker ... purge_expired` emits its report as JSON.
`tests/test_logging.py` applies the dict for real rather than asserting its shape,
because a bad `formatter` reference raises inside `dictConfig` at boot.

**The purge job's loop is scoped, not bound.** It iterates tenants inside one job;
binding without restoring would label the summary line — the one an operator reads — with
whichever tenant happened to be last, which is worse than no tenant at all. `scoped()`
restores the previous values on exit.

**Content: proven absent two ways, because either alone is weak.** A canary string is
driven through a *real* turn over the wire (`POST /conversations/{id}/messages`), so it
passes the router, the turn loop, retrieval, the prompt builder and the provider; it must
not appear in any captured record, while the conversation id *must*. That catches code
that exists today. The second check is an AST guard over `backend/api/app` and
`backend/worker/worker`: an info-or-above call handed a name like `question`, `answer`,
`content` or `page_url` fails the suite — and a `len(...)` argument is skipped so counts
stay loggable, while a sibling argument in the same call is still inspected (both
directions tested, so the exemption cannot launder the rest of the call). The guard
asserts a **floor** on the number of log calls it read, because zero findings from zero
files is indistinguishable from a clean tree (§8.25, §8.30). The canary test also asserts
that records were captured at all: a `configure()` that replaced the root handlers would
have made it pass silently, which is why `configure()` now *adds* its handler instead of
swapping the list.

**Two lines at container start are still uvicorn's text.** "Started server process" is
logged before the lifespan runs, so it precedes `configure()`. Fixing it would mean
configuring logging at import time, which would also reconfigure pytest on import. Left
as a known bounded gap and documented in the module: those lines carry no request and no
tenant. The worker does not have the gap.

**Live check, not just the suite** (§: `curl` and `TestClient` do not see the deployed
wiring). Rebuilt `api` and `worker` and drove the stack through Caddy:
`GET /api/v1/conversations` as a registered tenant produced
`"request_id": "live-tenant-read", "tenant_id": "37f070f6-..."`; the widget flow
(`session` → `conversations` → `messages/after-handover`) produced both ids on every
line; and `docker compose logs api | grep -c SECRETVISITORWORDS` returned **0** with the
message body present in the request. `X-Request-ID` came back as a response header.

**Suites, measured this session with CI's commands.** Backend
`pytest --ignore=tests/test_eval_gate.py` → **738 passed, 2 skipped** (716 before this WP,
+22 in `tests/test_logging.py`). Eval gate → **6 passed**. `scripts/security_scan.py` →
clean, 9 findings all baselined. Four mutations were run, each red: the tenant bind in
`current_user` removed (1 red), `bind` rewritten as a per-field `ContextVar.set()`
(2 red), the access line demoted to `debug` (5 red), and a
`logger.info("turn question: %s", question)` added to the turn loop (2 red — the canary
*and* the AST guard). A first attempt at the access-line mutation did not apply — its
string anchor did not match the source, so the suite ran green against unmodified code.
That is the same false-green §8.30 records for a minified bundle; the anchor was
corrected and the mutation re-run before the count above was written.

**CI, read for this WP's own shas.** Code `2e91cc1`, run 44196944177: `test` **success**,
`security` **success**, `widget-browser` **success** — all three. Docs `616f336`, run
44197674252: `test` **success**, `security` **success**, `widget-browser` **failure**, and
the annotation names the case: *"✖ Escape closes the panel and focus returns to the
launcher (WIDG-11) … Escape from the host page left the panel open
(focus=BUTTON:Close chat, expanded="true")"* on **both** chromium and firefox. The widget
subtree is byte-identical across the two shas (`7ed153c` on both, and on `16b39e2`,
`0a038c1`, `fe021a3`), and `git diff 2e91cc1 616f336` touches only `AGENTS.md`,
`docs/FEATURES.md` and `docs/PLAN.md`. So the same tree that passed the Escape case one
commit earlier failed it one commit later with no code change — the §8.26/§8.27 flake,
confirmed again, still owned by the widget row. Job logs return HTTP 403 to this token, so
the annotation is the evidence available. The third sha, `c5147c1` (run 44201507508), also
Markdown-only and also at `7ed153c`, repeated it: `test` and `security` green,
`widget-browser` red on *the same case* (chromium). Three shas, two with the defect firing
and one without, one unchanged widget tree — which is why this record stops here rather
than chasing a green docs run.

### 8.32 **WP-30 / OPS-02** (2026-11-18, code `f67af87`) — a trace that was never exported, and the bug that found

§22's bullet asks for OpenTelemetry traces on the conversation turn: retrieval ms, LLM
time-to-first-token and total, and per-provider errors. All of it is implemented. **None
of it is exported**, and that is the honest headline: there is no collector in §20.1's
topology and no SDK or exporter in the image, so the spans are created and discarded. What
OPS-02 delivers is instrumentation that is correct *when* a collector exists, proven by a
recording provider in the tests. Reading this row as "traces now visible in Grafana" would
be wrong; that needs OPS-03's stack plus a ruling on a collector container.

**No dependency was added, and that was checked, not assumed.** `opentelemetry-api`
**1.45.2** is already installed because pinned `fastapi==0.163.1` declares
`Requires-Dist: opentelemetry-api>=1.44.0`. Only the API is present: `import
opentelemetry.sdk` raises, and `trace.get_tracer()` hands back a `ProxyTracer` whose spans
are `NonRecordingSpan` with `is_recording() == False`. So instrumentation is free, an SDK
would be a new dependency (G3.1 → halt), and the absence was measured by calling the API
rather than inferred from a `pip list`.

**The bug this WP found cost an hour and is the reason to read this section.** The obvious
implementation is `tracer.start_as_current_span(...)`. These spans are opened **inside an
async generator that yields while they are open**, so the attach/detach pair straddles a
suspension point. A probe (not a theory) showed the failure precisely: when the *consuming
task* is cancelled — every SSE visitor who closes the tab — the generator is closed from a
**different context**, and `use_span`'s `detach` raises
`ValueError: <Token ...> was created in a different Context`, which OTel logs as
`ERROR opentelemetry.context: Failed to detach context`, leaving the context `ContextVar`
skewed. So `span()` uses `start_span` and passes the parent explicitly; the active span is
one `ContextVar` that `event()` reads, with a guarded `reset` because a cancelled task
never returns to the `finally` in the context that set the token. Mutation-checked by
reverting to `start_as_current_span`: **2 tests red**, one of them the cancellation test.

**That cancellation test was itself a test that could not fail, and the mutation caught
it.** The first version cancelled *the same task* that owned the generator, so the
generator was still closed in the original context, no detach error was possible, and the
test passed against the broken code. It now cancels a separate consumer task and calls
`aclose()` from the test's own task. This is §8.25's lesson in a new place: a mutation that
stays green means the test is the thing to fix.

**A hostile test double poisoned 36 unrelated tests.** The recording `Span` first raised
from `record_exception` to prove the module never embeds an exception body. But FastAPI
0.163.1 ships its own instrumentation and calls `get_tracer` per request, so the
session-scoped provider answered *every* scope and the raise landed inside FastAPI's own
error handling — `test_widget_tokens.py` went from 45 passed to 36 failed, alone but never
in the full suite, which is exactly the cross-module shape §8.24 records. Baseline was
measured before blaming the change: **738 passed / 2 skipped** at `744e285`, and the 36
failures disappeared when the change was stashed. The fix is scope filtering in the
provider (`NoOpTracer()` for any scope but `eva.conversation`) — a fixture in one file must
not change the framework's behaviour for the other 737 tests — plus a plain source-reading
test for `record_exception`. The suite is back to **748 passed / 2 skipped** = 738 + the 10
new tests.

**What each span carries, and what it deliberately does not.**
`conversation.turn` (tenant id, conversation id, declined/degraded/model/confidence from
the `final` event it already emits, so the trace cannot disagree with what the visitor
saw), `retrieval.search` nested inside it, and `llm.stream` with
`llm.time_to_first_token_ms` and the helper's `duration_ms`. `duration_ms` is set by
`span()` for every span rather than at each call site, so "retrieval ms" and "the LLM's
total" cannot drift or be forgotten on one branch, and the clock is `perf_counter` so a
system clock adjustment cannot produce a negative duration. The turn wrapper reads outcome
attributes from the events the loop already yields; `_turn_events` is the old body
unchanged, because re-indenting 400 lines to add a `with` is how an event-ordering contract
gets broken.

**Per-provider errors are events on `llm.stream`, not spans.** The fallback chain is
*nested inside* the LLM call: a chain that tried three providers is one LLM call with three
facts, and a span per attempt would triple the tree for one visitor turn. `event()` reads
the active span from the module's `ContextVar`, so the `LlmProvider` protocol did not have
to grow a span parameter — a provider that had to know about tracing is a provider that
cannot be faked in a test, and WP-13 built that seam deliberately. A tripped breaker is
`provider.skipped` (reason `circuit_open`) rather than an error, which is the distinction an
operator needs: one is capacity, the other is a fault.

**Content never reaches a trace, and the guard can fail.** A canary through the *real* turn
loop must not appear in any span's name, attributes or events, while the conversation id
must — the second half is what makes the absence meaningful. Mutation-checked: adding the
question as an attribute turns it red. `error_event` records
`type(exc).__name__` and never the exception, because `record_exception` puts the
exception's message into the trace — and a provider error message is built from up to 300
characters of the provider's response body (`providers.py:212`), which can echo the prompt.
That is a stronger rule than OPS-01's logging guard needed: a log line stays in the
operator's own stack, a trace usually does not.

**Gaps, stated not hidden.** (1) Nothing is exported — no SDK, no exporter, no collector;
`is_recording()` is `False` in this deployment. A collector container plus
`opentelemetry-sdk`/`opentelemetry-exporter-otlp` is a new dependency and a topology
change, so it is a ruling, not a package install. (2) `llm.model` and `llm.provider` come
from `chosen_model` and `getattr(provider, "key", None)`; the model is a config default
today, so the attribute documents configuration rather than a provider response. (3) The
worker's ingestion path has no spans — OPS-02 names the conversation turn, and ingestion
latency belongs with OPS-03's metrics rather than being smuggled in here.

### 8.33 **WP-32 / OPS-04** (2026-11-18) — alerts that had nothing to watch, and a budget stop that cannot fire

OPS-04 asks for five alerts and a runbook. **None of it was built, and the halt is the
deliverable.** Registry row moved `backlog` → `blocked (HALT-09)`. No source file was
touched. The suite was run *before* any edit to fix the baseline at **748 passed /
2 skipped** — identical to WP-30's and WP-31's record — so this turn cannot be blamed for a
later red, and cannot claim one either.

**The inherited explanation was wrong, and that is the substantive finding.** Both AGENTS.md
and the OPS-03 row say OPS-04 "inherits whichever metrics transport the ruling picks", i.e.
it is blocked behind HALT-08. Measurement says otherwise: an alerting poller does not need
Prometheus. `redis` is a hard `Requires-Dist` of the pinned `arq==0.28.0`, is already
installed in the `api` image, and `LLEN arq:queue` reads 0 from both `api` and `worker`;
`shutil.disk_usage` is stdlib and reports the same 336746889216 bytes from both. **Two of the
five signals are buildable today with no new dependency.** So OPS-04 is blocked on its own
grounds, and granting HALT-08 would not have moved it.

**Three of the five cannot fire, because the event is never written down.**

- *Provider error-rate spike* has neither a numerator nor a provider dimension. No provider
  failure is persisted — the degraded path logs `logger.exception("llm stream failed …")`
  and returns, and there is no log sink in §20.1 to read it back from. The usage ledger
  cannot stand in: **`provider` is NULL on 22 of 22 rows**, because the sole `record_usage`
  call omits `provider=` and is reached only on the answered path.
- *Budget exhaustion* is a **dead branch in every deployment**, and this is a product defect,
  not just an alerting gap. `units` is computed as `(prompt_tokens or 0) + (completion_tokens
  or 0)` while the only caller passes `None` for both, so it is structurally 0. Measured:
  22 usage rows, `sum(units)=0`, zero with `units>0`; 48 assistant messages, zero carrying
  `prompt_tokens`, `completion_tokens` or `latency_ms`. Running `check_budget` against a real
  tenant returns `used=0, budget=1000000` and raises nothing. R31's ledger is therefore zero,
  CONV-14 cannot trip, and `GET /analytics/summary`'s token figures are 0. The providers
  already parse real counts (`providers._usage`); they are dropped at the call site.
- *Backup failure* watches a job that does not exist. There is no producer anywhere — no
  script, no cron, no service, no volume, no marker table — and OPS-09 is the row that would
  create one.

**Two thresholds per signal, zero specified.** The blueprint names the five signals and never
a number: "spike" has no window or ratio, "disk usage" no percentage, "backlog" no depth. The
only matches in the full blueprint for `threshold` are the answer-confidence floor and the
mitigation "Scale worker count". Choosing them is policy.

**"A monitored address" is absent, and the mail path cannot reach one.** `eva-smtp-sink` is
not in `compose.yaml`; it is an ad-hoc container running `python sink.py 2525` that
**writes each message to `inbox/msg-NNN.eml` and returns `250`** — captured, not delivered.
No `ALERT_*` or operator-address key exists in code or `.env`, and `SMTP_*` are not declared
in `.env.example` at all. The one genuine mailbox field, `settings.handover.email` (set on 17
tenants), belongs to the **tenant owner**: alerting there would ship platform topology and
error details to customers, which the confidentiality rule forbids.

**What the halting bought.** Reading A — a Prometheus-free `arq` cron emailing disk and queue
alerts — is buildable in an hour, but it delivers 2 of 5, invents both thresholds, and sends
to a file sink, so §27's actual acceptance line ("alerts firing to a monitored address") would
remain false while the row looked done. That is the same "instrumentation with no transport"
shape WP-30 and WP-31 each recorded. The written runbook was skipped for the same reason: a
procedure for five alerts that never fire, at unset thresholds, to no address, is untestable
documentation.

**A gap found while measuring, outside this row.** `/health` and `/ready` answer **200 inside
`api`** but **404 through Caddy** — `caddy/Caddyfile` routes only `/api/*`, `/widget/*` and
`/demo/*` to the API and sends everything else to the dashboard. Compose's container probe
calls `127.0.0.1:8000` directly so nothing is broken today, but an external uptime monitor on
the public origin would sit on a permanent 404 and could not tell a dead API from a routing
rule. Recorded, not patched: the fix puts an unauthenticated probe on the public origin and
§17 does not list it.

**Gaps, stated not hidden.** (1) Nothing was implemented, by design. (2) The token-ledger
defect is reported and untouched — it is not OPS-04's row, and fixing it changes what a turn
writes. (3) Queue depth is a snapshot; a poller would need its own sampling interval, which
is one of the unanswered thresholds. (4) `LLEN` sees only the main queue — arq's per-job
retry keys and in-progress set would need their own definition of "backlog", which §22 does
not supply.

### 8.34 **WP-33 / OPS-09** (2026-11-18, code `670516f`) — a backup that proves it is one, and the restore trap that hides

OPS-09 asked for *"daily off-box `pg_dump` backup (plus files) + timed restore drill +
written runbook and rollback procedure"* (§19.1, §20.4, §27). Registry row `backlog` →
`done`. Three artefacts, all runnable: `scripts/ops09_backup.py` (`backup`, `verify`,
`drill`), `ops/systemd/eva-backup.{service,timer}` for the daily schedule, and
`docs/ops/RUNBOOK.md`. `tests/test_ops09_backup.py` is 45 tests; backend **797 passed / 2
skipped** (752 + 45), eval gate 6 passed, security scan clean.

**The line that had nothing behind it.** PLAN §8.33 recorded that OPS-04's *backup-failure*
alert "watches a job that does not exist — no script, cron, service, volume or marker", and
that OPS-09 owned creating one. It did not exist. There was no `pg_dump` invocation in the
repository, no timer, no backup volume in `compose.yaml`, and no record anywhere that a
restore had ever been attempted. §20.4's "restore latest pg_dump + files to a fresh VPS,
RTO target: hours" was therefore not a plan with an untested step; it was a plan with no
first step.

**A `--data-only` restore does not work, and every row count still matches.** The restore
order is the trap, and it cost a measurement to find. `pg_restore --data-only` into a
newly migrated database replays `COPY` in archive order while the schema's foreign keys are
already live, so the first dependent table aborts — measured: `insert or update on table
"usage_records" violates foreign key constraint ... Key (conversation_id)=(76f05724-…) is
not present in table "conversations"`, 16 tables skipped, `tenants` 75 but `conversations`
**0**. Restoring the *whole* archive into an **empty** database is the path that works, and
it is also the only one that restores the RLS policies and the grants. The runbook says so
in those words.

**`--no-privileges` is the failure a counts-only drill calls a pass.** Measured: with it,
the restored database holds 787 conversations, 1047 messages and all 17 RLS policies, and
every count the manifest records matches — while `eva_app` gets `ERROR: permission denied
for table conversations` and the restored deployment would answer every request with a 500.
The grants are per-table and per-verb (SELECT on 19 tables, INSERT 19, UPDATE 18, DELETE
18) and they travel in the archive, so the drill reads **one tenant's conversations as
`eva_app` inside a tenant scope**: that needs the grants *and* the policies, and is the
assertion that fails when the restore drops privileges. Measured live: 719 of 719 visible
scoped, **0 visible unscoped**. The unscoped read is a separate test because a lost policy
makes the same query *succeed* and return too much — the branch a "the query worked" check
misses.

**Roles are not in `pg_dump`,** which is why the runbook's step 3 creates the database
before restoring. `CREATE ROLE eva_app LOGIN NOSUPERUSER NOBYPASSRLS` is done idempotently
by migration code (`APP_ROLE`), so a restore depends on the role pre-existing on the
cluster — a fact that is invisible until someone tries it on a fresh box.

**The drill measures 25.76 s end to end** (create 1.62, stage 0.26, restore 2.73, compare
1.70, integrity 6.67, rollback 12.77), restoring **2876 rows across 19 tables** with the
alembic revision intact. (The row count is a dev-stack snapshot and drifts as fixtures are
seeded — the same drill re-run after seeding the widget bot for the browser matrix restored
**2898** and stayed green. The invariant is *restored == dumped for every table*, plus the
table count; nobody should read 2876 as a property of the system.) That is the §27 *"backup restore drill completed and timed"* line,
and it is a measurement of a scratch database on the same box — **not** the §20.4 RTO,
which is an operator exercise on a fresh VPS and is recorded here as still unmeasured.

**The rollback half is tested on every drill, and it is narrower than it looks.**
`alembic downgrade -1` then `upgrade head` runs against the restored copy
(`0006_widget_public_lookup` → `0005_version_payload` → `0006_widget_public_lookup`, 4.87 s
/ 4.38 s). What that proves is that the **head** revision is reversible, which is the
rollback a bad deploy needs; it does not prove every migration is, and the runbook says
that rather than implying the stronger claim. Downgrading further is not exercised and
`0001`'s `downgrade` deliberately only revokes this database's privileges.

**Three confidentiality decisions, because a backup is the easiest place to leak.**

1. **The age identity is excluded, always.** `vault.py` keeps the identity apart from the
   ciphertext on purpose — *"the identity is on the host so that a backup of one is not a
   backup of the other"*. Archiving both would make the backup a plaintext key store.
   `identity_paths()` matches by **name as well as** by environment variable, because an
   identity at the default path is named by no variable; measured exclusion on the live
   vault: 16 ciphertext files archived, 1 key excluded, and a test asserts a
   `age-identity.bak` beside it is excluded too.
2. **An unencrypted dump is refused by default.** A `pg_dump` carries password hashes,
   session token hashes and every tenant's data. `OPS09_AGE_RECIPIENT` is required and
   `--no-encrypt` says so in the manifest. The dump is piped **through** `age`, so the
   plaintext never lands on disk and a crash cannot leave an unprotected copy behind.
3. **A destination inside the repository is refused outright** — one `git add -A` from
   being committed — and a destination on the database's own filesystem is *warned* about
   and recorded as `same_filesystem_as_database: true`, with `off_box_proven` staying
   `false`. The script cannot mount the operator's disk, so it measures rather than claims:
   on this box the destination and the pgdata volume are the same device (64771).

**The vault ciphertext is root-owned and a host backup cannot read it.** Written by the api
container as root at mode 600, so an unprivileged `tar` reads zero files and exits 1. The
script streams `tar -czf -` and retries through `sudo -n tar`, recording
`read_with_privilege: true` — found by running it, not by reading the code, and the failure
would otherwise have been a silently empty files archive.

**Retention is opt-in and there is no default number.** `[[FILL: backup_retention]]` is
still a marker in the DPA because no blueprint section specifies a period. `--keep` prunes
whole backup *sets* (five differently-suffixed files whose suffixes nest, so stripping one
extension — the first implementation — prunes nothing), and the absence of a default is
tested on the CLI rather than described.

**Because this row completed the drill, two compliance sentences became false and were
regenerated, not left standing.** The DPA said *"Until the restore drill is complete and
timed, this agreement must not claim a backup regime"* and the US notice *"no restore drill
exists yet"*. Both now describe the regime that exists and keep the bounded disclosure that
actually matters: **an erasure does not reach a backup**, no restore-and-redact procedure
exists, and the retention window therefore bounds the one copy of a conversation that a
deletion cannot touch. That is a consequence of this WP, not a new row — but it is the kind
of claim that is only true until the next work package, which is why it is rendered.

**Mutation-checked, all eight red.** Dropping the required-table check (2 red), adding
`--no-privileges` back (1), archiving the identity (1), ignoring the unscoped read (1),
removing the table floor (1), letting the drill target any database (1), accepting a
rollback that does not move the revision (1), allowing a repository destination (1). A
mutation also found a real defect: an RLS refusal raised out of `rls_check` instead of
being reported, which aborted the drill and then hung in cleanup. The refusal is now a red
verdict with the postgres error in it, and every docker call carries a timeout.

**A `pg_restore --list` needs a seekable file.** Piping the dump into the container gives
`did not find magic string in file header`, so `verify` copies the archive in and lists it
there. Two things this avoids: `pgvector/pgvector:pg16` is the only image with `pg_restore`
(neither `api` nor `worker` ships it), and binding a host path into a throwaway container
would have needed the file to be readable by the daemon.

**No new dependency and no new image.** The backup runs on the **host**: `pg_dump` is in
the postgres image, `age` is on the host, and the vault is root-owned there. A
containerised cron would need `pg_dump` and `age` in a product image plus the docker socket
mounted — two supply-chain surfaces for one job — so the schedule is a systemd timer the
operator installs, not a compose service.

**CI, read for this WP's own shas.** Code `670516f`, docs `7696f24`. Run 44287792039
(`7696f24`): `test` **success**, `security` **success**, `widget-browser` **failure** at step
10 "Browser matrix" with an **empty** annotation — a bare step conclusion, the signature
§8.26/8.27 and §8.33 record for the WIDG-11 Escape flake. It was ruled out rather than
assumed: the widget subtree is byte-identical at `7ed153c` on `eddd994`, `670516f` and
`7696f24`, `git diff eddd994..7696f24 -- backend/api/widget` is **empty**, and the matrix
re-run locally against the live stack at this sha passed **9/9 on chromium and 9/9 on
firefox** with `skipped 0` — including the Escape case. The first local attempt reported
`skipped 9` because `WIDGET_BOT_ID` was unset; that is the skip guard working as designed,
and it is not evidence, so the matrix was seeded and re-run before this paragraph was
written.

**Still the operator's, recorded rather than implied**: the off-box destination, the age
recipient and identity, the retention number, and the **backup-failure alert** — this row
created the producer that OPS-04's alert was waiting for, which removes OPS-04's dependency
inversion but does not implement it. A genuine fresh-VPS RTO is also unmeasured.

### 8.35 **WP-34 / OPS-11** (2026-11-18, code `848d6bf`) — the gate was on the wrong surface, and the receipt that would have undone an erasure

§22's last bullet is one sentence with two halves: *"Access control on observability:
conversation-level debugging requires owner role + is itself audit-logged."* Both halves
are implemented; the work was mostly in deciding **what** "conversation-level debugging"
means here, and the first reading was wrong.

**The obvious reading — gate the transcript route — is the wrong control.** A viewer is
*supposed* to read conversations (ADMN-06), and the transcript text is what the handover
email carries and what the SAR export returns. Putting `RequireOwner` on
`GET /conversations/{id}` would have deleted the product feature the row below it exists
for. The facts §22 calls observability are the ones describing *how* an answer was
produced rather than *what it said*: which model served the turn, how slow it was, and
what it cost in tokens. Those are what move.

**The measured finding that shaped the change.** The transcript route was already
answering `model`, `latency_ms`, `prompt_tokens` and `completion_tokens` to **any** role.
So adding an owner-only `/debug` route alone would have shipped a control with a second
door onto the same data — decorative. The four fields are now `null` below owner on the
transcript view. `confidence` stays, deliberately and not by oversight:
`compile_transcript` has rendered it into the text the owner receives by email since
HAND-04, so hiding the field would leave the same number one paragraph away in the body.
Gating a value that is already in the email is theatre.

**The export is deliberately not narrowed, and that is a ruling-sized choice made
conservatively.** `GET /conversations/{id}/export` is R32's data-subject access
mechanism. Narrowing it to protect operational data would answer a legal request with a
partial record. A test asserts a *viewer* still receives the telemetry from the export,
so a future "tighten everything" change goes red instead of being found by a subject.

**The receipt had to be committed, and that is the bug `tenant_session` invites.** The
first implementation wrote the audit row and returned; the route answered 200 and
recorded nothing, because `tenant_session` closes without committing. `session.commit()`
is explicit with a comment saying why. Dropping that one line turns **4** tests red.

**What the receipt must not contain.** `audit_logs` is append-only and never purged
(R29), so a receipt quoting the conversation would deposit the transcript — and with it
the visitor's name, email and session id — into the one table nothing can ever delete,
undoing an erasure by its own receipt. This is exactly the trap SEC-10's erasure route is
built around, so the detail is `{"turns": n, "usage_records": n}` and both the debug view
and the stored JSONB are asserted absent of message content and of every seeded visitor
identifier. Mutating the detail to include a content-shaped value turns 2 tests red.

**A mutation that did not go red, recorded rather than papered over.** Dropping
`AND tenant_id = :tenant_id` from the conversation lookup leaves the whole suite **green**,
because RLS has already refused the row. Measured directly: as `eva_app` in a tenant scope
that owns nothing, 0 conversations visible and 0 by id. This is the same finding WP-19
(reads) and WP-23 (erasure) recorded, and it means the predicate is defence in depth and
legibility, **not** the guard. The docstring says so and a separate unscoped test pins the
policy, because the failure mode of a lost RLS policy is a query that *succeeds and returns
too much* rather than one that errors.

**A stated limit: refusals are not audited.** A non-owner is stopped by `RequireOwner`
before the handler runs, so a 403 leaves no trace. Auditing denials needs a
role-check-shaped dependency that can write — a wider change than this row, and §22 asks
for debugging *access* to be recorded, every instance of which is. Recorded here and in
the module docstring rather than left to be discovered.

**Deviation, written up rather than assumed free.** §17.5 lists `GET /conversations/{id}`
(transcript), `/{id}/export` and `/api/v1/analytics/summary`; it does not list `/debug`.
The new route adds a path **under an existing resource** with no schema or shape change —
the same class of addition SEC-10 recorded for `DELETE /conversations/{id}`. If the
operator prefers the diagnostics folded into the transcript view behind a role check (no
new path, no new response model), that is a contract call, not an implementation detail.

**Evidence.** 17 tests in `tests/test_observability_access.py`; baseline fixed before any
edit at **797 passed / 2 skipped**; after, **814 passed / 2 skipped** (797 + 17), eval
gate **6 passed**, security scan **clean**. 5 mutations run — the commit (4 red),
`RequireAdmin` (1 red), the transcript telemetry gate (2 red), a content-shaped audit
detail (2 red), and the tenant predicate (**survives, explained above**). Exercised on the
live stack **through Caddy**, which in-process tests cannot see: rebuilt `api`, registered
a tenant over HTTP, and observed owner `200` with `model=deepseek-v4-flash latency=377
prompt=99`, exactly one `conversation.debug_viewed` row in `/audit-logs` carrying the right
actor and entity, viewer `403 requires role owner or higher`, and the viewer transcript
with `model=None latency=None` while `content` and `confidence` stayed intact. CI on
`848d6bf` (run **44295556727**) is **green on all three jobs** — `test`, `security` *and*
`widget-browser`; the WIDG-11 Escape flake did not fire on this sample, which changes
nothing about that defect being unfixed.

**M6 after this row**: `OPS-03` (HALT-08), `OPS-04` (HALT-09), `OPS-05`, and the rest of
WP-25's §27 sweep remain open. `OPS-07`'s uptime-alerting half still points at OPS-04, and
`/health` through Caddy is still a 404 (recorded in §8.33; not touched here).

### 8.36 **WP-35 / FUT-04** (2026-11-18) — a dedicated database, and the worker that was pointed at the wrong one

§16.3 promises *"Prototype = shared schema; single-tenant later = same schema, dedicated
database (R13)."* R13 defers the commercial single-tenant offering, so this row is
**readiness**, not a product: the claim is that deploying one tenant onto its own database
needs a `POSTGRES_DB` value and no code fork.

**The schema half was already true, and that was worth establishing rather than assuming.**
A fresh database migrated with the repository's own `alembic upgrade head` in **2.0 s**
produces an object-for-object identical deployment: 19 tables, 156 columns, 17 RLS
policies *with their predicates*, 19 per-table `eva_app` grants, 47 indexes, 63
constraints, 169 functions, 3 extensions, the append-only `audit_logs` trigger, and
`relrowsecurity`/`relforcerowsecurity` set on all 19 relations. Measured by diffing a
fresh dedicated database against the live shared one — they agree on every one of those
dimensions today, which is a *different* fact from the one this row needs (see the
reproducibility note below).

**The deployment half was not true, and one line was the whole defect.** `compose.yaml`
had `api` building both DSNs from `${POSTGRES_DB:-eva}` and the `worker` pinned at
`.../eva`. Setting `POSTGRES_DB=customer_acme` therefore rendered an API talking to the
customer's database and an ingestion worker writing chunks into the **shared** one — a
silent fork in the direction that matters, because the widget would answer from an
unpopulated knowledge base while every health check passed. Fixed to follow
`${POSTGRES_DB:-eva}`, with a comment naming the failure. This is exactly the class of
bug the in-process suite cannot see (AGENTS.md: "the pytest suite calls the ASGI app
in-process, so it structurally cannot see proxy, container or compose wiring"), so it
took a `docker compose config` render with a non-default `POSTGRES_DB` to expose it.

**A measured accident became a guard, and it is the most transferable finding here.**
Migration `0001` runs `ALTER ROLE eva_app ... PASSWORD` on *every* upgrade. A role is
**cluster-wide** while a database is not, so migrating a second database on a shared
cluster with a different `EVA_APP_DB_PASSWORD` **rewrites the credential the first
deployment is using**. Observed directly: after migrating a scratch database with
`EVA_APP_DB_PASSWORD=SOMEOTHERPW`, `eva_app` authenticated with the new password on `eva`
and was **refused** with the old one — the running stack's runtime login changed out from
under it. (Repaired immediately; the live login was re-verified working afterwards.) The
script therefore refuses with exit 2 when the role exists and rejects the configured
password, and *allows* a missing role, because on a genuinely dedicated cluster `CREATE
ROLE` is the correct outcome. `pg_dump` does not carry roles (§8.34), so the two facts
compose: a fresh-box restore needs the role to pre-exist, and pre-existing means shared.
This does not block the row — a dedicated tenant on its own cluster is the enterprise path
and works as designed — but it means "same cluster, new database" is not equivalent to it,
and the guard says so instead of silently rotating a credential.

**Evidence.** `scripts/fut04_single_tenant.py check` is the one command (exit 0 identical,
1 differences, 2 could not complete — the same vocabulary `scripts/security_scan.py`
uses, and 2 is never a pass). It migrates **two** scratch databases and compares them
with each other, not against the live one: comparing a fresh database to the live one
would prove the opposite of the claim, since the live one carries whatever drift its own
history left. `tests/test_single_tenant_readiness.py` is **19 tests**; backend **833
passed / 2 skipped** (814 baseline, fixed before any edit) plus eval gate **6 passed**,
security scan **clean (9 baselined, 0 open)**. Six mutations, all red: reintroducing the
hardcoded worker DSN (3 tests), comparing policies by name only, removing the floors,
removing the role-guard refusal, dropping the grant columns, and — the one that first
**stayed green** — making `run_check` inventory one database twice. That last mutation is
recorded rather than fixed quietly: the report now carries `compared`, and the test
asserts the two names are distinct and are the two that were migrated.

**Exercised through the real containers, not only in-process.** Migrated
`eva_fut04_live` from inside the `api` container, then ran the `api` **image** against it
with `docker compose run`: register `201`, `/auth/me` `200`, `/bots` `200`,
`/conversations` `200`, `/analytics/summary` `200`, `/health` `200` — no code change, no
branch. Isolation confirmed by reading both databases: the tenant exists in the dedicated
one and its email is **absent** from `eva`. Scratch databases dropped afterwards and
verified gone. `docker compose config` with `POSTGRES_DB=customer_acme` now renders
`customer_acme` on **all three** DSNs (api ×2, worker ×1).

**Not claimed.** This is readiness, not a shipped SKU. There is no per-tenant provisioning
command, no tenant-pinning UI, and no migration tool for moving an existing tenant out of
the shared database; §16.3 asks for none of those and R13 defers the offering. The
readiness claim is narrower and is what the command proves: the same schema is
reproducible from the same code on a database that shares nothing with the shared one.

**CI on `0f80e84`** (run **44302596253**): `test` **green** (steps 6–10, including the
eval gate) and `security` **green** (step 6, dependency scan + SAST); `widget-browser`
**red at step 10 "Browser matrix"**. That red is the **WIDG-11 Escape race**, read off the
annotation rather than assumed: *"Escape closes the panel and focus returns to the
launcher (WIDG-11) … expanded=["false@583","true@584"]"* in chromium — the documented
close-then-reopen-one-tick signature (§8.26/§8.27). The widget subtree is
`7ed153c` at this sha and at its parent, and the commit touches no widget file, so the
gate followed the tree, not the change. The defect is still unfixed and still belongs to
the widget row.

### 8.37 **WP-36 / OPS-12** (2026-11-18, code `36dc0c8`) — the ledger was zero by construction, and the failure that left no trace

The §8.25 RULINGS made this row a prerequisite of OPS-04 rather than part of it, and §8.33
had already measured the two dead signals. Both were real, and both were *product* defects
before they were observability ones: CONV-14's budget stop was unreachable in every
deployment, and `GET /analytics/summary`'s token totals were structurally 0.

**`units` was 0 because the caller said so, not because a counter failed.** `record_usage`
computes `units = (prompt_tokens or 0) + (completion_tokens or 0)` and its only call site
passed `None, None`. The providers had already parsed real counts
(`providers._usage`) — they were dropped one frame above the ledger. That is a different
bug from "no provider integration": everything downstream of the call was correct.

**The counts were never even requested.** The request body had no
`stream_options.include_usage`, so an OpenAI-compatible stream sends no usage frame at
all and the client cannot lose what it never asked for. Measured against the configured
provider (Ollama Cloud, `deepseek-v4.1-flash`): with the option the stream ends in a
`choices: []` frame carrying `usage`, without it nothing arrives. Two consequences that
had to be handled together — the frame has **empty `choices`**, so the existing early
return would have eaten it, and it arrives **after** the last token, so there is no
chunk left to attach it to. A stream has no return value, and a counter on the provider
object is shared state across concurrent turns, so the counts ride a trailing empty-text
chunk.

**Attribution is the provider that answered, not the chain that wrapped it.**
`FallbackChain` is itself an `LlmProvider` whose `key` is `fallback-chain`; stamping that
onto the ledger would give every row the same provider and make a per-provider error rate
meaningless. The chain now stamps the serving link's key onto each forwarded chunk
(`dataclasses.replace`, because chunks are frozen) and the turn loop reads the first
non-empty one.

**One chunk shape change had a bug of its own.** `if not yielded: breaker.record_success()`
in the streaming fallback treated *any* chunk as an answer token, so a provider that
emitted only a usage frame and no text would count as success and **suppress the fallback
that exists for exactly that case**. Guarded on `chunk.text`; the mutation that removes the
guard turns one test red.

**`error_class` is a type name and nothing else.** A provider message is built from up to
300 characters of the response body and can echo the prompt; this table is read by an
alerting poller, not by an incident reader who could judge it. Same rule OPS-02 applied to
spans, and here it is *persisted*, which is the stronger claim. A canary is asserted absent
from the row through the real turn loop, not only from the trace.

**An estimate is labelled as one.** A provider that reports nothing still produces a
non-zero ledger from the repository's own `count_tokens` estimator, and `final.usage`
carries `"reported": true|false` so a consumer charting it can tell a measurement from an
estimate. §17.4 has carried a `usage` key on `final` since WP-11 and had sent `{}` for
every turn until now.

**Measured through Caddy, not only in-process.** After `docker compose up -d --build api`
and migrating the live database to `0007`: a real widget turn on the `Pub` tenant returned
`final.usage {"prompt_tokens": 365, "completion_tokens": 56, "reported": true}`, and the
database holds **one** `provider_outcomes` row (`ollama_cloud`, `success`, the model) and
one `usage_records` row with `units=421`, with `messages.prompt_tokens`/`completion_tokens`
`365`/`56`. The degraded path driven in the container with an `UnconfiguredProvider`
writes `(unconfigured, failure, RuntimeError)` and **no** usage row — the outcome exists on
both branches, which is the whole point of the numerator. Scratch databases and probe rows
dropped and verified gone; the live database is at head.

**Rollback exercised, not assumed.** `0007` was migrated onto a scratch database, then
`downgrade -1` → `upgrade head`, verified at each step (`0006` with the table gone, then
`0007` with it back), and only then applied to the live database.

**15 tests**, 7/7 mutations red: dropping the failure-path row (2 tests), stamping the
chain's own key (1), leaking the message into `error_class` (2), restoring `"usage": {}`
(2), weakening the RLS predicate to `true` (1), letting a usage frame count as `yielded`
(1), and reverting the token threading to `None` (3).

**Not claimed.** The table has no `latency_ms`; §22's latency half is already covered by
`messages.latency_ms` and OPS-02's spans, and duplicating it here would give two sources of
truth that disagree. `usage_records.provider` is populated only for *answered* turns,
because that is what the ledger bills; failed turns are recorded in `provider_outcomes`
only, so OPS-04 must union the two rather than read either alone. **OPS-04 is still not
implemented** — it now has all five signals' *events* (this row plus OPS-09's producer) but
still has no thresholds in code, no poller and no monitored address; the HALT-09 Reading B
ruling supplies the defaults and leaves `ALERT_EMAIL` as the operator's `.env` value.

### 8.38 **WP-37 / OPS-03** (2026-11-18) — metrics from the store, and the exposition defect only a real database showed

OPS-03 asked for *"Prometheus metrics (request rates, SSE stream health, LLM latency/error by
provider, token usage by tenant, queue depth, ingestion failures, DB pool saturation) + light
self-hosted Grafana"* (§22, §9.8). HALT-08 ruled **local-first**: no Prometheus, no Grafana,
no metrics library, with `/metrics` in valid v0.0.4 text exposition as the forward-compatible
contract. Registry row `backlog` → `done`. One new module,
`backend/api/app/observability/metrics.py`; `tests/test_metrics.py` is 32 tests.

**The ruling already named both endpoints, so neither is a deviation.** §8.25 point 2 of the
HALT-08 ruling requires `GET /metrics` in exposition format and point 3 requires
`GET /admin/metrics/summary` as JSON "for the dashboard's existing analytics screen". The
summary route is mounted at `/api/v1/admin/metrics/summary` behind `RequireAdmin` +
`ScopedSession`, so a caller sees only their own tenant. §17's route list does not contain
either path; both are **authorised by the ruling that created the row**, which is a stronger
basis than the additive-route argument SEC-10 used. `/metrics` carries **no** `/api/v1`
prefix and is deliberately absent from `caddy/Caddyfile`: §22's "access control on
observability" and an unauthenticated scrape cannot both hold on the public origin, so the
scrape is container-to-container only. Measured through Caddy: `/metrics` → **404**,
`/api/v1/bots` → 401, i.e. the route exists and is not published.

**The event store is the metric store, as ruled.** `messages` supplies latency
(`latency_ms`), model and role; `provider_outcomes` (OPS-12) supplies per-provider
success/failure; `usage_records` supplies tokens and cost by tenant; `documents` and
`knowledge_sources` supply ingestion status. No exporter, no sidecar, no new dependency:
`redis` is a hard `Requires-Dist` of the pinned `arq==0.28.0` and is already in the image.

**`latency_ms` had no writer, so the latency metric would have been zero by construction —
and the fix is in the turn loop, not in the metrics module.** Measured before the change:
**473** assistant rows, **0** with `latency_ms` set. This is OPS-12's defect class (§8.37)
in a second place, so the WP fixed the producer: `service.py` measures around the
`provider.stream` call and passes the result to `insert_message`. Measured after, through
Caddy on a real widget turn: `messages.latency_ms = 527` and
`eva_llm_latency_p50_seconds{provider="ollama_cloud",tenant_id=…} 0.527`. The measurement is
around the provider stream rather than the whole turn so the number is provider latency, not
retrieval plus rendering, and it is skipped on the degraded path — that branch writes a
canned apology, so its duration is time-to-failure and including it would make an outage look
like a slow provider.

**The parser was written independently and then found to be too lenient — which is the
whole reason the ruling demanded one.** The first version used `dict.setdefault` for
`# HELP`/`# TYPE`, so a duplicate name overwrote silently and reported a clean parse. Against
the **live database** (77 active tenants) the renderer emitted
`eva_llm_latency_p50_seconds` **77 times**, because `tenant_families` returns one family per
tenant and `render` emitted each independently: 545 families for 55 samples, 83 KB. A real
Prometheus rejects a duplicate metric family, so this was a genuine contract violation that
every unit test passed. `render` now merges same-named families into one block, sorts samples
for byte-stable output, and **omits empty families** (a HELP line for a series that does not
exist is noise). The parser now asserts uniqueness of `# HELP`, `# TYPE` and of each
`(name, labels)` series, and a live-scrape test parses the real endpoint strictly. Measured
after: **5 families, 65 lines, 5.3 KB**.

**A second parser bug, found only in the full suite.** Label *values* legitimately contain
braces — the route template `/api/v1/conversations/{conversation_id}` is a label value — and
the parser split the label block on the first `}` regardless of quoting. It passed alone and
failed once another test had recorded a templated route. The scan is now quote-aware, and the
whole label block must round-trip as `name="value"` pairs.

**`scope["route"].path_format` drops the `include_router` prefix** (measured on fastapi
0.163.1: `/api/v1/conversations/<uuid>` reports `/conversations/{conversation_id}`). Labelling
on it raw would merge `/api/v1/widget/x` and `/api/v1/x` into one series — a graph that looks
plausible and is wrong. `route_template` recovers the prefix by aligning the template's
trailing segments, and an unmatched path becomes the fixed label `(unmatched)` rather than
echoing the request path, so no client input reaches a label.

**Queue depth is `ZCARD`, not `LLEN`, and the old measurement proved nothing.** `arq` pushes
with `pipe.zadd(_queue_name, …)` (`arq/connections.py:175`), so the queue is a sorted set.
Measured in the live Redis: `LLEN` on a real zset returns
`WRONGTYPE Operation against a key holding the wrong kind of value`, while `ZCARD` returns the
depth. The `LLEN = 0` recorded in §8.33/§8.37 was reading a **key that did not exist** — Redis
answers 0 for any type on a missing key, so that reading was not evidence of an empty queue.
Unreachable Redis omits the family instead of reporting 0, because "queue empty" and "cannot
see the queue" must not render as the same graph.

**Counters are per process, and that is a stated gap.** §20.1 specifies two uvicorn workers
while the container runs one, so `eva_http_requests_total` and the SSE counters are correct
today and would each report roughly half the traffic under two workers. That is why every
*business* number is computed from the store on request; moving the counters to Redis is the
fix if the worker count changes, and it is named in the module docstring rather than hidden.

**Cardinality is bounded by construction.** Labels are closed vocabularies (method, status,
route template, provider, status) plus `tenant_id`, which is the one unbounded label the
ruling permits and is computed per request rather than stored as a series per tenant. No
conversation id, message id or visitor identifier is a label value. A canary test pushes
synthetic visitor text, name, email and question through the real render path and asserts none
of them, and no conversation id, appears in the scrape.

**Measured through the container, not only in-process.** `docker compose up -d --build api`,
then a scrape from inside `api`: **200**, `content-type: text/plain; version=0.0.4;
charset=utf-8`, 71 lines. A real widget turn (mint session → open conversation → send message)
emitted `event: token` … `event: final` and produced
`eva_sse_streams_started_total 1` / `completed_total 1` / `aborted_total 0`, with the
route-templated HTTP counter showing
`/api/v1/widget/conversations/{conversation_id}/messages{status="200"}`. Probe conversation
rows were deleted afterwards and the deletion verified.

**Mutations: 9/9 red.** Counter naming rule, route-template reconstruction, unmatched-path
bucketing, label escaping, the SSE `completed` flag, the tenant collection loop, the
error-ratio query, the latency producer, and — added this session — the family merge, which
turns **6** tests red. The SSE test was rewritten because the original counted a stream that
raised *after* setting `completed` as an abort, which is a definition the metric does not use:
completion is "a `final` event was emitted", so a visitor who reads the whole answer and then
disconnects is a completion, not an abort.

**CI run 44342240592 on `370cb70` is green on all three jobs** (`test` 1m33s, `security`
23s, `widget-browser` 29m45s). The follow-up docs commit `a0ca7b5` — **Markdown only**, three
files, and widget subtree **byte-identical** (`7ed153c` at both shas) — was green on `test`
and `security` and **red on `widget-browser` alone**, step 10 "Browser matrix", bare
`Process completed with exit code 1` and no per-engine annotation. That is the documented
WIDG-11 Escape race (`§8.26`/`§8.27`), not this change: the same code sha passed that job
minutes earlier. **Not re-run to chase a green** — each push samples the same coin, and the
defect is still unfixed and still owned by the widget row.

**Not claimed.** No Grafana dashboard (the ruling removed it). No Prometheus scrape config, no
`/metrics` publication through Caddy. The counters are single-process. `eva_ingestion_documents`
is current state, not windowed, and the summary route says so in its `window.basis` field.

### 8.39 **WP-38 / OPS-04** (2026-11-18, code `72c408b`) — five alerts, and the two directions a transition rule can fail in

OPS-04 asked for five alerts and §27 asks for *"Alerts firing to a monitored address;
runbook written"*. HALT-09 ruled **Reading B**: the two signals whose events were never
recorded became prerequisites, not excuses — OPS-12's per-turn `provider_outcomes` and real
token ledger, and OPS-09's backup producer. Both landed, so this row is the poller those
rulings implied. Registry row `backlog` → `done`. One module,
`backend/api/app/observability/alerts.py`; `tests/test_alerts.py` is 53 tests. Backend
**935 passed / 3 skipped** (882/3 baseline), eval gate 6 passed, security scan clean
(9 baselined, 0 open). CI run **44369966455 green on all three jobs** (`test`, `security`,
`widget-browser`).

**No new dependency and no new container, because HALT-09 had already measured that
neither was needed.** `redis` is a hard `Requires-Dist` of the pinned `arq==0.28.0` and
`shutil.disk_usage` is stdlib; both are in the worker image. So the poller is a **minute
cron beside the 03:15 purge** (`cron(check_alerts, second=0, unique=True)`), not a compose
service — a container per loop would be a new supply-chain surface for one query a minute.
`unique=True` is what keeps it one evaluation per tick when §20.1's second worker process
is running.

**The two readings that decide whether an alert is real.** The provider error rate takes
*both* halves of its ratio from `provider_outcomes`: `usage_records` only records
**answered** turns, so a ratio built on it has no numerator — the defect class §8.37 found
in the ledger, one table over. And the budget reading calls
`controls.monthly_token_usage`, the same helper CONV-14 stops on, rather than re-querying
`usage_records`: an alert and a hard stop that disagree about how much of the month is spent
would be two definitions of one number, and the alert is the one nobody watches.

**Announcing transitions is what makes a minute poller usable, and it fails in two
directions.** Re-sending an active warning sixty times an hour trains an operator to filter
the channel (§13's noise argument, now on the platform's own mail); *never* re-announcing
means the next real outage is suppressed as a repeat. The second direction was a real bug
in the first draft: a scope that produced **no reading this sweep** stayed in memory
forever, and a fixed provider outage empties its window — so the provider that recovered
would have been permanently "already alerted". `dispatch` now clears any remembered key it
did not see, and a mutation (`for key in []`) turns that test red.

**"Cannot see" is not "nothing there", in an alert rather than a graph.** Unreachable Redis
is a **warn**, with the exception *class* and never its message. §8.38 made this choice for
the metrics exposition (an absent family, never `0`); the alert had the same trap in a
worse form, because a silent poller is indistinguishable from a healthy one. Depth is
`ZCARD` — arq pushes with `zadd`, and `LLEN` on a zset answers `WRONGTYPE`.

**Thresholds are defaults, not policy, and the override path is asymmetric on purpose.**
HALT-09's five signals are `ALERT_*` in `app.config`, so changing one is a `.env` edit. The
two tenant-scoped pairs (provider error rate, budget) also accept `tenants.settings ->
alerts`, the read-only JSONB path `retention.policy` and `handover.policy` already use. Disk,
queue and backup describe the **deployment**, so a tenant cannot move them: one customer
silencing an alert about everyone's disk is not a configuration option. An inverted or
unparseable override **degrades to the operator's defaults** instead of raising — a merged
pair that raised would make every sweep fail for that tenant, which is a way to hide an
alert rather than configure one. `True` is refused explicitly, being an `int` subclass.

**The provider alert has an attempt floor, which HALT-09 did not name and is stated as a
default.** Three turns, two failed, is 67% and would page on a quiet deployment's first bad
minute. `ALERT_PROVIDER_MIN_ATTEMPTS = 10` gates the rate at all; below it the reading is
`ok` and says "below the floor, so no rate is claimed". This is the one number here the
ruling did not fix, and it is a config value like the rest rather than a hidden constant.

**Alert text carries ids, numbers and thresholds — nothing else.** A platform alert leaves
the box by email, so a canary visitor message, the visitor session id and the conversation
id are asserted absent from every subject and body, while the tenant id is asserted
*present* because it is what makes the alert actionable. OPS-01's log guard and OPS-02's
span rule, in a third place.

**`ALERT_EMAIL` is the operator's mailbox, and the test that "proves" it did not.** HALT-09
point 3. With `ALERT_EMAIL` set, *any* fallback is invisible — the first address wins — so
the first version of the test passed against a mutation that fell back to `SMTP_FROM`.
Rewriting it where the fallback would fire (a tenant with a configured handover address, no
operator address, `SMTP_FROM` set) turns both mutations red. Absent the variable, the alert
is logged with its signal and severity and the report records `delivery: "logged"`: an
unset channel must be as visible as a sent alert.

**Measured through the containers, not only in-process.** `docker compose up -d --build api
worker`, then a live sweep in the worker: **80 readings**, `worst=warn`, `backup_failure`
warn because `/var/backups/eva` is empty — the honest state, not a false green. With
`ALERT_EMAIL` and `ALERT_DISK_WARN=0.40` the sink captured two correctly-formed messages
(`[EVA WARN] disk usage at 49% on /`, `[EVA WARN] no backup found`) with values and
thresholds. A seeded probe tenant (3 failures of 4 turns, 950/1000 tokens) produced
`severity: critical` at 75% and a budget warn at 95%, both delivered; probe rows and the
tenant were then deleted and verified gone. The cron fires every minute in the worker log
(`alert sweep: 80 readings, worst=warn, sent=0, recovered=0`).

**`compose.yaml` gains one read-only mount**, `${OPS09_BACKUP_HOST_DIR:-/var/backups/eva}`
into the worker. Without it `backup_reading` would report "destination is not visible"
forever, which is correct behaviour and a useless alert. Verified by rendering
`docker compose config`.

**18/18 mutations red**, after one that did not: restoring a per-state send, re-adding the
frozen-key behaviour, reading `LLEN`'s key, dropping the attempt floor or the window
predicate, `>` instead of `>=` at the budget limit, hard-zeroing the ledger, mtime over the
manifest timestamp, counting the drill manifest as a backup, `ok` for a missing backup,
letting a tenant move the disk threshold, `"sent"` for an absent address, an inverted
default pair — the last fails at import, which is the right place.

**Not claimed.** No real mail provider: `eva-smtp-sink` **captures, never delivers**, so the
evidence proves the channel, not a human receiving it — HALT-09 accepted the sink for
testing and a real SMTP host is an `.env` change. State is **per process**, so a worker
restart re-announces an active alert and restarts the queue sustain window. A backup that
fails **mid-run** leaves no manifest and arrives as *staleness*, not as a distinct failure;
a failure marker is OPS-09's file. **Uptime is not one of the five** (OPS-07's row is now
precise about that), and `/health` remains 404 through Caddy. Alert mail is not durably
stored: the arq job result is the record and it is overwritten each run.

### 8.40 **WP-39 / RAG-11** (2026-11-18, code `9f6322e`) — a bot serves a set, and the pointer that had to stop being one

**RAG-11 is done** (code `9f6322e`). Registry row `backlog` → `done`; `ADMN-05` moved
`blocked (HALT-07)` → `partial (unblocked by RAG-11)`, because the ruling removed the
product blocker but its own Chromium gate has not been re-run. Migration
`0008_knowledge_sets`, `tests/test_knowledge_sets.py` (11 tests). Backend **950 passed /
3 skipped** (935/3 baseline, measured before any edit), eval gate 6 passed, security scan
clean (9 baselined, 0 open). CI run **44389096503** green on **all three** jobs (`test`
1m34s, `security`, `widget-browser`) at `a05b3e2` — the WIDG-11 flake did not fire on this
sample and that defect is still unfixed. The follow-up docs commit `63b415b` (Markdown
only, three files, widget subtree byte-identical `7ed153c`) was green on `test` and
`security` and **red on `widget-browser` alone**, with the check-run annotation naming the
defect outright: *"Escape closes the panel and focus returns to the launcher (WIDG-11) …
Escape from the host page left the panel open (focus=BUTTON:Close chat)"*. Not re-run to
chase a green: that is the same coin landing the other way, and the widget is not this row. Reasoning below; the shape is HALT-07's ruling,
not a design choice of this turn.

**Why the ruling had to be implemented rather than worked around.** §8.22 measured the
defect: with one `active_knowledge_version_id`, publishing a second document made the
first unreachable, and a live shipping question declined at best 0.4367 against a 0.55
floor **while the content sat indexed in the same tenant**. So `/knowledge/publish` was
not additive and the documents table promised a knowledge base the product could not
keep. A per-version `published` flag would not have fixed it: a flag cannot express
*"these five, not those five"*, and a diff of flags has no atomic swap — which is why the
ruling chose snapshot sets (§10.5's single-row pointer swap survives intact, because a
set is pointed at by one column).

**The migration migrates rather than strands, and then removes the second truth.**
`0008` creates both tables, adds `bots.active_knowledge_set_id`
(`ON DELETE SET NULL` — deleting a set must clear the pointer, never delete the bot),
wraps every existing `active_knowledge_version_id` in a one-member set, re-points the bot
at it, and **clears the old column**. The column itself stays because §16.1 lists it and
the ruling migrates rather than drops, but nothing reads it: a test writes an unrelated
version into it and asserts the served scope does not change. Two pointers that can
disagree is precisely the bug being replaced, and the next reader would reintroduce it.

**Publishing is additive at the service layer, not just in the UI.** A publish inherits
the live members, adds the named version and collapses to the newest version per
document. The collapse is a correctness rule, not tidiness: unioning alone would serve
the superseded and the superseding version of the same document side by side, so an
owner who corrects a page would be answered from the text they replaced. `/rollback` is
deliberately **not** additive — one document, one pointer swap, one code path — which is
RAG-09's meaning restated against a set pointer.

**Draft/live still needs no extra state.** A set that is not the bot's pointer is a
draft, and a member still ingesting is filtered by `status IN ('ready','published')`.
ADMN-05's two-column preview therefore keeps its meaning: draft = everything indexed,
live = the pointed set.

**What this intentionally changes for callers.** Publish/rollback now return
`active_knowledge_set_id` + `active_knowledge_version_ids`; the old
`active_knowledge_version_id` is gone from those responses because a set cannot honestly
answer it. `GET /bots[/{id}]` gained the two set fields and **kept**
`active_knowledge_version_id` as a one-version *view* that is null when the set holds
several. The dashboard reads the list for the `Served` column and previews against the
whole set; `app/page.tsx`'s onboarding check moved from "one non-null id" to set
membership, since a bot with five published documents has a null single-version field and
is very much published. This is an API **shape** change on existing paths, written up
rather than assumed free: §17.3 lists the publish route but not its response body.

**The mutation that stayed green and is recorded, not papered over.** Dropping
`AND m.tenant_id = :tenant_id` from the membership query leaves the suite green — RLS
refuses the row first. Same finding as WP-19, WP-23 and WP-34. The predicate is defence
in depth, **not the guard**. The isolation test now asserts the policy in both
directions *as `eva_app`*: the foreign set is invisible **and** the tenant's own set is
visible. The first half alone is vacuous — a table with RLS forced and no policy denies
everything, so deleting the policy kept the test green until the positive assertion
existed. Reading through the `eva` admin role would have proved nothing at all, since it
is a superuser with BYPASSRLS.

**`knowledge_set_members.tenant_id` is not in the ruling's column list, and is added.**
Every tenant-owned table in §16.1 carries one and is fail-closed under RLS (§16.3). A
member table scoped only by `set_id` would be the single tenant table in this schema with
no policy of its own, and a lost predicate there is a query that *succeeds and returns
another tenant's knowledge*.

**Verified on a scratch database, not only in-process.** `upgrade 0007` → seed a bot with
`active_knowledge_version_id` and a second bot with nothing → `upgrade head`: the first is
served from a one-member set with the old column cleared, the second stays unpublished →
`downgrade 0007`: the one-member set restores the old pointer, both tables and the new
column are gone → `upgrade head` again. Multi-document sets are deliberately left NULL by
the downgrade: silently serving one of five documents is the defect this row removed.

**Not claimed**: no live Chromium pass over the multi-document case (ADMN-05 owns that
screen's gate); no UI for composing a set by hand — sets are created by publish and
rollback, which is what the ruling asked for. `TABLES` in `app/compliance/facts.py` gained
both tables and the DPA was regenerated, so the data inventory still cannot drift from
the schema.

## 9. Environment Resolution Record

2026-11-15: user ruled **Netcup VM is the OpenHands platform for this project's development and prototyping — not Oracle**. Verified and fixed same day: gateway `Host <build-vm>` alias (<netcup build vm>, key `<operator key path removed>`) already worked; box had canvas + wrappers but lacked the `openhands` CLI and its wrapper read the masked legacy settings file → installed CLI via `uv tool install openhands@1.16.0` (parity with Oracle) and patched `~/bin/talos-openhands.sh` to the proven live-settings scheme (backups: `.bak-20261115`, `.bak2-20261115`). Smoke run passed. Oracle VM untouched (its 98%-full disk no longer gates this project).

## 10. Rulings Status

1. **Build VM — RESOLVED:** Netcup (user, 2026-11-15). Oracle disk blocker void.
2. **Embeddings — defaulting:** self-host BGE-M3 (capacity now confirmed ample: 14 Gi RAM / 8 cores; free, data local). Managed-API override possible until WP-06 starts.
3. **Codename — RESOLVED:** **EVA** (user, 2026-11-15). Repo dir `~/work/eva`; product name "EVA".
4. **Go — OPEN:** awaiting explicit go to start Sprint 0 (WP-00: repo init, docs bundle, FEATURES.md registry, G1 checksum gate, CI skeleton) on <build-vm> at `~/work/eva`.

### 8.41 **WP-40 / SEC-15** (2026-11-18) — a domain is verified by a record, not by a claim

**SEC-15 is done** (`771ae2a`). Registry row `backlog` → `done`.
`backend/api/app/widget/domains.py` (the resolver), `backend/api/app/botconfig/domains_router.py`
(the routes), `tests/test_domain_verification.py` (41 tests). No schema change, no migration,
no new dependency.

**The row existed because the widget could not serve anybody.** `bot_domains` had no writer
at all: every tenant registered since the schema was created had zero rows, so
`widget/router.py::_allowed_domains` returned an empty list, `POST /widget/session` answered
403 for every origin, and the snippet the dashboard hands out could not work. That is what
HALT-06 recorded, and option 2 was ruled on 2026-11-17.

**Add and verify are two endpoints, and that is the whole point.** §17.2 describes one route,
"Add + verify allowed domain", which hides two operations with different trust. Adding is the
owner's claim: it stores the row with `verified = false` and a fresh 128-bit token. Verifying
is proof of control: an explicit action that looks the token up and only then flips the flag.
Option 1 of the ruling ("self-assert on add") is the cheap reading that makes `verified`
decorative, and a combined route is how it would come back. Nothing verifies on add, on a
timer, or as a side effect of a read — an owner who adds a domain they do not control must not
be able to make the widget serve on it, and "the record is there now" only means something at
the moment someone asks.

**The test that matters asserts the widget, not the row.** Every other isolation test in this
repo seeds the happy path and proves the code path it already takes (AGENTS.md's `verified`
lesson, which cost a cycle in WP-16). So a domain's `verified` flag is never the assertion on
its own: the origin is driven through `POST /widget/session` — the endpoint the loader calls —
and is 403 `no_allowed_domains` after the add, 200 after the verify, and 403 again after the
row is deleted. If `_allowed_domains` ever stops filtering, that test fails; a test on the row
would not.

**Why a hand-written resolver rather than a dependency.** `dnspython` is not in the api image:
`import dns` fails there, `dig`/`nslookup`/`host` are absent, and the only mention of it in the
whole installed closure is a `psycopg` *dev* extra that does not ship. Adding a runtime
dependency is a G3.1 halt, and the alternative — shelling out to a tool that is not in the
image — is not an alternative. So `domains.py` speaks DNS itself: `socket`, `struct`, one
question, one answer type. It is about a hundred lines, which is the cost of not adding a
supply chain for one record type, and it is tested against a **real UDP server** rather than a
mocked one. A stub at `query_txt` would have proved nothing about the packet builder, the
length-prefixed TXT reader, or the compression-pointer guard — which are exactly the parts
that break.

**The defect class this row is most likely to hide: reading "could not ask" as "no record".**
A naive parser returns `[]` for a truncated reply, and the owner is then told their DNS is
wrong while the record sits there correctly. §8.38 recorded the same confusion on a metrics
graph; here it sends a paying customer to edit a working record. So truncation (TC),
SERVFAIL, a mismatched query id, a forward-pointing compression pointer, an unreachable
resolver and a silent one each raise `lookup_failed`, and the route maps that to **503**.
NXDOMAIN and NODATA are different, and honest: an empty list → **200** with
`verified: false` and the exact name and value to publish. Four of the twelve mutations
attack this boundary and all four go red.

**The SSRF guard is on the name, because the name is tenant-controlled.** We never connect to
the tenant's host — that is why TXT was chosen over `/.well-known/`, which would have put a
server-side fetch of a tenant-chosen URL into §21.2's class, the one `handover/webhooks.py`
exists for. What is left is an *existence oracle*: a tenant could ask our resolver whether
`db.internal` exists. IPs (no record to publish), single-label hosts (no public record) and
the special-use and internal suffixes — `.local`, `.localhost`, `.localdomain`, `.internal`,
`.intranet`, `.lan`, `.home`, `.corp`, `.arpa`, `.onion` — are refused before a packet is
built. `.test`/`.example`/`.invalid` are deliberately allowed: RFC 2606 guarantees they never
name an internal service, so querying them reveals nothing.

**One mutation stayed green, and it was a real gap, not a formality.** Dropping
`AND bot_id = :bot_id` from the listing left all 40 tests passing. RLS is scoped by *tenant*,
and the MVP creates exactly one bot per tenant, so cross-bot leakage is invisible to every
other test — the query is still correct for the only bot that exists. §12.3's rule is that a
bot's domains belong to that bot, so the test now seeds a **second bot in the same tenant**
and asserts it sees nothing and cannot verify or delete the first bot's row. That is the
difference between "one bot happens to work" and "the predicate is there". 12/12 red
afterwards.

**Another bug the tests found: one bad legacy row 500'd the whole listing.** The dev seed
inserts `http://127.0.0.1:8099` straight into `bot_domains` (AGENTS.md's HALT-06 workaround),
and `record_name` refuses an IP by design. `_summary` called it unguarded, so every listing on
that bot raised. Naming the record is a display concern: the row is now listed with
`record_name: ""` and the other domains still appear.

**Audited in both directions, without the token.** `domain.added`, `domain.verified`,
`domain.verify_failed`, `domain.removed`. The token is kept out of `detail` — it is public by
design (it goes in a public TXT record, and anyone who can read the tenant's DNS can read it),
but `audit_logs` is append-only and never purged, so a value written there can never be
withdrawn (SEC-10's trap). A failed verification is its own action rather than folded into the
success, because a claim someone could not prove is the interesting case.

**Verified live through Caddy, not only in process.** The suite calls the ASGI app directly,
so it cannot see proxy or container wiring (AGENTS.md). On the running stack: register → add
`live.example.com` (`verified: false`, `record_name: _eva-verify.live.example.com`) → widget
403 → a fixture resolver **container on `eva_default`** answering `eva-verify=<token>` (a
host-side listener is unreachable — the sandbox firewall blocks container→host, the same
lesson M4 recorded) → verify 200 `the record matched` → widget 200 on that origin → **403 on
`https://evil.example.com`**, so verification admits one origin and not the internet. Probe
rows and the fixture container were removed and `.env` restored to its prior contents.

**Not claimed.** No UI: the settings screen and the onboarding checklist used to say no
mechanism had been chosen, which this change makes false, so both were corrected to describe
the TXT mechanism and to say the control is still missing — the add/verify screen is ADMN-10's
remaining work, not this row's. No IDN: a Unicode name is refused with "publish the punycode
form" rather than guessed, because we cannot know which name the owner meant. No CAA, no
re-verification sweep, no wildcard handling.

**Deviations recorded, not assumed free.** §17.5 lists no `/domains/{id}/verify` and no
`DELETE /domains/{id}`. Both add a path under an existing resource with no schema or shape
change — the same additive class as SEC-10's `DELETE /conversations/{id}`, written up rather
than treated as free. `EVA_DNS_RESOLVER` is a new config key with an empty default meaning
"first nameserver in `/etc/resolv.conf`"; it exists so a deployment with an internal resolver
can name one, and so the tests can too.

**CI on `771ae2a` (run 44415254889): `test` success, `security` success, `widget-browser`
failure at step 10 "Browser matrix"** — and the annotation names the defect, not this change:
*"Escape from the host page left the panel open (focus=BUTTON:Close chat, ...,
expanded=["false@594","true@595"])"*, i.e. WIDG-11 closing and reopening one tick later, the
§8.26/8.27 race. The widget subtree is **byte-identical at `7ed153c`** on `771ae2a` and its
parent `0a831eb`, `git diff -- backend/api/widget` is empty, and this WP's diff touches no
widget file — a Markdown-plus-Python diff cannot produce that assertion. Recorded rather than
re-run to chase a green: each further push only samples the same coin.

**Then `d1effed` (run 44420954115) is green on all three jobs** — `test`, `security` *and*
`widget-browser`. That sha differs from `771ae2a` only in documentation and the removal of one
unused test helper: `git diff --stat 771ae2a d1effed -- backend/` is **empty** and the widget
subtree is `7ed153c` on both. So the same backend code passed the matrix on the next sample,
which is what a ~50% flake looks like — it is **not** evidence that WIDG-11 is fixed.

### 8.42 **WP-41 / SEC-16** (2026-11-18) — the purge leaves a receipt, because arq drops the report

SEC-09 built the enforcement: a per-tenant window and a daily cron that deletes finished
conversations as the RLS-subject app role. What it did **not** build is the thing §27 asks
for in the same breath — that the purge be *verified* to delete what it claims. The job
returned a dict, and arq keeps a job result for `keep_result` seconds, measured here as
**3600**. So "what did the purge remove last Tuesday" had no answer from the job itself:
the only copy of the evidence expired an hour after it was produced, and a run that
deleted nothing looked exactly like a run that never happened.

**The receipt is one `retention.purged` audit row per tenant per run, written on the
delete's own transaction.** Not a new table and not a new dependency: `audit_logs` is the
existing append-only, RLS-forced, never-purged store, and `Action` is the existing closed
vocabulary. Two properties follow from riding the same transaction, and both are tested:
a crash between the delete and the commit leaves the conversations *and* no receipt, and a
receipt cannot outlive a deletion it did not perform. The alternative — commit, then
record — passes a happy-path test and lies after any failure in between.

**A run that deletes nothing still writes a row.** This is the point of the report rather
than a side effect: a quiet day and a dead cron both delete zero, and only "this tenant was
examined, 0 conversations past its window" distinguishes them. The cost is stated: ≤50
tenants at one row a day, a bounded trickle that is itself the evidence a review asks for.
The row carries `deleted`, all five child counts, the window and the eligible statuses, so
it answers "what went" and "under what rule" without a second query.

**The receipt carries counts, never the data it counts.** `audit_logs` is append-only and
never purged, so a receipt naming the visitor's session id, address or message text would
deposit it in the one table nothing can delete — SEC-10's erasure trap, in the place where
a purge is most tempted to quote its own work. A canary test seeds every identifier and
searches the whole row for it; the mutation that puts them back is red.

**Counted before the delete, in the same statement set.** After the cascade the children
are gone, and "we deleted something" is not evidence — an account of what went is. The
implementation counts first, deletes, and logs an `error` if the two disagree, because a
mismatch means something else is deleting concurrently and the receipt would otherwise
describe an event that did not happen. Counting after the delete leaves the receipt
permanently zero; that mutation is red.

**Two findings that were investigated and deliberately not "fixed".**

*The 765 `active` conversations are not a purge defect.* `_close_idle` runs only on a
subsequent read, and there is no end-conversation endpoint, so an abandoned conversation
stays `active` for ever. But §19.3 and PLAN §8.26 make excluding `active` **policy** —
erasure takes live conversations, retention does not, because a purge deletes without being
asked — and the live data confirms there is nothing to reclaim: **0** `active` rows are
past 90 days (the oldest is two days old, from this deployment's own start). Widening
`ELIGIBLE_STATUSES` would silently make the purge able to delete a conversation someone is
still having. It stays as it is.

*`arq` result retention was not raised.* A longer `keep_result` would have been a one-line
alternative, and it is the wrong one: Redis is not the record of a compliance control, it
evicts, and the API cannot read a job result for a tenant owner. The durable row is the
report; the return value remains useful for the operator who just ran the job by hand.

**Verified live through Caddy, not only in-process.** Rebuilt `api` and `worker`, then ran
the real job inside the worker container against the dev database. A probe tenant with a
2-day window and a 10-day-old `closed` conversation holding a message, a rating, a handover
and a visitor file: the job reported `deleted: 1`, psql showed the conversation and all four
children gone, and the receipt read
`{"deleted": 1, "conversations": 1, "messages": 1, "feedback": 0, "handovers": 0, "visitor_files": 1, "retention_days": 2, ...}`.
A second tenant registered through the API then read its own zero-deletion receipt at
`GET /api/v1/audit-logs` with a **200** as the owner — so the evidence is reachable by the
person it belongs to, through the public origin, not only by psql. A canary sweep over every
receipt found **0** occurrences of the seeded session id, name, address, page URL or message
body. Probe tenants and their rows were removed; 81 pre-existing tenants were untouched.

**A constraint the tests hit before production did.** `audit_logs.tenant_id` is
`ON DELETE RESTRICT`, so the new receipts block a tenant delete. No production path deletes
a tenant, but three existing test teardowns did — they now drop receipts first, the way
`test_erasure.py` already had to. Recorded rather than relaxed: the RESTRICT is what stops
a tenant's audit trail from vanishing with it.

**The compliance inventory would have gone stale and was regenerated.** The DPA's
`audit_logs` row enumerated the actions it holds; a new action makes that list incomplete,
so it now names scheduled purges and states the counts-only rule for both writers.
`app.compliance.render` was re-run, and the document tests pass against the code.

**9 mutations red**: drop the receipt (6 tests), leak the identifiers (1), count after the
delete (1), commit before recording (1), skip the receipt for later tenants (7). Backend
**997 passed / 3 skipped** (991/3 baseline, measured before any edit), eval gate 6 passed,
security scan clean (9 baselined, 0 open). **No schema change, no migration, no new
dependency, no new container.**

**Not claimed**: no dashboard surface for the receipts (they are readable at
`GET /api/v1/audit-logs`, admin-only, which is ADMN-10's screen to own); no alert on a
receipt that stops appearing (that is OPS-07's uptime half); and the drill proves the
receipt is durable in the live database, not that a backup restores it — OPS-09 owns that.

**CI on `664a35d` (run 44428311517): green on all three jobs** — `test`,
`security` *and* `widget-browser`. The widget subtree is byte-identical `7ed153c`
here and at its parent `565b3ba`, so the matrix passing is a sample where the WIDG-11
Escape flake did not fire — **not** evidence it is fixed; that defect is still unfixed and
still owned by the widget row.

**The docs commit `927e6e9` was red on `widget-browser` alone** — `test` and `security`
success, step 10 "Browser matrix" `failure` with an **empty** check-run annotation (bare
exit 1, no per-engine title). `git diff --stat 664a35d 927e6e9` is two Markdown files, the
code diff under `backend/`, `dashboard/` and `scripts/` is empty, and the widget subtree is
`7ed153c` at both. A Markdown-only diff failing that job is the §8.26/8.27 flake, whose rate
is about half; it is not this change. Not re-run to chase a green.

### 8.43 **WP-42 / ADMN-13** (2026-11-19) — a browser walk that had to be able to say no

**ADMN-13 is done** (code `10641ca`). Registry row `backlog` → `done`. `scripts/admn13_walk.py` drives
`scripts/admn13_driver.mjs` (real Chromium against the live compose stack through Caddy) and
`tests/test_admn13_walk.py` holds the verdict's 12 tests. Backend **1009 passed / 3 skipped**
(997/3 baseline, measured before any edit), eval gate 6 passed, security scan clean
(9 baselined, 0 open). No source change, no migration, no new dependency, no container.

**Why this row exists as its own WP.** RAG-11 (`9f6322e`) replaced the served pointer with a
knowledge set but recorded, honestly, that *"no live Chromium pass over the multi-document
case"* had been done — ADMN-05's own browser gate was verified against the single-pointer
model it superseded. The in-process suite asserts retrieval over a set and structurally
cannot see the screen; ADMN-05's evidence was therefore evidence about a shape that no
longer ships.

**The walk, and the control that makes it evidence.** Two Q&As are added through the screen,
then the test box is asked the same question at four states: nothing published (the live
column says *"Nothing is published yet"*, the draft knows document B), A published (live
finds A and **not** B), both published (live finds both), rollback to A (live finds A and not
B again). The third state alone is worthless — a preview that ignored its scope and searched
everything passes it — so the second state is the discriminating clause and the fourth
proves rollback is not a no-op. §8.22's defect, read off the screen: under one pointer,
publishing B would have made A unreachable, and the "both published" assertion is what goes
red.

**The `Served` column is read after a reload.** Clicking "Make live" sets local notice state;
the badge that says `live` must come from `GET /bots`, or the walk would be asserting on its
own click. `rows_after_publish_2` is read from a reloaded page.

**Rollback is driven through the API, and that limit is stated in three places.** The screen
has no rollback control — ADMN-05's outstanding gap — and adding one would be a new screen
control, i.e. a scope change for a verification row. So the driver calls
`POST /knowledge/rollback` from inside the same authenticated browser session and then reads
the *effect* off the screen (`admn13_walk.py`'s docstring, its pass line, and the registry
row). A test pins that publish is a click and rollback is the only API call, so the walk
cannot quietly degrade into an API test.

**The verdict is a pure function, and that is what makes it checkable in CI.** `clauses()`
takes the driver's JSON report and returns `(name, bool)` pairs. The browser half needs a
running stack, so CI cannot run it; the half that decides pass/fail can. `report()` in the
test file is the report the driver actually printed on a passing walk, and every clause has
one mutation that makes it fail — asserted as a set equality, so a new clause without a
mutation is a red test rather than an unfalsifiable one. §8.25 and §8.32 both record a gate
that passed because nothing it inspected could fail.

**Four mutations run, all red**: weakening the served count to `>= 1`; making the "serves A,
not B" clause scope-blind; replacing the "Make live" click with a publish POST; and dropping
the reloads so the badge is read from click state. The second one is the important one — it
leaves "both documents answer" green, which is precisely how this row could have shipped as
proof of nothing.

**Not claimed.** No rollback button (ADMN-05/ADMN-10 territory, and a scope change if taken
here). No widget conversation in this walk — M5's gate already drives the visitor path and
this row is about the knowledge screen. No crawl-sourced second document: two FAQs are two
documents, and adding a crawl would have made the walk depend on an outbound fetch for the
same assertion.

**The live walk, measured, through Caddy on the compose stack.** `python scripts/admn13_walk.py`
printed all seven clauses PASS with these observations: `documents: 2 document row(s) after two
Q&As`; `draft only: draft finds B: true; live finds B: false`; `A only: live finds A: true; live
finds B: false`; `both published: live finds A: true; live finds B: true`; `rollback to A: live
finds A: true; live finds B: false`. `served_counts` read `{before_publish: 0,
after_publish_2: 2, after_rollback: 1}` from the **reloaded** table, and the publish notice
read *"“Do you ship to the EU?” is live. The bot now answers from 2 documents."* The dashboard
container had to be rebuilt first: the running image predated the RAG-11 screen, which is
itself the small reminder that a `next build` artifact is not the source tree. Four probe
tenants created by iterations of the walk were deleted afterwards (their `audit_logs` rows
removed with the append-only trigger disabled for the delete only, the same way
`tests/test_erasure.py` does it) and `SELECT count(*) FROM users WHERE email LIKE
'admn13-%@example.test'` is now `0`.

**CI on `a1eb86f` (run 44493989918): `test` success, `security` success, `widget-browser`
failure at step 10 "Browser matrix".** The check-run annotation names the defect outright —
*"chromium failed (exit 1): ✖ Escape closes the panel and focus returns to the launcher
(WIDG-11) … expanded=["false@590","true@591"]"* — i.e. the §8.26/8.27 close-then-reopen race at
its recorded ~50% rate. The widget subtree is **byte-identical `7ed153c`** at `a1eb86f` and at
its parent `fe2ff30` (`git rev-parse a1eb86f:backend/api/widget` = `fe2ff30:backend/api/widget`),
and the whole diff is `docs/`, `scripts/` and `tests/` — `git diff --stat fe2ff30..a1eb86f --
backend/api/widget dashboard` is **empty**. A Markdown-Python-JS-scripts diff cannot produce
that assertion. Recorded rather than re-pushed to chase a green, per the charter: each further
push only samples the same coin, and WIDG-11 is still unfixed and still owned by the widget row.
The `widget-browser` job also stalled 30 minutes in "Install browsers" before the matrix ran —
a download stall on the runner, worth knowing before reading a long run time as a slow suite.

**Then `613c9b2` (run 44501851511) is green on all three jobs** — `test`, `security` *and*
`widget-browser`. So is `2796a4d` (run 44496008442), a descendant carrying the same code.
That is the same coin landing the other way, **not** evidence WIDG-11 is fixed: nothing
between `a1eb86f` and `613c9b2` touches the widget, the subtree is `7ed153c` on all three,
and the two shas differ only in Markdown. Record both readings or the file starts claiming a
fix it does not have.

### 8.44 **WP-43 / OPS-05** (2026-11-19, code `0f7be76`) — a gap is a declined turn, and the threshold a live stack corrected

**OPS-05 is done.** Registry row `backlog` → `done`; `ADMN-08`'s misnamed field corrected in
the same commit. `backend/api/app/analytics/gaps.py`, `tests/test_knowledge_gaps.py` (20
tests). Backend **1029 passed / 3 skipped** (1009/3 baseline, measured before any edit), eval
gate 6 passed, security scan clean (9 baselined, 0 open).

**The field was named after something it did not measure, and that was the defect.**
`top_unanswered_questions` ranked visitor messages by exact-text count. The most-asked
question in a support chat is usually the *best-answered* one, so the field's own name was
the lie: it was a popularity list. There are now two answers, on purpose — a frequency list
called `top_questions`, and `GET /api/v1/analytics/knowledge-gaps` for the declines. Folding
them back into one field is the mistake to not repeat.

**A gap is `confidence < bots.confidence_threshold`, read from the column the turn loop
already writes.** `conversation/service.py` persists `result.best_score` as the assistant
message's `confidence` on every retrieval path, and the pipeline declines exactly when that
score is below the floor, so no new column, no flag, no schema change and no migration. The
`confidence IS NULL` paths — injection refusal, handover offer, provider outage — are
*excluded* rather than counted: they are not declined retrieval, and counting them would
report a product's safety refusals as content gaps. A test seeds all three and asserts none
appears.

**The live stack corrected the threshold, and that is the finding worth keeping.** The first
draft used `CLUSTER_SIMILARITY = 0.62`, justified by a probe of four paraphrase pairs. Driving
it through Caddy against the real embed service with five declined questions produced a
**false merge**: "do you offer a warranty" collapsed into "what is your refund policy". A
wider probe (11 questions, 15 pairs) explained it — BGE-M3 puts short support questions in a
narrow band:

| | measured cosine |
|---|---|
| same topic, must merge | 0.6942 0.7300 0.7406 0.7658 0.7710 0.8028 |
| different topic, must not | 0.4315 0.5302 0.5303 0.5456 0.5463 0.5893 0.6244 0.6304 **0.6736** |

0.62 sat *inside* the different-topic range. The constant is now **0.68**, between the highest
different-topic pair and the lowest same-topic pair, and two regression tests pin both sides
(`> 0.6736`, `< 0.6942`) so a future edit cannot quietly slide back. The re-run through Caddy
then returned three gaps: refund (2 phrasings), shipping (2 phrasings), warranty (1) — and
the 3×-asked answered question absent.

**The margin is ~0.02 and the module says so rather than implying safety.** "what is your
refund policy" vs "do you offer a warranty" (0.6304) is *closer* than a genuine paraphrase
pair (0.7658). Expect adjacent-topic merges and same-topic splits on real traffic; the label
is illustrative, the counts and the phrasings list are what an owner acts on. A tighter
embedding of a longer passage would widen the band; a short question does not carry enough
signal.

**"Could not cluster" is not "no gaps"** — §8.38/§8.41's rule, on an analytics field this
time. An embedder that raises returns `clustered: false` **with the turns still listed**
unmerged and a note saying so; returning `[]` would tell an owner their knowledge base is
complete at the exact moment the system cannot tell. The empty-tenant path never calls the
embedder at all, because the API image has no torch and a new tenant must not 500.

**Ranking is frequency decayed by recency**, `0.5 ** (age_days / 7)`, summed per cluster — a
topic asked twice this week outranks one asked five times a month ago, which is the row's
requirement. The half-life travels in the payload with the threshold, because a ranking whose
rule is invisible is one the owner cannot disagree with.

**Clustering is greedy leader clustering, not k-means.** *k* is the thing the owner is trying
to learn, so choosing it first answers the question with its own assumption. Single-pass and
deterministic: two identical windows cannot render two different lists (asserted).

**"Same text" is equality, not similarity.** The unclustered path groups identical wordings
and merges nothing else — a bug found while testing, where it made five occurrences of one
wording read as five gaps.

**Mutation-checked, 13 mutations, 13 red** — the two that stayed green on the first pass are
recorded because both were real:

1. *Dropping `AND m.tenant_id = :tenant_id`* leaves the suite green: **RLS refuses the row
   first** (same finding as WP-19, WP-23, WP-34). The predicate is defence in depth, not the
   guard. Rather than cite the isolation test as proof, a separate test now asserts the
   policy and `FORCE ROW LEVEL SECURITY` exist on all three tables the query reads **and**
   that the owning scope sees its rows — because a forced-RLS table with no policy denies
   *everything*, which would also make the isolation test pass while the feature returned
   nothing to anyone.
2. *Ignoring `:since`* stayed green because the window test aged only the assistant replies,
   leaving the `LATERAL` join with no visitor message to pair. The fixture now ages every
   message and asserts a wide window still finds them, so the test cannot pass for the wrong
   reason.

**Two false claims on that same screen were found and are NOT fixed here.** The browser
screenshot that verified the empty state also showed **"Answered turn rate 100.0%"** beside
the gap counters while 5 of 8 turns were declined, and the live database confirms it is not a
seed artifact: **384 of 473** assistant turns are below the default floor while
`answered_turn_rate` reports ~98%. Cause: `counts.answered` is
`role='assistant' AND confidence IS NOT NULL`, and the decline path writes
`confidence=result.best_score` — a real number — so a decline counts as an answer, and the
field's own definition ("a declined turn records no score") is false. That is ADMN-08's
pre-existing defect, not OPS-05's, there is no registry row for it, and fixing it changes a
number another row already reports as done — so it is reported, not absorbed. **Note for
whoever takes it: `test_conversation_archive.py:510` asserts the current arithmetic
(`answered / assistant`), so that test would have to change with the fix, and the correct
predicate needs the bot's floor, which that query does not join.**

**API shape**: one new path under an existing resource, no schema change, no request/response
change to any existing field except the rename `top_unanswered_questions` → `top_questions`
in `/summary`. The rename is a deliberate break of a shipped field (ADMN-08, `WP-19`) —
written up rather than assumed free: the only consumer in this repo is the dashboard, updated
here, and the field was explicitly marked provisional in §8.21.

**Verified live through Caddy, not only in-process**: `api`+`dashboard` rebuilt; empty tenant
200 with `gaps: []`; seeded tenant → 3 clusters with correct counts; screenshot of
`/analytics` showing both sections with the misnamed one gone. Probe tenants deleted, verified
gone; `.env` untouched.

**CI on `0f7be76` (run 44518767004): `test` success, `security` success, `widget-browser`
failure at step 8 "Install browsers".** That is *not* the WIDG-11 signature — the matrix never
ran (steps 9 and 10 skipped). The annotation is `apt-get` fetching from
`security.ubuntu.com` returning **503 Service Unavailable** for six packages and
`playwright install failed for chromium after 3 attempts`: an upstream mirror outage on the
runner, not this change. `test` (which builds and tests the widget, steps 6-9) and `security`
both passed, and the widget subtree is byte-identical `7ed153c` at `0f7be76` and `cadf91f`.

**The docs commit `3f9e485` (run 44522580462) settled it: green on all three jobs**, including
`widget-browser` with steps 8 "Install browsers" and 10 "Browser matrix" both succeeding, on a
Markdown-only diff over the same widget subtree. That is the same mirror working on the next
attempt, so the `0f7be76` failure was the outage and **not** this change — and it is *not*
evidence about WIDG-11 either, which is still unfixed. **When `widget-browser` is red, read the
failing step before suspecting the diff**: the flake lives in step 10 and its annotation names
WIDG-11; a step-8 failure with `apt-get` 503s is infrastructure.

**Not claimed**: no per-gap drill-down, no "mark as answered/dismissed" state, no write path
(a gap list is read-only), no cross-tenant trending, and no re-clustering on a schedule — the
list is computed per request and bounded (`MAX_GAP_TURNS` 500, `MAX_CLUSTER_QUESTIONS` 120)
so a bad week cannot make the analytics screen slow. Embedding 120 questions measured at
~12 s for 60 texts against the live service, so a full window is a few seconds and is why
this is a separate route rather than a field on `/summary`.

**M5 after this row**: `ADMN-05` (its own Chromium gate) and `ADMN-10`'s domain control;
`ADMN-01`/`ADMN-02` remain partial. M6 rows are unchanged by this WP.

### 8.45 **WP-44 / ADMN-14** (2026-11-19) — a screen that printed a number it had never read

**ADMN-14 is done.** Registry row `backlog` → `done`. `dashboard/app/settings/page.tsx`,
`dashboard/lib/api.ts`, `tests/test_settings_copy.py` (13 tests). Backend **1042 passed / 3
skipped** (1029/3 baseline, measured before any edit), eval gate 6 passed, security scan
clean (9 baselined, 0 open). Dashboard `tsc --noEmit` clean, `npm test` 8 passed, `next
build` 9 routes. **No schema change, no migration, no new dependency, no server code
change** — this row is copy plus the one type that made the copy unreadable.

**The row's own premise had already been half-fixed, and finding that changed the work.**
Its text says the screen "still tells the owner that retention and domains are *not
shipped, deliberately*". Those sentences were removed by SEC-10 (`1ba051f`, retention) and
SEC-15 (`771ae2a`, domains), so the literal complaint was gone. What was still wrong was
worse than stale prose and survived both edits: the retention card said **"90 days unless
you change it"**, a hardcoded default, while `dashboard/lib/api.ts` declared
`TenantSettings = { handover: HandoverSettings }` — the `retention` block was **dropped by
the client type**, so the screen structurally could not read the number it was describing.
A copy fix alone would have made the sentence more accurate and still false for every
tenant that had changed their window. **The type was the defect; the sentence was the
symptom.**

**Measured on the live stack, because a settings card is a screen.** Registered a tenant
through Caddy, read the card (rendered **90 days**), PATCHed `retention.conversation_days`
to 7 from inside the authenticated session, reloaded: the card reads **7 days**. The old
copy printed 90 in both states. Chromium, no page errors; the probe tenant was removed and
verified gone (2 audit rows deleted with the trigger disabled, per the `test_bot_config` /
`test_conversation_archive` teardown pattern, then the tenant).

**The eligibility sentence was the second false claim, not a nicety.** The card said
conversations are purged by age. `worker/purge_job.py` has `ELIGIBLE_STATUSES = ("closed",
"handed_over")` and ages on `last_activity_at`, so an `active` conversation is never purged
however old it is — §8.42 records that as deliberate policy with 0 live rows affected.
An owner reading "old conversations are deleted" would answer a data subject wrongly, which
is the same class of harm as the original row. The card now states the rule.

**The domains card keeps pointing at the API on purpose.** SEC-15 built add + verify, and
ADMN-10 still owns the control, so the card describes the mechanism (a TXT record at
`_eva-verify.<domain>` whose value is checked by an explicit verify) and says plainly that
adding alone allows nothing — the distinction HALT-06's option 2 exists for. Saying "no
control here, the API is at `/api/v1/bots/{id}/domains`" is honest; offering a form would
be ADMN-10's row and a dead control besides.

**13 mutations, 13 red — after two stayed green and were real test defects.** The first
draft of the stale-phrase guard compared case-sensitively and let a re-introduced "Not
configurable yet." through capitalised; the API-path guard asserted `/api/v1/bots/` and
`/domains` as two separate checks, so a mutation to `/api/v1/bots/domains` — a path with no
bot id, i.e. not a route — stayed green. Both are the §8.25/8.30 lesson in a new place:
a guard written from the intended wording rather than the failure it must catch passes on
its own author's phrasing.

**A third mutation stayed green and is recorded rather than papered over.** Two guards
initially checked the whole card for a phrase that a *different sentence* in the same card
supplied: `"through the API"` (both the erasure sentence and the window sentence have it)
and `"schedule"` (the card opens "deleted on a schedule"). Both now bind to the sentence
that carries the claim. Only after that did the erasure mutation go red. **Deleting a
cross-reference** (the "exporting is available from the Conversations screen" pointer) is
still green, deliberately: it is a helpfulness loss, not a false claim, and a test that
fails on it would be asserting the prose's shape rather than its truth.

**Not claimed**: no control to change the window and no domain control (both ADMN-10's),
no browser gate for the whole `/settings` screen (ADMN-10 owns that too — this row drove
the two cards it changed), and the copy is now correct for the behaviour as shipped, not a
statement that the retention policy itself is right.

**M5 after this row**: `ADMN-05` (its own Chromium gate) and `ADMN-10`'s domain control;
`ADMN-01`/`ADMN-02` remain partial. `ADMN-14` was the last unclaimed M5 row. M6 rows are
unchanged by this WP.

### 8.46 **WP-45 / ADMN-15 + ADMN-16** (2026-11-19, code `f163a1d`) — one row shipped, one halted on a control that would have shrunk the knowledge base

**ADMN-16 is done. ADMN-15 halted as HALT-10 (§8.1) with no code.** Registry: `ADMN-16`
`backlog` → `done`; `ADMN-15` `backlog` → `blocked (HALT-10)`; `ADMN-05` recorded
`partial (unblocked by RAG-11, affordance gap now ADMN-15/HALT-10)`. Backend **1067 passed
/ 3 skipped** (1042/3 baseline, measured before any edit), eval gate 6 passed, security scan
clean (9 baselined, 0 open). Dashboard `tsc --noEmit` clean, `npm test` 8 passed, `next
build` 9 routes. **No schema change, no migration, no new dependency, no server code
change** on either row — both are dashboard and test work.

**ADMN-16 closes two API-only gaps, and the two controls have different risks.**
`dashboard/app/settings/page.tsx` and `dashboard/lib/api.ts`; `tests/test_settings_controls.py`
(13 tests), `tests/test_admn16_walk.py` (9 tests), `scripts/admn16_walk.py` +
`scripts/admn16_driver.mjs`.

**The retention bound is asserted against `app.retention.policy`, not against a literal.**
The screen's `RETENTION_MIN_DAYS`/`RETENTION_MAX_DAYS` are compared to the module's own
constants and the validator is exercised at both ends and one step outside each, so a
server-side range change that nobody mirrored is a red test rather than a 400 an owner
cannot act on. Measured live through Caddy: default 90 → `PATCH 7` → 7 → `PATCH 0` → 400 →
`PATCH 731` → `invalid_retention`, "conversation_days must be at most 730".

**The browser found the defect the source tests could not, and it was a real one.** With
`min`/`max` left to native validation, typing 5000 and clicking Save produced **no message
at all**: the browser blocked submit and showed a tooltip that disappears on the next
click. The stored value was correctly unchanged (14) and the screen said nothing — which is
worse than a wrong message, because the owner cannot tell a refusal from a dead button. The
form now carries `noValidate` and the handler renders a durable sentence naming both the
bound and the fact that nothing was saved; `min`/`max` stay as spinner hints. Pinned by
`test_the_bound_is_refused_with_a_message_that_stays`. This is the M4 lesson in a form: a
201 in the log is not a working control, and neither is a correctly-unchanged row.

**Add and verify stay two actions, and the gate asserts the *refusal*.** The add handler is
bounded to its own body and asserted not to call `verifyDomain`; the verify handler is
asserted not to call `addDomain`. The browser clause then checks the widget still answers

**403** for the origin after the add, which is the property SEC-15 closed — a control that
verified on add would serve a host nobody proved they control. `lookup_failed` gets its own
sentence ("Could not check the record right now"), because telling an owner to fix a record
that may already be correct is §8.38's trap in a third place.

**Six browser clauses, all observed in live Chromium through Caddy**, and the verdict is a
pure function over the driver's report so every clause is mutation-checked. 16 mutations
across the two test files, all red: add-also-verifies, send-the-loaded-number-instead-of-
the-draft, drifted max, collapsed `lookup_failed` branch, verify-without-a-record-name,
post-nothing-still-appears-to-save, add-that-verified, record-not-shown, rejected-save-that-
still-stored, and the driver-printing-its-own-verdict guard.

**Not claimed**: the successful-verify path (a live TXT record needs SEC-15's resolver
fixture, which `tests/test_domain_verification.py` drives against a real UDP server), and
the whole-screen `/settings` browser pass, which ADMN-10 still owns.

**ADMN-15 halted because the control it names cannot be built honestly.** The full reasoning
is HALT-10 in §8.1; the short form is that the API offers "add this one back" (publish, not
additive-safe as a revert) and "serve *only* this one" (`/rollback`, which discards the rest
of the set), and **neither is a restore**. A button wired to `/rollback` under a "restore
previous version" label would silently shrink a five-document knowledge base to one, with a
200. Evidence is reproducible two ways: `scripts/admn15_evidence.py` measures the two
live-stack facts (2 served → 2 served on re-publish; 2 served → **1** served on rollback),
and `tests/test_knowledge_version_history.py` pins the third (the listing reports one row
per document, at the newest version, so no older `version_id` reaches the screen).

**Two things this WP deliberately did not do to make ADMN-15 look done.** It did not wire
the button to `/rollback` with a warning, because a confirmation dialog does not turn an
unpublish into a restore; and it did not build a versions-listing route, because §17.5 does
not list one and a real undo needs a ruling on history depth first. Both are options in
HALT-10.

**A measured correction to the inherited plan for this session.** ADMN-16's note said the
two failing copy tests were "being rewritten to guard the new truth". Only one was a
premise-falsified guard; the other was a stale duplicate of the same test, and it was
deleted rather than rewritten — two copies of one assertion means a mutation can be caught
by the copy and leave the original green, which is the failure mode §8.45 recorded from the
other direction.

**CI on `bdf67e4` (run 44552960909): green on all three jobs** — `test` (widget build, dependencies, dashboard client tests, full suite, eval gate), `security`, and `widget-browser` with steps 8 "Install browsers" and 10 "Browser matrix" both passing. The WIDG-11 Escape flake did not fire on this sample; that defect is still unfixed and still owned by the widget row.

**The docs commit `d5d8f19` (run 44555114185) is then red on `widget-browser` alone** — step 10 "Browser matrix" — with the annotation naming **WIDG-11** in full on both engines (`Escape from the host page left the panel open`, `expanded=["false@579","true@580"]` in chromium and `["false@617","true@636"]` in firefox). `test` and `security` are green. That commit differs from `bdf67e4` only in Markdown (`git diff --stat` = AGENTS.md, docs/FEATURES.md, docs/PLAN.md; 5 insertions, 1 deletion) and the widget subtree is byte-identical `7ed153c` on both, so the assertion cannot come from this change. The flake fired on the docs push, not on the code push — **do not push again to chase a green**, and do not read it as a regression in this WP.

**M5 after this WP**: `ADMN-05` (its own Chromium gate) and `ADMN-10`'s screen pass;
`ADMN-01`/`ADMN-02` remain partial; `ADMN-15` is blocked on HALT-10. M6 is unchanged.

### 8.48 **WP-46 / ADMN-17** (2026-11-19, code `7e80723`) — rollback restores the previous set, and the gate that could not have seen it

**ADMN-17 is done.** Registry: `ADMN-17` `backlog` → `done`. It implements HALT-10's
ruling (§8.47) in full: a rollback restores the **whole** set the bot served before the
current one, or refuses with a named reason. Backend **1103 passed / 3 skipped**, eval gate
6 passed. Dashboard `tsc --noEmit` clean, `npm test` 10 passed. Migration `0009_knowledge_set_history`,
`tests/test_knowledge_rollback.py` (12), `tests/test_knowledge_rollback_control.py` (`botconfig`
exposure), `tests/test_knowledge_api.py`, `tests/test_admn17_walk.py` (16) — **56 tests green**
across the four files. **No new dependency.**

**The defect and the fix, in one sentence.** `POST /knowledge/rollback` took a `version_id`
and built a **one-member** set from it, so an owner serving five documents who rolled one
back ended up serving that document alone — four silently stopped being answerable, with a
200. Measured in §8.46 as 2 served → 1 served. Publishing stayed additive throughout; only
rollback's meaning changed.

**The edge is recorded, not inferred, and that is the ruling's first consequence.**
`knowledge_sets.previous_set_id` is written by publish (`was_serving = active_set(...)` read
*before* `point_at` moves the pointer, so a concurrent publish cannot stamp the wrong edge).
The alternative — deriving "previous" from `created_at` — ties on two publishes in the same
second and would restore an arbitrary set. `ON DELETE SET NULL`, so deleting an old set does
not cascade into the sets that followed it; a cleared edge makes rollback refuse, which is
the fail-closed direction. **No backfill**: sets predating `0009` carry `NULL` and rollback
says so rather than guessing.

**Three refusals, each with its own code, because the owner's next action differs**:
`nothing_published` (no pointer at all), `no_previous_set` (the first publish, or a set from
before `0009`), `previous_set_empty` (an edge to a set whose documents were since deleted —
restoring it would leave the bot answering from nothing, which is the unpublish this row
removes wearing a restore's label). All three are **409**, not 404: the request is well
formed and the state refuses it.

**Atomic by construction, and that is §10.5's single-column swap, not a new mechanism.** A
rollback moves `bots.active_knowledge_set_id` to the earlier set in one `UPDATE`. It creates
**no new set row**, so history stays the record of *publishes*; a second rollback therefore
walks one more set back, which is what "the previous set" means.

**`version_id` is refused, not ignored.** A caller naming one is asking for the old
subset semantics; answering with a different effect would be this defect one layer up. That
is a **422 `version_id_not_supported`**, and `scripts/m1_gate.py` had to be updated because
it was POSTing `version_id` to this endpoint — it was asserting the HALT-07/HALT-10 defect
as expected behaviour (`served == [version]`).

**The edge is followed only when both ends belong to this tenant and this bot.** `previous_set`
is a join, not two lookups: the FK cannot express "same bot", so a stray edge is refused
rather than followed. Both directions are tested (a foreign-bot edge, a foreign-tenant edge).

**The live browser gate found two real defects, both in the gate rather than the screen —
and this is the transferable part.** `scripts/admn17_walk.py` first run:
**2 of 7 clauses red**. Neither was a page bug:

1. **The notice was read after `page.reload()`.** The notice is React state written from
   the rollback response, so a reload remounts the screen and clears it — the clause
   reported `notice=` (empty) for every restore and **could never have passed**, on a
   correct screen. The table still has to be re-read after a reload (its count must come
   from the API, not the click); the notice must be read *before* it.
2. **The hint lookup matched the `<h2>Documents</h2>`.** `controlState` searched the whole
   Documents section for the first line matching `/document|restore/i`, and the section's
   own heading matches — so it returned the heading and never reached the sentence beside
   the button. It now reads the span in the button's own row.

Mutation-checked rather than assumed: re-introducing the read-after-reload order
reproduces exactly one red clause (the notice) and leaves the other six green. **A gate
whose observation procedure cannot observe is indistinguishable from a passing gate** —
§8.28's "a test that cannot fail is not evidence", on the browser side.

**All seven clauses observed in live Chromium through Caddy**, four publishes then three
restores: served counts **1 → 2 → 3 → 4** on publish and **3 → 2 → 1** on restore, each the
whole earlier set; the `Served` column agreeing with the API after every reload; the control
disabled with "Nothing to restore yet…" before the first publish and again once history is
exhausted. The old behaviour fails clause 3 by construction (every restore yields 1).

**Two source mutations, both red.** Publish recording no edge (`previous_set_id=None`) →
1 red. Rollback building a one-member subset (the defect itself) → **6 red**, including
"restores every document", "the restored documents actually answer", "a rollback writes no
new set" and "the pointer is still one column".

**Verified on the live stack, not only in-process.** `api` and `dashboard` rebuilt and
probed through Caddy (`/api/v1/bots` → 401, `/knowledge` → 200), live database migrated to
`0009_knowledge_set_history`, then the walk drove the real screen. The served pointer moves
only through `point_at` — checked by grepping every `active_knowledge_set_id` write.

**CI on `7e80723` (run for this sha): green on all three jobs** — `test`, `security` and
`widget-browser`, so the WIDG-11 Escape flake did not fire on this sample and that defect
remains unfixed and still owned by the widget row.

**Not claimed**: no per-document undo (a rollback is whole-set by the ruling), no history
browser, and no restore of a set that predates `0009` (refused, deliberately).

### 8.49 **WP-47 / WIDG-17** (2026-11-19) — the Escape race had two causes, and the row named the wrong one

**WIDG-17 is done** (code `5093bb2`). Registry row `backlog` → `done`; `WIDG-11`
annotated with the fix pointer and its now-honest settle-window clause. Backend **1103
passed / 3 skipped**, eval gate 6 passed, security scan clean (9 baselined, 0 open). Widget
unit **41 passed / 11 skipped**; dashboard untouched. **No new dependency, no schema or API
change, no backend source change** — `backend/api/widget/src` plus tests, `ci.yml`, and one
Python test file.

**The row's hypothesis was wrong in its second half, and saying so is the point.** It read
"the frame posts `close` AND the loader's document-level keydown also fires on the same
press". The loader's document listener is real, but **an iframe's keydown cannot bubble to
the host document** — they are separate documents. That listener is also *load-bearing*: it
is what makes Escape work when focus sits on the host's launcher or `body`, the WIDG-16
Firefox fix. Deleting it as "the double handler" would have re-broken keyboard close on
Firefox. Had the row been implemented as written, the flake would have survived and a green
matrix would have been the only evidence.

**Both real causes were measured, not reasoned.** Two probes with the app bundle held:

1. A single Escape press starting inside the panel posted `close` **2** times. The app bound
   `onKeyDown` on `panel` *and* on `document`; a keydown in the panel bubbles to the
   document, so both listeners ran for one event. Fix: bind the document only — it sees
   every focused element inside the frame, since the panel is the frame's only content.
2. The loader's `case "ready"` called `openChat()` unconditionally. When the app bundle
   finished loading *after* Escape had closed the panel, the frame's `ready` post reopened
   it. That is the observed shape exactly: `expanded=["false@630","true@648"]` — 18 ms.
   Fix: return early when `!open`. When the panel *is* open the same call is what hands the
   freshly loaded app its session, so the open path is unchanged.

**The WIDG-11 case could not have caught this, and that is why it flaked.** It asserted the
panel *was closed* at one instant, not that it **stayed** closed, so a close-then-reopen
inside the 400 ms settle window passed the assertion and only failed when the sampling
landed between the two states. It now reads the `aria-expanded` mutation log and fails if
any `true` follows the first `false`, in both the host-focus and in-frame halves.

**Two new browser cases, each pinned to one cause.** `one Escape press posts exactly one
close` counts `postMessage` calls at the frame's own boundary — a source-level "there is one
listener" reading cannot satisfy it. `a late frame ready cannot reopen a panel the visitor
closed` holds `app.js` via `page.route` so the ordering is deterministic instead of a race;
**that is the difference between a gate that fails every run and one that fails half the
time.**

**Three mutations, three red, each isolating a different clause.** Removing the `ready`
guard leaves the late-ready case as the only red; restoring the panel listener leaves the
one-close case as the only red; making `closeChat` reopen 20 ms later turns both the new
case and WIDG-11's settle clause red. A mutation run was repeated after a stale container
bundle contaminated the first one (both mutations applied at once produced two reds and
looked like a shared cause) — **push the bundle into the container before believing a
browser mutation**, the in-process suite cannot see the served asset at all.

**Static guards, for the CI surface that has no browser.** `a11y.test.mjs` asserts exactly
one `addEventListener("keydown"` in the app; `widget.test.mjs` slices the `ready` branch
specifically and requires `if (!open) return;` in it, so an `open` test elsewhere in the
loader cannot satisfy the guard. Both mutation-checked.

**The CI floor was a literal that this WP would have made a lie.** The matrix step demanded
"9 cases"; this WP makes 11, and a floor of 9 would have let a whole case vanish. It is now
11, and `tests/test_ci_workflow.py` **derives** it from the `matrixTest(` count rather than
retyping it — the same defect class as §8.45's retyped retention bound.

**Verified live through Caddy on a rebuilt image.** `loader.js` and `app.js` served by
`eva-api-1` are byte-identical to `dist/` (not a hand-copied bundle), then **11/11 on
Chromium, Firefox and WebKit**, and Chromium **5 consecutive runs at 11/11** — the flake
fired at roughly half before. Probe tenants were deleted and verified gone.

**CI run 44608793911 on `f33dd2d` is green on all three jobs** — `test`, `security` and
`widget-browser`, the last with step 8 "Install browsers" and step 10 "Browser matrix" both
passing. Job logs need admin (HTTP 403), so the case count is read from the step's own gate
rather than a log: step 10 exits 1 if `skipped != 0` or `pass < 11`, and it passed, which is
11 cases run and 11 passed. That is the first green `widget-browser` here on a commit that
touches the widget subtree. **One green sample is not proof the race is gone** — the local
evidence is the five consecutive green runs plus three mutations that each reproduce a red
deterministically, and the flake fired at roughly half before. Do not read this single CI
sample as more than it is; equally, do not re-push to chase a green if a later unrelated
commit samples it red.

**Not claimed**: nothing about the frame's own Escape handling changed beyond removing the
duplicate listener; the WIDG-11 row keeps its screen-reader limit; Edge is covered only as
Chromium.
