import os

from arq.connections import RedisSettings

from worker.ingest_job import ingest_document


async def ping(ctx: dict) -> str:
    return "pong"


class WorkerSettings:
    functions = [ping, ingest_document]
    redis_settings = RedisSettings.from_dsn(
        os.environ.get("REDIS_URL", "redis://redis:6379/0")
    )
    # Retry with backoff rather than dropping work (KNOW-09 / ARCHITECTURE 20.4).
    max_tries = 5
    job_timeout = 900
