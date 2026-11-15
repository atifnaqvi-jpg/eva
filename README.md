# EVA

**EVA** is a multi-tenant SaaS platform that lets a business deploy an AI customer-support
chatbot on its website via a one-line script embed — grounded answers from the business's own
documents and site content (RAG), proactive suggested questions, and email-based human handover
when the assistant cannot answer.

**Status:** prototype in active development. Text chat, English only, read-only assistant.

- Architecture and decisions: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (frozen, checksum-gated)
- Programme plan and gate constitution: [`docs/PLAN.md`](docs/PLAN.md)
- Full requirements/architecture source: [`docs/spec/blueprint-full.md`](docs/spec/blueprint-full.md)
- Feature registry: [`docs/FEATURES.md`](docs/FEATURES.md)

## Stack

Python 3.12 / FastAPI · Next.js · vanilla-TS embed widget (iframe) · PostgreSQL + pgvector ·
Redis · LiteLLM (model-agnostic) · Docker Compose · Caddy.

## Security posture

Secrets never enter the repository: runtime configuration uses `.env` (gitignored) with
placeholders in `.env.example`. The build agent's standing charter (AGENTS.md) forbids
committing credentials, customer data, personal information, or machine-internal topology
to any repository.

## License

Apache-2.0 — see [LICENSE](LICENSE).
