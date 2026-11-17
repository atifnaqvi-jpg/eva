"""Per-type parsers (KNOW-02).

Each parser preserves document structure — headings, paragraphs, tables — because
the chunker in ``chunking.py`` splits on structure first (ARCHITECTURE 10.2).

Scope per the HALT-02 ruling: PDF (pypdf + pdfplumber), Word (python-docx),
plain text/markdown, and HTML (trafilatura). PPTX and CSV/XLSX are deferred —
``pandas``/``python-pptx`` are deliberately not installed yet.
"""

import re
from dataclasses import replace
from io import BytesIO
from pathlib import PurePath

from app.ingestion.types import (
    Block,
    BlockKind,
    ContentMismatch,
    CorruptDocument,
    ParsedDocument,
    UnsupportedFormat,
)
from app.security import injection

# --- PDF --------------------------------------------------------------------


def _pdf(payload: bytes) -> ParsedDocument:
    """Text per page via pypdf; tables via pdfplumber where they are cheap.

    Table extraction is attempted but never required: pdfplumber is much slower
    than pypdf and fails on many scan-like PDFs, so a failure degrades to plain
    text rather than failing the ingestion.
    """
    from pypdf import PdfReader

    blocks: list[Block] = []
    try:
        reader = PdfReader(BytesIO(payload))
    except Exception as exc:
        # pypdf raises its own exception types (PdfStreamError and friends), none of
        # which is a ValueError, so without this a truncated upload reached the client
        # as a 500 "Internal Server Error" — measured against the live stack.
        raise CorruptDocument("this file is not a readable PDF") from exc

    for page_no, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        for para in _split_paragraphs(text):
            blocks.append(Block(kind=BlockKind.PARAGRAPH, text=para, page=page_no))

    try:
        blocks.extend(_pdf_tables(payload))
    except Exception:
        # Table extraction is a bonus, never a requirement: a broken table layer
        # must not lose the page text already parsed above.
        pass

    title = None
    meta_title = (reader.metadata or {}).get("/Title")
    if isinstance(meta_title, str) and meta_title.strip():
        title = meta_title.strip()
    return ParsedDocument(blocks=blocks, title=title)


def _pdf_tables(payload: bytes) -> list[Block]:
    import pdfplumber

    found: list[Block] = []
    with pdfplumber.open(BytesIO(payload)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if len(table) < 2 or len(table[0]) < 2:
                    continue
                header = tuple((c or "").strip() for c in table[0])
                rows = [
                    " | ".join((c or "").strip() for c in row)
                    for row in table[1:]
                    if any((c or "").strip() for c in row)
                ]
                if rows:
                    found.append(
                        Block(
                            kind=BlockKind.TABLE,
                            text="\n".join(rows),
                            header=header,
                            page=page.page_number,
                        )
                    )
    return found


# --- Word -------------------------------------------------------------------


def _docx(payload: bytes) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(BytesIO(payload))
    blocks: list[Block] = []
    section: list[str] = []
    title: str | None = None

    for item in _iter_docx_blocks(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            style = (item.style.name or "").lower()
            if style.startswith("heading"):
                level = _heading_level(style)
                section = section[: level - 1] + [text]
                if title is None:
                    title = text
                blocks.append(
                    Block(
                        kind=BlockKind.HEADING,
                        text=text,
                        level=level,
                        section=tuple(section),
                    )
                )
            else:
                blocks.append(
                    Block(kind=BlockKind.PARAGRAPH, text=text, section=tuple(section))
                )
        elif isinstance(item, Table):
            grid = [[(c.text or "").strip() for c in row.cells] for row in item.rows]
            if len(grid) < 2 or not any(grid[0]):
                continue
            header = tuple(grid[0])
            rows = [
                " | ".join(cells) for cells in grid[1:] if any(cells)
            ]
            if rows:
                blocks.append(
                    Block(
                        kind=BlockKind.TABLE,
                        text="\n".join(rows),
                        header=header,
                        section=tuple(section),
                    )
                )

    return ParsedDocument(blocks=blocks, title=title)


def _heading_level(style_name: str) -> int:
    for token in style_name.replace("heading", " ").split():
        if token.isdigit():
            return max(1, int(token))
    return 1


def _iter_docx_blocks(document):
    """Yield paragraphs and tables in true document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


# --- plain text / markdown --------------------------------------------------

_MD_HEADING = r"^(#{1,6})\s+(.*)$"


def _text(payload: bytes) -> ParsedDocument:
    """Markdown-aware: ``#`` headings become structure, everything else prose."""
    import re

    raw = payload.decode("utf-8", errors="replace")
    blocks: list[Block] = []
    section: list[str] = []
    buffer: list[str] = []
    title: str | None = None

    def flush() -> None:
        if buffer:
            joined = " ".join(buffer).strip()
            if joined:
                blocks.append(
                    Block(kind=BlockKind.PARAGRAPH, text=joined, section=tuple(section))
                )
            buffer.clear()

    for line in raw.splitlines():
        match = re.match(_MD_HEADING, line.strip())
        if match:
            flush()
            level = len(match.group(1))
            text = match.group(2).strip()
            section = section[: level - 1] + [text]
            if title is None:
                title = text
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=text,
                    level=level,
                    section=tuple(section),
                )
            )
        elif not line.strip():
            flush()
        else:
            buffer.append(line.strip())
    flush()
    return ParsedDocument(blocks=blocks, title=title)


# --- HTML -------------------------------------------------------------------


