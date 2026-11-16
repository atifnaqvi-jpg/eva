"""Knowledge ingestion, versioning, publish/rollback and preview (WP-06/WP-08).

The versioning model follows ARCHITECTURE 10.5 literally:

* every ingestion appends a ``document_versions`` row; chunks hang off that row
* **publishing** only moves ``bots.active_knowledge_version_id`` — a single-row
  pointer swap, so it is atomic by construction and rollback is the same
  operation pointing backwards
* nothing mutates a published version in place, so the previous version stays
  readable (and previewable) until the pointer moves
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ingestion.pipeline import IngestedDocument
from app.retrieval.embedding import EmbeddingProvider
from app.retrieval.store import VectorRecord


@dataclass(frozen=True)
class VersionInfo:
    version_id: UUID
    document_id: UUID
    version_no: int
    status: str
    chunk_count: int


def create_document(
    session: Session,
    tenant_id: UUID,
    *,
    source_id: UUID,
    title: str,
    uri: str | None = None,
) -> UUID:
    return session.execute(
        text(
            """
            INSERT INTO documents (tenant_id, source_id, title, uri)
            VALUES (:tenant_id, :source_id, :title, :uri)
            RETURNING id
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "source_id": str(source_id),
            "title": title,
            "uri": uri,
        },
    ).scalar_one()


def latest_version_no(session: Session, document_id: UUID) -> int:
    return (
        session.execute(
            text("SELECT coalesce(max(version_no), 0) FROM document_versions"
                 " WHERE document_id = :document_id"),
            {"document_id": str(document_id)},
        ).scalar()
        or 0
    )


def create_version(
    session: Session,
    tenant_id: UUID,
    *,
    document_id: UUID,
    content_hash: str,
    status: str = "pending",
) -> UUID:
    version_no = latest_version_no(session, document_id) + 1
    return session.execute(
        text(
            """
            INSERT INTO document_versions
                   (tenant_id, document_id, version_no, content_hash, status)
            VALUES (:tenant_id, :document_id, :version_no, :content_hash, :status)
            RETURNING id
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "document_id": str(document_id),
            "version_no": version_no,
            "content_hash": content_hash,
            "status": status,
        },
    ).scalar_one()


def set_version_status(
    session: Session, version_id: UUID, status: str, error: str | None = None
) -> None:
    session.execute(
        text("UPDATE document_versions SET status = :status, error = :error WHERE id = :id"),
        {"id": str(version_id), "status": status, "error": error},
    )


def store_chunks(
    session: Session,
    tenant_id: UUID,
    *,
    version_id: UUID,
    ingested: IngestedDocument,
    embedder: EmbeddingProvider,
) -> list[VectorRecord]:
    """Insert chunks and their vectors for a version; returns the records written."""
    texts = [chunk.text for chunk in ingested.chunks]
    vectors = embedder.embed(texts) if texts else []

    records: list[VectorRecord] = []
    for chunk, vector in zip(ingested.chunks, vectors, strict=True):
        row = session.execute(
            text(
                """
                INSERT INTO chunks
                       (tenant_id, document_version_id, ordinal, text, token_count,
                        metadata, embedding, embedding_model, embedding_dim)
                VALUES (:tenant_id, :version_id, :ordinal, :text, :token_count,
                        CAST(:metadata AS jsonb), CAST(:embedding AS vector),
                        :model, :dim)
                RETURNING id
                """
            ),
            {
                "tenant_id": str(tenant_id),
                "version_id": str(version_id),
                "ordinal": chunk.ordinal,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "metadata": _json(chunk.metadata),
                "embedding": _pgvector(vector),
                "model": embedder.key,
                "dim": embedder.dimension,
            },
        ).scalar_one()
        records.append(
            VectorRecord(
                chunk_id=row,
                document_version_id=version_id,
                text=chunk.text,
                embedding=vector,
            )
        )
    return records


def publish(session: Session, tenant_id: UUID, *, bot_id: UUID, version_id: UUID) -> UUID:
    """Point the bot at ``version_id``.

    Single UPDATE, so concurrent publishes cannot interleave into a half-swapped
    state; the last writer wins, which is the intended semantics.
    """
    session.execute(
        text(
            "UPDATE bots SET active_knowledge_version_id = :version_id"
            " WHERE id = :bot_id AND tenant_id = :tenant_id"
        ),
        {
            "version_id": str(version_id),
            "bot_id": str(bot_id),
            "tenant_id": str(tenant_id),
        },
    )
    return version_id


def active_version(session: Session, tenant_id: UUID, bot_id: UUID) -> UUID | None:
    return session.execute(
        text("SELECT active_knowledge_version_id FROM bots"
             " WHERE id = :bot_id AND tenant_id = :tenant_id"),
        {"bot_id": str(bot_id), "tenant_id": str(tenant_id)},
    ).scalar()


def version_chunk_count(session: Session, version_id: UUID) -> int:
    return session.execute(
        text("SELECT count(*) FROM chunks WHERE document_version_id = :version_id"),
        {"version_id": str(version_id)},
    ).scalar() or 0


def _pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def _json(value: dict[str, object]) -> str:
    import json

    return json.dumps(value)
