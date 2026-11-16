"""Parse -> clean -> chunk, with no database or embedding concerns.

Kept separate from the worker so the whole transformation is testable as a pure
function of bytes (see ``tests/test_ingestion_*.py``).
"""

from dataclasses import dataclass

from app.ingestion.chunking import chunk as chunk_document
from app.ingestion.clean import clean, content_hash
from app.ingestion.parsers import parse, parse_html_bytes
from app.ingestion.types import Chunk, ParsedDocument


@dataclass(frozen=True)
class IngestedDocument:
    title: str | None
    content_hash: str
    chunks: list[Chunk]


def ingest_bytes(filename: str, payload: bytes) -> IngestedDocument:
    return _finish(parse(filename, payload))


def ingest_html(payload: bytes) -> IngestedDocument:
    return _finish(parse_html_bytes(payload))


def ingest_block_document(doc: ParsedDocument) -> IngestedDocument:
    return _finish(doc)


def _finish(parsed: ParsedDocument) -> IngestedDocument:
    cleaned = clean(parsed)
    chunks = chunk_document(cleaned)
    return IngestedDocument(
        title=cleaned.title,
        content_hash=content_hash(cleaned.text()),
        chunks=chunks,
    )
