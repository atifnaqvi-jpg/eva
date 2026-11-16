"""Enqueue helpers for the arq worker.

Kept separate from the router so the request path only needs ``arq``'s client, and
a missing Redis surfaces as one clear failure instead of a stack trace deep in
ingestion.
"""

import logging
from uuid import UUID

from arq import create_pool
from arq.connections import RedisSettings

from app.config import REDIS_URL

logger = logging.getLogger(__name__)


async def _pool():
    return await create_pool(RedisSettings.from_dsn(REDIS_URL))


def enqueue_ingestion(tenant_id: UUID, version_id: UUID, filename: str) -> None:
    _run(_enqueue_ingestion(tenant_id, version_id, filename))


def enqueue_crawl(tenant_id: UUID, source_id: UUID, url: str) -> None:
    _run(_enqueue_crawl(tenant_id, source_id, url))


async def _enqueue_ingestion(tenant_id: UUID, version_id: UUID, filename: str) -> None:
    pool = await _pool()
    await pool.enqueue_job(
        "ingest_document", str(tenant_id), str(version_id), filename
    )
    await pool.aclose()


async def _enqueue_crawl(tenant_id: UUID, source_id: UUID, url: str) -> None:
    pool = await _pool()
    await pool.enqueue_job("crawl_source", str(tenant_id), str(source_id), url)
    await pool.aclose()


def _run(coroutine) -> None:
    import asyncio

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
            return
        loop.create_task(coroutine)  # called from an async context
    except Exception:
        logger.exception("failed to enqueue job")
        raise
