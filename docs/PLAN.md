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

## 9. Environment Resolution Record

2026-11-15: user ruled **Netcup VM is the OpenHands platform for this project's development and prototyping — not Oracle**. Verified and fixed same day: gateway `Host <build-vm>` alias (<netcup build vm>, key `<operator key path removed>`) already worked; box had canvas + wrappers but lacked the `openhands` CLI and its wrapper read the masked legacy settings file → installed CLI via `uv tool install openhands@1.16.0` (parity with Oracle) and patched `~/bin/talos-openhands.sh` to the proven live-settings scheme (backups: `.bak-20261115`, `.bak2-20261115`). Smoke run passed. Oracle VM untouched (its 98%-full disk no longer gates this project).

## 10. Rulings Status

1. **Build VM — RESOLVED:** Netcup (user, 2026-11-15). Oracle disk blocker void.
2. **Embeddings — defaulting:** self-host BGE-M3 (capacity now confirmed ample: 14 Gi RAM / 8 cores; free, data local). Managed-API override possible until WP-06 starts.
3. **Codename — RESOLVED:** **EVA** (user, 2026-11-15). Repo dir `~/work/eva`; product name "EVA".
4. **Go — OPEN:** awaiting explicit go to start Sprint 0 (WP-00: repo init, docs bundle, FEATURES.md registry, G1 checksum gate, CI skeleton) on <build-vm> at `~/work/eva`.
