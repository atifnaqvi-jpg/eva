"""Knowledge ingestion, versioning, snapshot sets and preview (WP-06/WP-08/RAG-11).

Two models live here and they are deliberately not the same thing:

* every ingestion appends a ``document_versions`` row; chunks hang off that row, and
  nothing mutates a published version in place
* **a bot serves a knowledge set** — a snapshot naming several document versions at
  once (``knowledge_sets`` + ``knowledge_set_members``). HALT-07's OPTION 2 (PLAN
  §8.24 RULINGS) is the reason: with one ``active_knowledge_version_id`` publishing a
  second document silently made the first unreachable, so "publish" was not additive.

``bots.active_knowledge_set_id`` is the only served pointer, so a publish is still a
single-row UPDATE — atomic by construction (§10.5) — and rollback is the same swap
pointing at an earlier set. A set that is not the pointer is a draft, which is what
gives ADMN-05's preview its two-column meaning without a per-version status flag.
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


def create_set(
    session: Session,
    tenant_id: UUID,
    *,
    bot_id: UUID,
    version_ids: list[UUID],
    label: str | None = None,
) -> UUID:
    """Store one snapshot naming ``version_ids``.

    Membership rows carry ``tenant_id`` so the table is fail-closed under RLS like
    every other tenant table (§16.3).
    """
    set_id = session.execute(
        text(
            "INSERT INTO knowledge_sets (tenant_id, bot_id, label)"
            " VALUES (:tenant_id, :bot_id, :label) RETURNING id"
        ),
        {"tenant_id": str(tenant_id), "bot_id": str(bot_id), "label": label},
    ).scalar_one()
    for version_id in dict.fromkeys(version_ids):
        session.execute(
            text(
                "INSERT INTO knowledge_set_members"
                "       (tenant_id, set_id, document_version_id)"
                " VALUES (:tenant_id, :set_id, :version_id)"
            ),
            {
                "tenant_id": str(tenant_id),
                "set_id": str(set_id),
                "version_id": str(version_id),
            },
        )
    return set_id


def point_at(session: Session, tenant_id: UUID, *, bot_id: UUID, set_id: UUID) -> UUID:
    """The one served pointer, moved in a single UPDATE.

    Concurrent publishes cannot interleave into a half-swapped state; the last writer
    wins, which is the intended semantics (§10.5).
    """
    session.execute(
        text(
            "UPDATE bots SET active_knowledge_set_id = :set_id"
            " WHERE id = :bot_id AND tenant_id = :tenant_id"
        ),
        {"set_id": str(set_id), "bot_id": str(bot_id), "tenant_id": str(tenant_id)},
    )
    return set_id


def publish(session: Session, tenant_id: UUID, *, bot_id: UUID, version_id: UUID) -> UUID:
    """Publish one version as a one-member set.

    Kept for callers that think in versions; the router resolves the additive case
    (publish alongside what is already live) before calling :func:`create_set`.
    """
    set_id = create_set(
        session, tenant_id, bot_id=bot_id, version_ids=[version_id],
        label="Published version",
    )
    return point_at(session, tenant_id, bot_id=bot_id, set_id=set_id)


def active_set(session: Session, tenant_id: UUID, bot_id: UUID) -> UUID | None:
    return session.execute(
        text("SELECT active_knowledge_set_id FROM bots"
             " WHERE id = :bot_id AND tenant_id = :tenant_id"),
        {"bot_id": str(bot_id), "tenant_id": str(tenant_id)},
    ).scalar()


def set_members(session: Session, tenant_id: UUID, set_id: UUID) -> list[UUID]:
    return [
        row[0]
        for row in session.execute(
            text(
                "SELECT document_version_id FROM knowledge_set_members"
                " WHERE set_id = :set_id AND tenant_id = :tenant_id"
                " ORDER BY document_version_id"
            ),
            {"set_id": str(set_id), "tenant_id": str(tenant_id)},
        )
    ]


def newest_version_per_document(
    session: Session, tenant_id: UUID, version_ids: list[UUID]
) -> list[UUID]:
    """Collapse versions to one per document, keeping the highest ``version_no``.

    A publish that merely unioned would serve the superseded and the superseding
    version of the same document side by side, so an owner editing a page could be
    answered from the text they just replaced. That is a correctness rule, not a
    tidy-up: the set is a snapshot of documents, and one document has one live text.
    """
    if not version_ids:
        return []
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (v.document_id) v.id
              FROM document_versions v
             WHERE v.tenant_id = :tenant_id
               AND v.id = ANY(:versions)
             ORDER BY v.document_id, v.version_no DESC
            """
        ),
        {"tenant_id": str(tenant_id), "versions": [str(v) for v in version_ids]},
    )
    return [row[0] for row in rows]


def resolve_publish_members(
    session: Session, tenant_id: UUID, *, bot_id: UUID, version_ids: list[UUID]
) -> list[UUID]:
    """The members a publish produces, given the versions the caller named.

    The ruling's unit of publication is a set, so a publish of one document must not
    silently drop the documents already being served — that was the HALT-07 defect
    one layer down. Named versions are added to the live set and the result is
    collapsed per document.
    """
    current = active_set(session, tenant_id, bot_id)
    inherited = set_members(session, tenant_id, current) if current else []
    return newest_version_per_document(
        session, tenant_id, inherited + list(version_ids)
    )


