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
# password hashing. NOTE: the in-repo pure-Python implementation is far too slow
# at these settings to serve requests — see the WP-03 report (HALT on the
# argon2-cffi dependency question). Tests lower these deliberately.
ARGON2_MEMORY_KIB = int(os.environ.get("ARGON2_MEMORY_KIB", "65536"))
ARGON2_TIME_COST = int(os.environ.get("ARGON2_TIME_COST", "3"))
ARGON2_PARALLELISM = int(os.environ.get("ARGON2_PARALLELISM", "4"))
