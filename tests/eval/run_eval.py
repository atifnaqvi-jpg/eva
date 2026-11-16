"""Run the golden set through the real retrieval pipeline.

This is the piece CI calls. It seeds a throwaway tenant with the golden documents,
then asks every question through ``retrieval.pipeline.retrieve`` — the same
function the API calls, with the same filters — and scores the result.

The seeded corpus is synthetic (confidentiality charter): four invented policy
documents for a fictional retailer, no real customer content.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import psycopg

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import TENANT_SETTING
from app.retrieval import pipeline
from app.retrieval.embedding import EmbeddingProvider, HashingEmbedder
from app.db.vector_store import PgVectorStore
from app.retrieval.store import VectorRecord

from harness import GoldenSet, Metrics, score

DIM = 1024


@dataclass
class Seeded:
    tenant_id: uuid.UUID
    version_id: uuid.UUID
    chunk_slug_by_id: dict[str, uuid.UUID]


def seed(session: Session, golden: GoldenSet, *, admin_dsn: str,
         embedder: EmbeddingProvider | None = None) -> Seeded:
    """Insert the golden corpus as one published version.

    Structural rows are written on a superuser connection because RLS is FORCE-enabled
    on every tenant table and a tenant cannot bootstrap itself. Vectors go through the
    production ``PgVectorStore.upsert`` on the app session, so the path under test is
    the real one.
    """
    embedder = embedder or HashingEmbedder(dimension=DIM)
    tenant_id = uuid.uuid4()
    source_id = uuid.uuid4()
    version_id = uuid.uuid4()
    slug_to_id: dict[str, uuid.UUID] = {}
    pairs: list[tuple[uuid.UUID, str]] = []

    with psycopg.connect(admin_dsn) as admin:
        admin.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)",
            (tenant_id, golden.tenant_name),
        )
        admin.execute(
            "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'upload')",
            (source_id, tenant_id),
        )
        for doc_index, doc in enumerate(golden.documents):
            document_id = uuid.uuid4()
            admin.execute(
                "INSERT INTO documents (id, tenant_id, source_id, title)"
                " VALUES (%s,%s,%s,%s)",
                (document_id, tenant_id, source_id, doc["title"]),
            )
            if doc_index == 0:
                # One version per corpus is enough: the golden set tests ranking,
                # not versioning (WP-09 covers that).
                admin.execute(
                    "INSERT INTO document_versions (id, tenant_id, document_id,"
                    " version_no, content_hash, status) VALUES (%s,%s,%s,1,'golden','ready')",
                    (version_id, tenant_id, document_id),
                )
            for ordinal, chunk in enumerate(doc["chunks"]):
                chunk_id = uuid.uuid4()
                slug_to_id[chunk["id"]] = chunk_id
                admin.execute(
                    "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal,"
                    " text, metadata) VALUES (%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        chunk_id, tenant_id, version_id, ordinal, chunk["text"],
                        json.dumps({"section": chunk["section"], "ordinal": ordinal,
                                    "slug": chunk["id"], "document": doc["title"]}),
                    ),
                )
                pairs.append((chunk_id, chunk["text"]))
        admin.commit()

    session.info[TENANT_SETTING] = str(tenant_id)
    session.execute(
        text("SELECT set_config(:n, :t, true)"), {"n": TENANT_SETTING, "t": str(tenant_id)}
    )
    store = PgVectorStore(session, dimension=DIM)
    store.upsert(
        tenant_id,
        [VectorRecord(chunk_id=cid, document_version_id=version_id, text=body,
                      embedding=embedder.embed([body])[0]) for cid, body in pairs],
    )
    session.commit()
    return Seeded(tenant_id, version_id, slug_to_id)


def run(
    session: Session,
    golden: GoldenSet,
    seeded: Seeded,
    *,
    embedder: EmbeddingProvider | None = None,
    threshold: float = 0.16,
    reranker=None,
) -> tuple[dict[str, tuple[list[str], bool]], Metrics]:
    """Ask every golden question through the published retrieval path."""
    embedder = embedder or HashingEmbedder(dimension=DIM)
    session.info[TENANT_SETTING] = str(seeded.tenant_id)
    slug_by_id = {cid: slug for slug, cid in seeded.chunk_slug_by_id.items()}
    config = pipeline.BotRetrievalConfig(
        confidence_threshold=threshold, rerank_enabled=reranker is not None
    )

    results: dict[str, tuple[list[str], bool]] = {}
    for case in golden.cases:
        vector = embedder.embed([case.question])[0]
        result = pipeline.retrieve(
            session, seeded.tenant_id, vector, case.question,
            version_ids=[seeded.version_id], config=config, reranker=reranker,
        )
        ranked = [slug_by_id[c.chunk_id] for c in result.chunks if c.chunk_id in slug_by_id]
        results[case.id] = (ranked, result.declined)

    return results, score(golden.cases, results)


def cleanup(session: Session, seeded: Seeded, *, admin_dsn: str) -> None:
    session.rollback()
    with psycopg.connect(admin_dsn) as admin:
        admin.execute("DELETE FROM tenants WHERE id = %s", (seeded.tenant_id,))
        admin.commit()