def _html(payload: bytes) -> ParsedDocument:
    """Main-content extraction via trafilatura (drops nav/footer boilerplate)."""
    import trafilatura
    from lxml import html as lxml_html

    raw = payload.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        raw, include_comments=False, include_tables=True, favor_recall=False
    )

    title: str | None = None
    try:
        doc = lxml_html.fromstring(raw)
        heading = doc.find(".//h1")
        if heading is not None and heading.text_content().strip():
            title = heading.text_content().strip()
    except Exception:
        title = None

    blocks: list[Block] = []
    if extracted:
        for para in _split_paragraphs(extracted):
            blocks.append(Block(kind=BlockKind.PARAGRAPH, text=para))
    else:
        # trafilatura returns None when it thinks there is no main content; fall
        # back to the visible text so a short page still ingests.
        try:
            doc = lxml_html.fromstring(raw)
            for node in doc.xpath("//script|//style|//nav|//footer"):
                node.getparent().remove(node)
            for para in _split_paragraphs(doc.text_content()):
                blocks.append(Block(kind=BlockKind.PARAGRAPH, text=para))
        except Exception:
            pass
    return ParsedDocument(blocks=blocks, title=title)


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    buffer: list[str] = []
    for part in parts:
        if part:
            buffer.append(part)
        elif buffer:
            out.append(" ".join(buffer))
            buffer = []
    if buffer:
        out.append(" ".join(buffer))
    return [p for p in out if p]


# --- dispatch ---------------------------------------------------------------

_PARSERS = {
    ".pdf": _pdf,
    ".docx": _docx,
    ".txt": _text,
    ".md": _text,
    ".markdown": _text,
    ".html": _html,
    ".htm": _html,
}

SUPPORTED_SUFFIXES = tuple(sorted(_PARSERS))


def parse(filename: str, payload: bytes) -> ParsedDocument:
    """Parse ``payload`` using the parser for ``filename``'s suffix, after checking
    the bytes actually are that format (SEC-07).

    The suffix is attacker-controlled and the parser for it decides what the file is,
    so a binary named ``notes.txt`` would otherwise be ingested as text and could be
    served back as an answer. The check is cheap and runs before any parser: it reads
    the first bytes, not the whole payload.
    """
    suffix = PurePath(filename).suffix.lower()
    parser = _PARSERS.get(suffix)
    if parser is None:
        raise UnsupportedFormat(
            f"no parser for {suffix or 'extensionless file'}; "
            f"supported: {', '.join(SUPPORTED_SUFFIXES)}"
        )
    _check_bytes_match_suffix(suffix, payload)
    return _sanitise(parser(payload))


# How each suffix is allowed to start. Text formats have no signature, so they are
# checked for the opposite property instead: that they decode as text at all.
_BINARY_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
}

# Formats whose bytes are text.
_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".html", ".htm"})

# Enough to cover every signature above; reading more would just buffer the file.
_PROBE = 8

# Control bytes that never appear in prose but do appear in binary headers. This is
# wider than a NUL check because a NUL-only test missed a measured case: PNG magic is
# ``\x89PNG\r\n\x1a\n`` — no NUL in the first eight bytes — so a PNG named
# ``notes.md`` was accepted. Tab/newline/form-feed are legitimate; everything else in
# C0 and DEL is not. High bytes are deliberately allowed: a Windows-1252 export is
# still text, and rejecting it would refuse a real owner's file to catch no attack.
_BINARY_CONTROLS = frozenset(
    byte for byte in range(0x20) if byte not in (0x09, 0x0A, 0x0C, 0x0D)
) | {0x7F}


def _check_bytes_match_suffix(suffix: str, payload: bytes) -> None:
    signatures = _BINARY_SIGNATURES.get(suffix)
    if signatures is not None:
        if not payload.startswith(signatures):
            raise ContentMismatch(
                f"a {suffix} file was expected, but these bytes are not a {suffix[1:].upper()}"
            )
        return
    if suffix in _TEXT_SUFFIXES:
        head = payload[:_PROBE]
        if any(byte in _BINARY_CONTROLS for byte in head):
            raise ContentMismatch(
                f"a {suffix} file was expected, but these bytes are binary, not text"
            )


# --- SEC-06: active content never reaches the knowledge base -----------------

# Tags whose *content* is not prose. trafilatura already drops them on the main-content
# path, but the fallback path does not, and a PDF or DOCX can carry markup in a text run.
# Matching the serialised text is the one layer common to every parser.
_ACTIVE_MARKUP = re.compile(
    r"<\s*(?:script|iframe|object|embed|style)\b[^>]*>.*?</\s*(?:script|iframe|object|embed|style)\s*>"
    r"|<\s*(?:script|iframe|object|embed|link|meta)\b[^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)

# A javascript:/data:/vbscript: URI is the payload of a markdown link. Nothing serves
# stored bytes back today, so this is not a live XSS fix; it is the blueprint's
# "strip active content at parse" holding for every parser, not just the one the
# author happened to be thinking about.
_ACTIVE_URI = re.compile(r"(?:javascript|vbscript|data)\s*:\s*[^\s)\"'>]+", re.IGNORECASE)

def _sanitise(doc: ParsedDocument) -> ParsedDocument:
    """Strip active content and invisible characters from every block (SEC-06)."""
    cleaned: list[Block] = []
    for block in doc.blocks:
        text = _ACTIVE_MARKUP.sub(" ", block.text)
        text = _ACTIVE_URI.sub("[removed link]", text)
        text = injection.strip_invisibles(text)
        if text.strip():
            cleaned.append(replace(block, text=text))
    return replace(doc, blocks=cleaned)


def parse_html_bytes(payload: bytes) -> ParsedDocument:
    """Crawler entry point: HTML bytes from a URL have no filename suffix."""
    return _sanitise(_html(payload))
