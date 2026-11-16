"""Structure-aware chunking (KNOW-04, ARCHITECTURE 10.2).

Recursive: split on document structure first (headings/sections), then
paragraphs, then sentences, targeting 400-800 estimated tokens with 10-15%
overlap. The three special cases the architecture calls out:

* **FAQ** — one chunk per Q&A pair, never split (``faq_chunks``).
* **Tables** — row-groups with the header repeated in every chunk, because a row
  without its header is unreadable to a retriever.
* **Overlap** — the tail sentences of one chunk seed the next, so a fact split
  across the boundary is still recoverable.

Every chunk carries its section breadcrumb and page, which is what makes the
citation payload in M2 possible.
"""

import re

from app.ingestion.tokens import count_tokens
from app.ingestion.types import Block, BlockKind, Chunk, ParsedDocument

TARGET_MIN = 400
TARGET_MAX = 800
# 12%: inside the 10-15% band the architecture specifies.
OVERLAP_RATIO = 0.12
_MAX_ROWS_PER_TABLE_CHUNK = 12

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def chunk(doc: ParsedDocument, *, target_min: int = TARGET_MIN, target_max: int = TARGET_MAX) -> list[Chunk]:
    """Chunk a cleaned document into retrieval units."""
    if not doc.blocks:
        return []

    if all(b.kind is BlockKind.FAQ for b in doc.blocks):
        return faq_chunks(doc.blocks)

    units: list[Chunk] = []
    buffer: list[Block] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if buffer:
            units.extend(_blocks_to_chunks(buffer, target_min, target_max))
            buffer = []
            buffer_tokens = 0

    for block in doc.blocks:
        if block.kind is BlockKind.HEADING:
            # A heading closes the current section; it is also prepended to the
            # following chunk's text (see _blocks_to_chunks) so chunks are
            # self-describing when retrieved alone.
            flush()
            buffer.append(block)
            continue

        if block.kind is BlockKind.TABLE:
            flush()
            units.extend(_table_chunks(block, target_min, target_max))
            continue

        block_tokens = count_tokens(block.text)
        if buffer and buffer_tokens + block_tokens > target_max:
            flush()
            buffer.append(block)
            buffer_tokens = block_tokens
        elif block_tokens > target_max:
            flush()
            units.extend(_split_oversized(block, target_max))
        else:
            buffer.append(block)
            buffer_tokens += block_tokens

    flush()
    return _renumber(units)


def _blocks_to_chunks(blocks: list[Block], target_min: int, target_max: int) -> list[Chunk]:
    """Join sibling blocks into one chunk, prefixing the active heading breadcrumb."""
    prose = [b for b in blocks if b.kind is BlockKind.PARAGRAPH]
    if not prose:
        return []

    section = prose[0].section
    page = next((b.page for b in prose if b.page is not None), None)
    body = "\n\n".join(b.text for b in prose)
    prefix = _prefix(section)
    text = f"{prefix}\n\n{body}" if prefix else body

    if count_tokens(text) <= target_max:
        return [_make_chunk(text, section, page)]

    # Too big after merging: fall back to per-block, then sentence splitting.
    out: list[Chunk] = []
    for block in prose:
        candidate = f"{prefix}\n\n{block.text}" if prefix else block.text
        if count_tokens(candidate) <= target_max:
            out.append(_make_chunk(candidate, block.section, block.page))
        else:
            out.extend(_split_oversized(block, target_max, prefix=prefix))
    return out


def _split_oversized(block: Block, target_max: int, *, prefix: str = "") -> list[Chunk]:
    """Sentence-window split with overlap, for a single over-long paragraph."""
    sentences = [s.strip() for s in _SENTENCE.split(block.text) if s.strip()]
    if not sentences:
        return []

    chunks: list[Chunk] = []
    current: list[str] = []
    for sentence in sentences:
        trial = " ".join(current + [sentence])
        if current and count_tokens(f"{prefix}\n\n{trial}" if prefix else trial) > target_max:
            body = " ".join(current)
            text = f"{prefix}\n\n{body}" if prefix else body
            chunks.append(_make_chunk(text, block.section, block.page))
            current = _overlap_tail(current, target_max)
            current.append(sentence)
        else:
            current.append(sentence)
    if current:
        body = " ".join(current)
        text = f"{prefix}\n\n{body}" if prefix else body
        chunks.append(_make_chunk(text, block.section, block.page))
    return chunks


def _overlap_tail(sentences: list[str], target_max: int) -> list[str]:
    """Keep the trailing sentences worth ~OVERLAP_RATIO of a chunk."""
    budget = max(1, int(target_max * OVERLAP_RATIO))
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        tokens = count_tokens(sentence)
        if total + tokens > budget:
            break
        tail.insert(0, sentence)
        total += tokens
    return tail


def _table_chunks(block: Block, target_min: int, target_max: int) -> list[Chunk]:
    """Row-groups, header repeated in every chunk (ARCHITECTURE 10.2).

    Rows are accumulated until the group reaches ``target_min`` or adding the
    next row would exceed ``target_max``. Grouping by the minimum keeps a table
    from producing a swarm of tiny chunks; the maximum is a hard cap.
    """
    header = " | ".join(block.header) if block.header else ""
    rows = [r for r in block.text.splitlines() if r.strip()]
    if not rows:
        return []

    out: list[Chunk] = []
    group: list[str] = []

    def emit() -> None:
        body = "\n".join(([header] if header else []) + group)
        out.append(_make_chunk(body, block.section, block.page, extra={"table": True}))

    for row in rows:
        candidate = "\n".join(([header] if header else []) + group + [row])
        if group and count_tokens(candidate) > target_max:
            emit()
            group = [row]
            continue
        group.append(row)
        if count_tokens("\n".join(([header] if header else []) + group)) >= target_min:
            emit()
            group = []
        elif len(group) >= _MAX_ROWS_PER_TABLE_CHUNK:
            emit()
            group = []
    if group:
        emit()
    return out


def faq_chunks(pairs: list[Block]) -> list[Chunk]:
    """One chunk per Q&A pair — never split, even if long (ARCHITECTURE 10.2)."""
    return [
        _make_chunk(pair.text, pair.section, None, extra={"faq": True})
        for pair in pairs
        if pair.text.strip()
    ]


def _prefix(section: tuple[str, ...]) -> str:
    return " > ".join(section)


def _make_chunk(
    text: str,
    section: tuple[str, ...],
    page: int | None,
    extra: dict[str, object] | None = None,
) -> Chunk:
    metadata: dict[str, object] = {}
    if section:
        metadata["section"] = " > ".join(section)
    if page is not None:
        metadata["page"] = page
    if extra:
        metadata.update(extra)
    return Chunk(
        ordinal=0,
        text=text.strip(),
        token_count=count_tokens(text),
        metadata=metadata,
    )


def _renumber(chunks: list[Chunk]) -> list[Chunk]:
    return [
        Chunk(
            ordinal=i,
            text=c.text,
            token_count=c.token_count,
            metadata=c.metadata,
        )
        for i, c in enumerate(c for c in chunks if c.text.strip())
    ]
