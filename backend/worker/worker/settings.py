import os

from arq import cron
from arq.connections import RedisSettings

from app.observability import logs as obs_logs
from worker.crawl_job import crawl_source
from worker.ingest_job import ingest_document
from worker.purge_job import purge_expired

# OPS-01. arq configures logging from its CLI before any of our code runs, so a
# `configure()` in `on_startup` would leave the worker's own banner — the first lines
# it writes — as plain text beside our JSON. `--custom-log-dict` is called at exactly
# that moment, and it needs a dict, not a function (see the worker Dockerfile CMD).
LOG_CONFIG = obs_logs.dict_config()


async def ping(ctx: dict) -> str:
    return "pong"


class WorkerSettings:
    functions = [ping, ingest_document, crawl_source, purge_expired]
    redis_settings = RedisSettings.from_dsn(
        os.environ.get("REDIS_URL", "redis://redis:6379/0")
    )
    # Retry with backoff rather than dropping work (KNOW-09 / ARCHITECTURE 20.4).
    max_tries = 5
    job_timeout = 900
    # Retention runs daily rather than continuously: the window is measured in days, so
    # a tighter schedule would only add load. `unique=True` stops overlapping runs if one
    # ever takes longer than a day.
    cron_jobs = [cron(purge_expired, hour=3, minute=15, unique=True)]
