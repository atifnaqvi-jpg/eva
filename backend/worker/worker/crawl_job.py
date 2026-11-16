"""arq job: crawl a site and ingest every page as a version (KNOW-05).

Each page becomes its own document+version so the owner can see per-page status
and preview a single page. Pages whose content is unchanged (same ``content_hash``
as the latest version) are skipped, so a weekly recrawl does not re-embed an
entire site to produce identical vectors.
"""

import logging
from uuid import UUID

from sqlalchemy import text

from app.db.session import tenant_session
from app.ingestion.crawler import crawl
from app.ingestion.pipeline import ingest_html
from app.knowledge import service as knowledge

from worker.ingest_job import _get_provider

logger = logging.getLogger(__name__)


async def crawl_source(ctx: dict, tenant_id: str, source_id: str, url: str) -> dict:
    tenant_uuid = UUID(tenant_id)
    result = await crawl(url)

    created = unchanged = 0
    failures: list[dict[str, str]] = []

    for page in result.pages:
        try:
            # Crawled pages are HTML by construction; the crawler already filtered
            # out non-HTML responses, so this does not need to re-sniff.
            ingested = ingest_html(page.html)
            with tenant_session(tenant_uuid) as session:
                existing = session.execute(
                    text(
                        "SELECT 1 FROM document_versions v"
                        "  JOIN documents d ON d.id = v.document_id"
                        " WHERE d.tenant_id = :t AND d.uri = :u"
                        "   AND v.content_hash = :h"
                    ),
                    {"t": str(tenant_uuid), "u": page.url, "h": ingested.content_hash},
                ).scalar()
                if existing:
                    unchanged += 1
                    continue

                document_id = knowledge.upsert_document(
                    session, tenant_uuid, source_id=UUID(source_id),
                    title=ingested.title or page.url, uri=page.url,
                )
                version_id = knowledge.create_version(
                    session, tenant_uuid, document_id=document_id,
                    content_hash=ingested.content_hash,
                )
                knowledge.store_chunks(
                    session, tenant_uuid, version_id=version_id,
                    ingested=ingested, embedder=_get_provider(),
                )
                knowledge.set_version_status(session, version_id, "ready")
                session.commit()
                created += 1
        except Exception as exc:
            logger.exception("page failed during crawl: %s", page.url)
            failures.append({"url": page.url, "error": str(exc)[:300]})

    return {
        "crawled": len(result.pages),
        "created": created,
        "unchanged": unchanged,
        "failed": failures,
        "skipped": result.summary()["skipped"],
    }