def active_bot_exists(session: Session, tenant_id: UUID, bot_id: UUID) -> bool:
    return session.execute(
        text("SELECT 1 FROM bots WHERE id = :b AND tenant_id = :t"),
        {"b": str(bot_id), "t": str(tenant_id)},
    ).scalar() is not None


def active_version(session: Session, tenant_id: UUID, bot_id: UUID) -> UUID | None:
    """The single served version, or ``None`` when the live set holds several.

    A compatibility view over the set pointer (RAG-11): screens and callers that can
    only express "one document is live" get an answer, and a multi-document set gets
    ``None`` rather than an arbitrary member. Retrieval never uses this — it reads the
    whole set (:func:`app.retrieval.pipeline.active_version_ids`).
    """
    set_id = active_set(session, tenant_id, bot_id)
    if set_id is None:
        return None
    members = set_members(session, tenant_id, set_id)
    return members[0] if len(members) == 1 else None


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


# --- FAQ entries (KNOW-06) --------------------------------------------------


def create_faq_version(
    session: Session,
    tenant_id: UUID,
    *,
    source_id: UUID,
    question: str,
    answer: str,
    embedder: EmbeddingProvider,
) -> tuple[UUID, UUID]:
    """Store a manual Q&A pair as a one-chunk version.

    ARCHITECTURE 10.2 wants one chunk per Q&A pair; representing the pair as its
    own document+version keeps that true without special-casing retrieval, and it
    means editing an FAQ creates a new version rather than mutating a published one.
    """
    from app.ingestion.chunking import faq_chunks
    from app.ingestion.pipeline import IngestedDocument
    from app.ingestion.types import Block, BlockKind

    text = f"Q: {question.strip()}\nA: {answer.strip()}"
    pair = Block(kind=BlockKind.FAQ, text=text, section=(question.strip(),))
    ingested = IngestedDocument(
        title=question.strip(),
        content_hash=_content_hash(text),
        chunks=faq_chunks([pair]),
    )

    document_id = create_document(
        session, tenant_id, source_id=source_id, title=question.strip()[:200]
    )
    version_id = create_version(
        session, tenant_id, document_id=document_id, content_hash=ingested.content_hash
    )
    store_chunks(
        session, tenant_id, version_id=version_id, ingested=ingested, embedder=embedder
    )
    set_version_status(session, version_id, "ready")
    return document_id, version_id


def _content_hash(text: str) -> str:
    from app.ingestion.clean import content_hash

    return content_hash(text)


# --- crawl (KNOW-05) -------------------------------------------------------


def create_crawl_source(
    session: Session, tenant_id: UUID, *, url: str, schedule: str = "weekly"
) -> UUID:
    if schedule not in ("manual", "daily", "weekly", "monthly"):
        raise ValueError(f"unsupported crawl schedule: {schedule}")
    import json

    return session.execute(
        text(
            """
            INSERT INTO knowledge_sources (tenant_id, type, config)
            VALUES (:tenant_id, 'crawl', CAST(:config AS jsonb))
            RETURNING id
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "config": json.dumps({"url": url, "schedule": schedule}),
        },
    ).scalar_one()


def list_documents(session: Session, tenant_id: UUID) -> list[dict[str, object]]:
    """Documents with their latest version status, for the admin list endpoint."""
    rows = session.execute(
        text(
            """
            SELECT d.id, d.title, d.uri,
                   v.id AS version_id, v.version_no, v.status, v.error,
                   (SELECT count(*) FROM chunks c WHERE c.document_version_id = v.id)
                       AS chunk_count
              FROM documents d
              LEFT JOIN LATERAL (
                   SELECT id, version_no, status, error
                     FROM document_versions
                    WHERE document_id = d.id
                    ORDER BY version_no DESC
                    LIMIT 1
              ) v ON true
             WHERE d.tenant_id = :tenant_id
             ORDER BY d.created_at DESC
            """
        ),
        {"tenant_id": str(tenant_id)},
    ).mappings()
    return [dict(r) for r in rows]


def upsert_document(
    session: Session, tenant_id: UUID, *, source_id: UUID, title: str, uri: str
) -> UUID:
    """Reuse the document for ``uri`` so a recrawl versions it instead of cloning it."""
    existing = session.execute(
        text(
            "SELECT id FROM documents WHERE tenant_id = :t AND uri = :u LIMIT 1"
        ),
        {"t": str(tenant_id), "u": uri},
    ).scalar()
    if existing:
        session.execute(
            text("UPDATE documents SET title = :title WHERE id = :id"),
            {"title": title, "id": str(existing)},
        )
        return existing
    return create_document(session, tenant_id, source_id=source_id, title=title, uri=uri)


def version_chunk_count(session: Session, version_id: UUID) -> int:
    return session.execute(
        text("SELECT count(*) FROM chunks WHERE document_version_id = :v"),
        {"v": str(version_id)},
    ).scalar_one()
