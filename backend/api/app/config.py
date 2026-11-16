"""Runtime configuration.

Values are read from the environment only; nothing secret lives in the repo.
Two credentials on purpose: migrations need the database owner (DDL, CREATE ROLE,
extensions), while the running app connects as a non-superuser so RLS applies.
"""

import os

# BGE-M3 default per docs/STACK.md. The dimension is fixed per database: a model
# change means new chunks (re-ingestion), never in-place mutation (§16.1).
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

# Runtime connection: non-superuser, subject to row-level security.
APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://eva_app:changeme@127.0.0.1:5432/eva"
)

# Owner connection, used only by Alembic.
MIGRATION_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://eva:changeme@127.0.0.1:5432/eva"
)

# Session-level GUC the RLS policies read (§16.3).
TENANT_SETTING = "app.tenant_id"

# --- Auth (WP-03) -----------------------------------------------------------
# Session lifetime. Rotation happens on login: a fresh row and a fresh token,
# never reuse (ARCHITECTURE 19.1).
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(60 * 60 * 12)))

COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "eva_session")
# Secure must stay on in production (TLS via Caddy). It is only relaxed for local
# http testing, and the default remains the safe value.
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false"
COOKIE_SAMESITE = "lax"

# Argon2id cost parameters. Defaults follow RFC 9106 4 / OWASP guidance for
# password hashing. Tests lower these deliberately.
ARGON2_MEMORY_KIB = int(os.environ.get("ARGON2_MEMORY_KIB", "65536"))
ARGON2_TIME_COST = int(os.environ.get("ARGON2_TIME_COST", "3"))
ARGON2_PARALLELISM = int(os.environ.get("ARGON2_PARALLELISM", "4"))

# Argon2 is memory-hard by design, so concurrent hashes multiply memory: at the
# defaults each hash needs ~64 MiB live, and FastAPI runs sync endpoints on a
# 40-thread pool. Unbounded, a login burst exhausted the api container's 512 MB
# limit and the kernel OOM-killed uvicorn (measured, M0 gate). Serialise hashing
# so memory is one hash per process regardless of request concurrency; the ~130 ms
# cost makes the queueing negligible.
ARGON2_MAX_CONCURRENT = int(os.environ.get("ARGON2_MAX_CONCURRENT", "1"))

# --- Widget (WP-16) ---------------------------------------------------------
# Signs public widget session tokens (§12.3). Deliberately has NO default: an
# unset value disables the widget rather than falling back to a shared key, because
# a shared key would let anyone mint a token for any tenant. Production sets it in
# the compose env, like the other secrets.
WIDGET_TOKEN_SECRET = os.environ.get("WIDGET_TOKEN_SECRET", "")

# Visitor attachment cap (R45). 5 MB, separate from the 25 MB knowledge-upload
# ceiling: this is untrusted stranger traffic on an unauthenticated endpoint, and
# the bytes are stored rather than ingested.
VISITOR_MAX_UPLOAD_BYTES = int(
    os.environ.get("VISITOR_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))
)

# --- Knowledge ingestion (WP-06 / WP-07) ------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

# Upload ceiling. A parser is the first thing an untrusted file touches, so the
# limit is enforced before parsing, not after.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

# Embedding for *queries*. Ingestion embeddings are produced in the worker; query
# embeddings come from the same model so the vectors share a space. Default is
# "local", which requires the model in the API image — see EMBEDDING_QUERY_URL for
# the deployment split.
EMBEDDING_QUERY_BACKEND = os.environ.get("EMBEDDING_QUERY_BACKEND", "local")
EMBEDDING_QUERY_URL = os.environ.get("EMBEDDING_QUERY_URL", "")

# --- Conversation / LLM (WP-11, WP-13) --------------------------------------
# An opaque "provider/model" string, exactly as LiteLLM spells it (CONV-11).
# The turn loop passes it through without interpreting it, so switching providers
# is a config change and not a code change.
LLM_DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "ollama_chat/deepseek-v3.1")

# Which provider implementation to build at startup. "unconfigured" is the honest
# default: without a platform key or a tenant BYOK credential there is nothing to
# call, and the turn loop degrades to the handover message instead of failing.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "unconfigured")
