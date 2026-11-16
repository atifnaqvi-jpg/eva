"""arq job: parse -> clean -> chunk -> embed -> store (KNOW-09, RAG-01).

Idempotency and failure visibility are the requirements (ARCHITECTURE 8.4/20.4):

* re-running the job for a version **replaces** that version's chunks rather than
  appending, so a retry after a partial write converges instead of duplicating
* a failure marks the version ``failed`` with the message and re-raises, so arq
  retries with backoff *and* the owner sees the error in the dashboard

The job takes ids, not payloads: the bytes live in the database, so a retry never
depends on a request body that has long since been discarded.
"""

import logging
from uuid import UUID

from sqlalchemy import text

from app.db.session import tenant_session
from app.ingestion.pipeline import ingest_bytes
from app.knowledge import service as knowledge
from app.retrieval.embedding import RemoteEmbedder

logger = logging.getLogger(__name__)

_provider: RemoteEmbedder | None = None


def _get_provider() -> RemoteEmbedder:
    """Ingestion embeddings come from the shared service (one model, one copy)."""
    global _provider
    if _provider is None:
        import os

        _provider = RemoteEmbedder(
            os.environ.get("EMBEDDING_QUERY_URL", "http://embed:8080")
        )
    return _provider


async def ingest_document(
    ctx: dict,
    tenant_id: str,
    version_id: str,
    filename: str,
    payload: bytes | None = None,
) -> dict:
    """Ingest one document version."""
    tenant_uuid = UUID(tenant_id)
    version_uuid = UUID(version_id)

    with tenant_session(tenant_uuid) as session:
        knowledge.set_version_status(session, version_uuid, "processing")
        session.commit()

        try:
            raw = payload or _load_payload(session, version_uuid)
            ingested = ingest_bytes(filename, raw)

            # Replace rather than append: a retry must converge.
            session.execute(
                text("DELETE FROM chunks WHERE document_version_id = :v AND tenant_id = :t"),
                {"v": str(version_uuid), "t": str(tenant_uuid)},
            )
            knowledge.store_chunks(
                session,
                tenant_uuid,
                version_id=version_uuid,
                ingested=ingested,
                embedder=_get_provider(),
            )
            knowledge.set_version_status(session, version_uuid, "ready")
            session.commit()
        except Exception as exc:
            session.rollback()
            with tenant_session(tenant_uuid) as error_session:
                knowledge.set_version_status(
                    error_session, version_uuid, "failed", str(exc)[:2000]
                )
                error_session.commit()
            logger.exception("ingestion failed for version %s", version_uuid)
            raise

    return {
        "version_id": str(version_uuid),
        "chunks": len(ingested.chunks),
        "content_hash": ingested.content_hash,
    }


def _load_payload(session, version_id: UUID) -> bytes:
    row = session.execute(
        text("SELECT payload FROM document_versions WHERE id = :v"),
        {"v": str(version_id)},
    ).scalar()
    if row is None:
        raise ValueError(f"version {version_id} has no stored payload")
    return row if isinstance(row, bytes) else bytes(row)
