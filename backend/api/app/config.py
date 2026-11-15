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
