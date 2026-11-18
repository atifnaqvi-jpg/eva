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

# --- Handover notifications (WP-17, §13, R19) -------------------------------
# Outbound email for handover. Unset SMTP_HOST means "no email": the handover row
# and transcript are still written, and the owner sees them in the dashboard, so an
# unconfigured deployment degrades rather than fails. Defaults are a local mailhog-
# style host on the compose network, which is what tests point at.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@eva.local")
# STARTTLS is the default because submission on 587 needs it (R19). A local sink on a
# private compose network has no certificate to offer, so it must be switchable —
# leaving it hardcoded would make the only testable configuration the unsafe one.
SMTP_TLS = os.environ.get("SMTP_TLS", "true").lower() != "false"

# Handover notifications run off the request path: the visitor has already been told
# a human is coming, and an SMTP timeout must not hold their connection open.
HANDOVER_NOTIFY_ASYNC = os.environ.get("HANDOVER_NOTIFY_ASYNC", "true").lower() != "false"

# --- Webhooks (HAND-06, R44) ------------------------------------------------
# A webhook URL is tenant-controlled and fetched from inside the network, so the
# SSRF guard in `handover.webhooks.validate_url` is on by default. This flag exists
# only so tests and local verification can point at a receiver on the compose
# network; setting it true in a deployment that accepts customer URLs removes the
# guard that stops a tenant from fetching the metadata service or Postgres.
WEBHOOK_ALLOW_PRIVATE_HOSTS = (
    os.environ.get("WEBHOOK_ALLOW_PRIVATE_HOSTS", "false").lower() == "true"
)

# --- Alerting (OPS-04, §22, §27) --------------------------------------------
# The monitored address. Deliberately separate from `settings.handover.email`: that one
# is the *tenant owner's* mailbox, and a platform alert carries tenant ids and provider
# names, i.e. deployment topology a customer did not ask for (HALT-09 point 3). Unset
# means alerts are logged, not sent — an explicit, visible degradation, because a
# deployment whose alert channel is silent must not look like a quiet system.
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")

# HALT-09's defaults are industry values, not policy: they are *starting values* an
# operator changes here (a `.env` edit, no code deploy) and, for the two tenant-scoped
# signals, a tenant changes in `tenants.settings -> alerts`. Changing a default is not a
# gate event.
#
# Provider error rate: >5% of turns failing over the window (warn), >20% (critical).
ALERT_PROVIDER_ERROR_WARN = float(os.environ.get("ALERT_PROVIDER_ERROR_WARN", "0.05"))
ALERT_PROVIDER_ERROR_CRITICAL = float(
    os.environ.get("ALERT_PROVIDER_ERROR_CRITICAL", "0.20")
)
# A noise floor, not a hidden threshold: 2 failures out of 2 attempts is 100% and would
# page on the first bad minute of a deployment with three turns an hour.
ALERT_PROVIDER_MIN_ATTEMPTS = int(
    os.environ.get("ALERT_PROVIDER_MIN_ATTEMPTS", "10")
)

# Budget: 90% of the month's token budget (warn), 100% (critical — CONV-14 is refusing
# turns at that point).
ALERT_BUDGET_WARN = float(os.environ.get("ALERT_BUDGET_WARN", "0.90"))
ALERT_BUDGET_CRITICAL = float(os.environ.get("ALERT_BUDGET_CRITICAL", "1.00"))

# Disk: 85% (warn), 95% (critical) — industry norm; measured usage on this box was 51%
# when the ruling was written (§8.33).
ALERT_DISK_WARN = float(os.environ.get("ALERT_DISK_WARN", "0.85"))
ALERT_DISK_CRITICAL = float(os.environ.get("ALERT_DISK_CRITICAL", "0.95"))
# `/` is what the container sees; it is the filesystem the overlay and the `pgdata` volume
# both sit on, which is the one whose fullness matters.
ALERT_DISK_PATH = os.environ.get("ALERT_DISK_PATH", "/")

# Queue: depth >100 for five minutes (warn), >500 (critical).
ALERT_QUEUE_WARN = int(os.environ.get("ALERT_QUEUE_WARN", "100"))
ALERT_QUEUE_CRITICAL = int(os.environ.get("ALERT_QUEUE_CRITICAL", "500"))
ALERT_QUEUE_SUSTAIN_SECONDS = int(
    os.environ.get("ALERT_QUEUE_SUSTAIN_SECONDS", "300")
)

# Backup: any failed or missed run — binary, so there is no warning pair. The window is
# the daily 02:45 schedule plus two hours of slack, so a missed night is reported the
# same morning rather than the next.
ALERT_BACKUP_MAX_AGE_SECONDS = int(
    os.environ.get("ALERT_BACKUP_MAX_AGE_SECONDS", str(26 * 60 * 60))
)
# Where OPS-09 writes its manifests. The worker mounts it read-only; see the runbook. The
# default is the script's own default so an unconfigured stack fails the same way the
# backup script would.
ALERT_BACKUP_DIR = os.environ.get("ALERT_BACKUP_DIR", "/var/backups/eva")

# How far back the provider error rate looks. Shares OPS-03's five-minute window for the
# reason `metrics.WINDOW_SECONDS` gives: an operator comparing the graph and the alert
# must not be comparing two definitions of "recently".
ALERT_WINDOW_SECONDS = int(os.environ.get("ALERT_WINDOW_SECONDS", "300"))

# --- Domain verification (SEC-15, §16.1) ------------------------------------
# Which resolver answers the TXT lookup. Empty means "the first nameserver in
# /etc/resolv.conf", which is what a container is given; the override exists so a
# deployment with an internal resolver, and the test suite, can name one explicitly
# rather than depending on whatever the Docker daemon wrote into the file.
DNS_RESOLVER = os.environ.get("EVA_DNS_RESOLVER", "")
