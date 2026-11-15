# EVA — Locked Stack (frozen)

Locked stack table with rejected alternatives. The build loop may NOT edit this file nor re-evaluate rejected options without a user ruling (G1).

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
