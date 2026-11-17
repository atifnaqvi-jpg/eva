"""Shared ingestion data types.

A parser turns bytes into ``ParsedDocument`` — structure preserved (headings,
paragraphs, tables) rather than one flat string, because chunking quality comes
from document structure (ARCHITECTURE 10.2).
"""

from dataclasses import dataclass, field
from enum import Enum


class BlockKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FAQ = "faq"


@dataclass(frozen=True)
class Block:
    """One structural unit of a document."""

    kind: BlockKind
    text: str
    # Heading depth (1 = top level). 0 for non-headings.
    level: int = 0
    # Heading breadcrumb this block belongs to, e.g. ["Warranty", "Exclusions"].
    section: tuple[str, ...] = ()
    # Page number for PDFs, "row group N" for tables, None otherwise.
    page: int | None = None
    # For TABLE blocks: header cells, repeated into every row-group chunk.
    header: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedDocument:
    blocks: list[Block] = field(default_factory=list)
    # Human-readable title if the format carries one (DOCX/HTML heading).
    title: str | None = None

    def text(self) -> str:
        """Whole-document plain text, for hashing and previews."""
        return "\n\n".join(b.text for b in self.blocks if b.text.strip())


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    token_count: int
    metadata: dict[str, object] = field(default_factory=dict)


class UnsupportedFormat(ValueError):
    """Raised when no parser is registered for a file type."""


class CorruptDocument(ValueError):
    """Raised when the bytes claim a format but cannot be read as it.

    A truncated or malformed PDF is the *client's* error, so it must reach them as
    a 4xx with a reason rather than as a 500. ``ValueError`` for the same reason as
    ``UnsupportedFormat``: callers already translate that to a refusal, and a
    parser raising it keeps the decision of which status code to send in the router.
    """


class ContentMismatch(ValueError):
    """Raised when the bytes are not the format the filename claims (SEC-07).

    The suffix is attacker-controlled. Dispatching a parser on it alone means an
    ELF binary named ``notes.txt`` is ingested as text and can be retrieved later
    as an "answer", so the bytes are also checked: a text suffix must be decodable
    text, a PDF must start with ``%PDF-``, a DOCX must be a ZIP.
    """
