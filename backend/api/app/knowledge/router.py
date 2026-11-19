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
from app.config import MAX_UPLOAD_BYTES
from app.conversation import controls
from app.ingestion.parsers import SUPPORTED_SUFFIXES
from app.ingestion.types import ContentMismatch, CorruptDocument, UnsupportedFormat
from app.ingestion.pipeline import ingest_bytes
from app.knowledge import service as knowledge
from app.retrieval import pipeline as retrieval
from app.retrieval.embedding import default_embedder
from app.retrieval.priority import source_weights

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


class RollbackRequest(BaseModel):
    bot_id: str
    # Accepted only to refuse it. A caller that names a version is asking the old
    # "serve this document alone" operation, which HALT-10 ruled must not exist; silently
    # ignoring the field would answer with a different effect than the one requested.
    version_id: str | None = None


class PreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    version_ids: list[str] | None = None
    limit: int = Field(default=5, ge=1, le=20)
    # Preview may test a candidate floor before an owner changes the bot's.
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class PreviewHit(BaseModel):
    text: str
    score: float
    section: str | None = None
    document: str | None = None
    uri: str | None = None


class PreviewResponse(BaseModel):
    hits: list[PreviewHit]
    version_ids: list[str]
    # A preview is a diagnostic, so it reports *why* it came back empty rather
    # than leaving an owner to guess between "nothing relevant" and "nothing here".
    declined: bool = False
    reason: str | None = None
    best_score: float = 0.0
    threshold: float = 0.0
    reranked: bool = False


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
    except CorruptDocument as exc:
        # 422, not 415: the type is one we accept, the file is simply unreadable. Told
        # apart from "wrong type" because the owner's next action differs — re-export
        # the file, rather than re-export it as a different format.
        raise HTTPException(
            status_code=422,  # deprecated constant alias; the number is the contract
            detail={"error": {"code": "unreadable_document", "message": str(exc)}},
        ) from exc
    except ContentMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": {"code": "content_mismatch",
                              "message": f"{exc}; supported: {', '.join(SUPPORTED_SUFFIXES)}"}},
        ) from exc
    except UnsupportedFormat as exc:
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
def publish(
    user: RequireAdmin, session: ScopedSession, body: PublishRequest
) -> dict[str, object]:
    """Add a version to the bot's live set and swap the pointer (RAG-11).

    Additive by the HALT-07 ruling (PLAN §8.24 RULINGS): the published unit is a set,
    so publishing a second document must not make the first unreachable — that was the
    measured defect (§8.22: a live shipping question declined at 0.4367 against a 0.55
    floor while the content was indexed). The new set inherits the live members, adds
    the named version and keeps one version per document.
    """
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
    if state not in ("ready", "published"):
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

    members = knowledge.resolve_publish_members(
        session, user.tenant_id, bot_id=bot_id, version_ids=[version_id]
    )
    # The edge rollback follows (ADMN-17). Read before the pointer moves, so it names
    # the set the bot was serving at the moment of this publish — not whatever a
    # concurrent publish left behind.
    was_serving = knowledge.active_set(session, user.tenant_id, bot_id)
    set_id = knowledge.create_set(
        session, user.tenant_id, bot_id=bot_id, version_ids=members,
        label="Published", previous_set_id=was_serving,
    )
    knowledge.point_at(session, user.tenant_id, bot_id=bot_id, set_id=set_id)
    session.commit()
    # CONV-15: a cached answer was produced from the *old* knowledge. Keeping it
    # would serve pre-publish answers for up to the TTL, which is exactly the
    # freshness bug the tenant just paid to fix.
    controls.cache().invalidate_tenant(user.tenant_id)
    return {
        "active_knowledge_set_id": str(set_id),
        "active_knowledge_version_ids": [str(v) for v in members],
    }


@router.post("/rollback")
def rollback(
    user: RequireAdmin, session: ScopedSession, body: RollbackRequest
) -> dict[str, object]:
    """Restore the whole set the bot served before the current one (ADMN-17).

    HALT-10's ruling (PLAN §8.47) redefined this endpoint: it used to take a
    ``version_id`` and serve **that document alone**, so an owner with five published
    documents lost four of them with a 200. The label and the effect disagreed, which
    is why ADMN-15 was halted rather than wired up.

    Now it follows the ``previous_set_id`` edge that publish records, and re-points the
    bot at that entire set — same single-column UPDATE as publish, so it is atomic by
    construction (§10.5). ``version_id`` is refused rather than ignored: a caller that
    names a version is asking for the old subset semantics, and answering with a
    different effect would be the defect this row removes, one layer up.
    """
    bot_id = _uuid(body.bot_id, "bot_id")
    if body.version_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "version_id_not_supported",
                    "message": "rollback restores the previously published set; it does "
                               "not take a version_id. Publish a document to serve it.",
                }
            },
        )

    if knowledge.active_bot_exists(session, user.tenant_id, bot_id) is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "bot_not_found", "message": "unknown bot"}},
        )

    try:
        set_id, members = knowledge.rollback_to_previous(
            session, user.tenant_id, bot_id=bot_id
        )
    except knowledge.RollbackRefused as exc:
        # 409, not 404: the bot exists and the request is well formed; the state is
        # what refuses it. The message names which state, because "nothing to restore"
        # and "nothing published at all" send the owner to different places.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": exc.code, "message": exc.message}},
        ) from exc

    session.commit()
    controls.cache().invalidate_tenant(user.tenant_id)
    return {
        "active_knowledge_set_id": str(set_id),
        "active_knowledge_version_ids": [str(v) for v in members],
    }


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
        return PreviewResponse(hits=[], version_ids=[], declined=True, reason="no_versions")

    vector = _embedder().embed([body.question])[0]
    # Priority weights come from the same tenant config the live path reads: a
    # preview that ranked differently from production would be a diagnostic that
    # disagrees with the thing it diagnoses.
    tenant_settings = session.execute(
        text("SELECT settings FROM tenants WHERE id = :t"),
        {"t": str(user.tenant_id)},
    ).scalar()
    config = retrieval.BotRetrievalConfig(
        candidates=max(body.limit, 20), source_weights=source_weights(tenant_settings)
    )
    if body.confidence_threshold is not None:
        config = retrieval.BotRetrievalConfig(
            rerank_enabled=config.rerank_enabled,
            confidence_threshold=body.confidence_threshold,
            candidates=config.candidates,
            source_weights=config.source_weights,
        )
    result = retrieval.retrieve(
        session, user.tenant_id, vector, body.question,
        version_ids=version_ids, config=config,
    )
    citations = retrieval.citations(session, user.tenant_id, result.chunks)
    return PreviewResponse(
        hits=[
            PreviewHit(
                text=chunk.text,
                score=round(chunk.score, 4),
                section=chunk.metadata.get("section"),
                document=citation["document"],
                uri=citation["uri"],
            )
            for chunk, citation in zip(result.chunks, citations, strict=True)
        ],
        version_ids=[str(v) for v in version_ids],
        declined=result.declined,
        reason=result.reason,
        best_score=round(result.best_score, 4),
        threshold=result.threshold,
        reranked=result.reranked,
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
