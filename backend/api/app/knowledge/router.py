"""Knowledge admin API (ARCHITECTURE 17.3): upload, crawl, FAQ, list, preview,
publish.

Two rules shape this file:

* **Owner-scoped only.** Every route needs the authenticated tenant from the
  session cookie; the tenant is never read from the request body.
* **Preview reads the draft, the widget reads the published pointer.** Preview is
  how an owner checks a change *before* visitors can see it, so it must never be
  served from the live pointer.
"""

import uuid
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth.dependencies import RequireAdmin, ScopedSession
from app.config import EMBEDDING_DIM, MAX_UPLOAD_BYTES
from app.db.vector_store import PgVectorStore
from app.ingestion.parsers import SUPPORTED_SUFFIXES
from app.ingestion.pipeline import ingest_bytes
from app.knowledge import service as knowledge
from app.retrieval.embedding import default_embedder

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class DocumentSummary(BaseModel):
    id: str
    title: str
    uri: str | None = None
    version_id: str | None = None
    version_no: int | None = None
    status: str | None = None
    error: str | None = None
    chunk_count: int = 0


class UploadResponse(BaseModel):
    document_id: str
    version_id: str
    chunks: int
    status: str


class FaqRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=8000)


class CrawlRequest(BaseModel):
    url: str = Field(pattern=r"^https?://")
    schedule: str = "weekly"


class PublishRequest(BaseModel):
    bot_id: str
    version_id: str


class PreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    version_ids: list[str] | None = None
    limit: int = Field(default=5, ge=1, le=20)


class PreviewHit(BaseModel):
    text: str
    score: float
    section: str | None = None


class PreviewResponse(BaseModel):
    hits: list[PreviewHit]
    version_ids: list[str]


def _embedder():
    """Query embeddings come from the shared embedding service by default.

    The API image does not contain torch, so loading BGE-M3 in-process is not an
    option — and a second copy of a 3.2 GiB model would not fit anyway.
    """
    return default_embedder()


@router.post("/uploads", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload(
    user: RequireAdmin,
    session: ScopedSession,
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
) -> UploadResponse:
    """Upload raw file bytes with the name in ``?filename=``.

    ARCHITECTURE 17.3 describes a multipart upload. Multipart parsing in FastAPI
    needs ``python-multipart``, which is not an approved runtime dependency
    (G3), so this endpoint takes the body directly instead — the bytes are
    identical and no capability is lost. Streaming in chunks bounds memory: a
    client cannot make the API buffer an arbitrarily large body.
    """
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={"error": {"code": "file_too_large",
                                  "message": f"limit is {MAX_UPLOAD_BYTES} bytes"}},
            )
    payload = bytes(payload)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "empty_body", "message": "request body is empty"}},
        )
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": {"code": "file_too_large",
                              "message": f"limit is {MAX_UPLOAD_BYTES} bytes"}},
        )

    try:
        ingested = ingest_bytes(filename, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": {"code": "unsupported_format",
                              "message": f"{exc}; supported: {', '.join(SUPPORTED_SUFFIXES)}"}},
        ) from exc

    source_id = _source_id(session, user.tenant_id, "upload")
    document_id = knowledge.create_document(
        session, user.tenant_id, source_id=source_id,
        title=ingested.title or filename, uri=filename,
    )
    version_id = knowledge.create_version(
        session, user.tenant_id, document_id=document_id, content_hash=ingested.content_hash
    )
    session.execute(
        text("UPDATE document_versions SET filename = :f, payload = :p WHERE id = :v"),
        {"f": filename, "p": payload, "v": str(version_id)},
    )
    knowledge.set_version_status(session, version_id, "processing")
    session.commit()

    # Embedding happens in the worker (BGE-M3 is ~3 GB and ~0.3 chunks/s).
    from app.knowledge.queue import enqueue_ingestion

    enqueue_ingestion(user.tenant_id, version_id, filename)

    return UploadResponse(
        document_id=str(document_id),
        version_id=str(version_id),
        chunks=len(ingested.chunks),
        status="processing",
    )


@router.post("/faqs", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
def create_faq(user: RequireAdmin, session: ScopedSession, body: FaqRequest) -> UploadResponse:
    source_id = _source_id(session, user.tenant_id, "faq")
    document_id, version_id = knowledge.create_faq_version(
        session,
        user.tenant_id,
        source_id=source_id,
        question=body.question,
        answer=body.answer,
        embedder=_embedder(),
    )
    session.commit()
    return UploadResponse(
        document_id=str(document_id),
        version_id=str(version_id),
        chunks=1,
        status="ready",
    )


@router.post("/crawls", status_code=status.HTTP_202_ACCEPTED)
def create_crawl(user: RequireAdmin, session: ScopedSession, body: CrawlRequest) -> dict[str, str]:
    try:
        source_id = knowledge.create_crawl_source(
            session, user.tenant_id, url=body.url, schedule=body.schedule
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "invalid_schedule", "message": str(exc)}},
        ) from exc
    session.commit()

    from app.knowledge.queue import enqueue_crawl

    enqueue_crawl(user.tenant_id, source_id, body.url)
    return {"source_id": str(source_id), "status": "scheduled"}


