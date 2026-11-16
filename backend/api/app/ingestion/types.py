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
