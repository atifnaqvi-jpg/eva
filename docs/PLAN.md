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

## 9. Environment Resolution Record

2026-11-15: user ruled **Netcup VM is the OpenHands platform for this project's development and prototyping — not Oracle**. Verified and fixed same day: gateway `Host <build-vm>` alias (<netcup build vm>, key `<operator key path removed>`) already worked; box had canvas + wrappers but lacked the `openhands` CLI and its wrapper read the masked legacy settings file → installed CLI via `uv tool install openhands@1.16.0` (parity with Oracle) and patched `~/bin/talos-openhands.sh` to the proven live-settings scheme (backups: `.bak-20261115`, `.bak2-20261115`). Smoke run passed. Oracle VM untouched (its 98%-full disk no longer gates this project).

## 10. Rulings Status

1. **Build VM — RESOLVED:** Netcup (user, 2026-11-15). Oracle disk blocker void.
2. **Embeddings — defaulting:** self-host BGE-M3 (capacity now confirmed ample: 14 Gi RAM / 8 cores; free, data local). Managed-API override possible until WP-06 starts.
3. **Codename — RESOLVED:** **EVA** (user, 2026-11-15). Repo dir `~/work/eva`; product name "EVA".
4. **Go — OPEN:** awaiting explicit go to start Sprint 0 (WP-00: repo init, docs bundle, FEATURES.md registry, G1 checksum gate, CI skeleton) on <build-vm> at `~/work/eva`.