@router.get("/documents", response_model=list[DocumentSummary])
def list_documents(user: RequireAdmin, session: ScopedSession) -> list[DocumentSummary]:
    rows = knowledge.list_documents(session, user.tenant_id)
    return [
        DocumentSummary(
            id=str(r["id"]),
            title=r["title"],
            uri=r["uri"],
            version_id=str(r["version_id"]) if r["version_id"] else None,
            version_no=r["version_no"],
            status=r["status"],
            error=r["error"],
            chunk_count=r["chunk_count"] or 0,
        )
        for r in rows
    ]


@router.post("/publish")
def publish(user: RequireAdmin, session: ScopedSession, body: PublishRequest) -> dict[str, str]:
    version_id = _uuid(body.version_id, "version_id")
    bot_id = _uuid(body.bot_id, "bot_id")

    if not _version_belongs(session, user.tenant_id, version_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "version_not_found", "message": "unknown version"}},
        )
    # Only a fully ingested version may go live: publishing a half-embedded
    # version would silently degrade answers.
    state = session.execute(
        text("SELECT status FROM document_versions WHERE id = :v"), {"v": str(version_id)}
    ).scalar()
    if state != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "version_not_ready",
                              "message": f"version is {state}, not ready"}},
        )

    if knowledge.active_bot_exists(session, user.tenant_id, bot_id) is False:
        # RLS already hides other tenants' bots; checking explicitly turns a
        # silent no-op UPDATE into a clear 404 instead of a false success.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "bot_not_found", "message": "unknown bot"}},
        )

    knowledge.publish(session, user.tenant_id, bot_id=bot_id, version_id=version_id)
    session.commit()
    return {"active_knowledge_version_id": str(version_id)}


@router.post("/rollback")
def rollback(user: RequireAdmin, session: ScopedSession, body: PublishRequest) -> dict[str, str]:
    """Re-point the bot at an earlier version.

    Deliberately the same operation as publish: the pointer swap is the rollback
    (ARCHITECTURE 10.5), so a rollback needs no special state and cannot diverge
    from a publish. There is exactly one code path that moves the pointer.
    """
    return publish(user, session, body)


@router.post("/preview", response_model=PreviewResponse)
def preview(user: RequireAdmin, session: ScopedSession, body: PreviewRequest) -> PreviewResponse:
    """Answer from the draft — never from the published pointer (ARCHITECTURE 17.3)."""
    if body.version_ids:
        version_ids = [_uuid(v, "version_id") for v in body.version_ids]
    else:
        # Default: every version of this tenant that finished ingesting, published
        # or not, which is exactly "the draft".
        version_ids = [
            row[0]
            for row in session.execute(
                text("SELECT id FROM document_versions WHERE tenant_id = :t AND status = 'ready'"),
                {"t": str(user.tenant_id)},
            )
        ]
    if not version_ids:
        return PreviewResponse(hits=[], version_ids=[])

    vector = _embedder().embed([body.question])[0]
    store = PgVectorStore(session, dimension=EMBEDDING_DIM)
    hits = store.search(user.tenant_id, vector, version_ids=version_ids, limit=body.limit)
    return PreviewResponse(
        hits=[
            PreviewHit(text=h.text, score=round(h.score, 4), section=h.metadata.get("section"))
            for h in hits
        ],
        version_ids=[str(v) for v in version_ids],
    )


def _source_id(session, tenant_id: uuid.UUID, source_type: str) -> uuid.UUID:
    existing = session.execute(
        text("SELECT id FROM knowledge_sources WHERE tenant_id = :t AND type = :ty LIMIT 1"),
        {"t": str(tenant_id), "ty": source_type},
    ).scalar()
    if existing:
        return existing
    return session.execute(
        text("INSERT INTO knowledge_sources (tenant_id, type) VALUES (:t, :ty) RETURNING id"),
        {"t": str(tenant_id), "ty": source_type},
    ).scalar_one()


def _uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "invalid_uuid", "message": f"{field} must be a UUID"}},
        ) from exc


def _version_belongs(session, tenant_id: uuid.UUID, version_id: uuid.UUID) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM document_versions WHERE id = :v AND tenant_id = :t"),
            {"v": str(version_id), "t": str(tenant_id)},
        ).scalar()
        is not None
    )
