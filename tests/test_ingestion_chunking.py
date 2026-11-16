"""KNOW-03 / KNOW-04: cleaning and structure-aware chunking.

Every expectation here traces to a line in ARCHITECTURE 10.2 or 10.5, so a
regression in chunk shape shows up as a failing budget/target assertion rather
than a vague quality complaint.
"""

import re

from app.ingestion.chunking import TARGET_MAX, chunk, faq_chunks
from app.ingestion.clean import clean, content_hash
from app.ingestion.types import Block, BlockKind, ParsedDocument


def _sentences(n: int, start: int = 0) -> str:
    return " ".join(
        f"Sentence number {i} states a distinct provision about returns and refunds for customers."
        for i in range(start, start + n)
    )


def _sentence_numbers(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"Sentence number (\d+)", text)]


# --- cleaner ---------------------------------------------------------------


def test_boilerplate_lines_are_dropped() -> None:
    doc = ParsedDocument(
        blocks=[
            Block(kind=BlockKind.PARAGRAPH, text="Refunds take five business days."),
            Block(kind=BlockKind.PARAGRAPH, text="All rights reserved."),
            Block(kind=BlockKind.PARAGRAPH, text="Copyright 2026 Example Corp."),
            Block(
                kind=BlockKind.PARAGRAPH,
                text="We use cookies to improve your browsing experience. By continuing you agree.",
            ),
            Block(kind=BlockKind.PARAGRAPH, text="42"),
        ]
    )
    kept = [b.text for b in clean(doc).blocks]
    assert kept == ["Refunds take five business days."]


def test_a_sentence_mentioning_cookies_is_not_boilerplate() -> None:
    """Patterns are whole-line and narrow: real content must survive."""
    doc = ParsedDocument(
        blocks=[
            Block(
                kind=BlockKind.PARAGRAPH,
                text="We use cookies to remember your cart, and you can disable them.",
            )
        ]
    )
    assert len(clean(doc).blocks) == 1


def test_duplicate_blocks_collapse_to_one() -> None:
    doc = ParsedDocument(
        blocks=[
            Block(kind=BlockKind.PARAGRAPH, text="Same text."),
            Block(kind=BlockKind.PARAGRAPH, text="same   TEXT."),
            Block(kind=BlockKind.PARAGRAPH, text="Different text."),
        ]
    )
    kept = [b.text for b in clean(doc).blocks]
    assert len(kept) == 2


def test_content_hash_ignores_reflow_and_case() -> None:
    assert content_hash("Hello   world") == content_hash("hello world")


# --- chunker ---------------------------------------------------------------


def test_no_chunk_exceeds_the_token_budget() -> None:
    doc = ParsedDocument(
        blocks=[Block(kind=BlockKind.PARAGRAPH, text=_sentences(200), section=("Big",))]
    )
    chunks = chunk(doc)
    assert len(chunks) > 1
    assert all(c.token_count <= TARGET_MAX for c in chunks)


def test_long_paragraph_splits_on_sentences_and_keeps_overlap() -> None:
    doc = ParsedDocument(
        blocks=[Block(kind=BlockKind.PARAGRAPH, text=_sentences(120), section=("Long",))]
    )
    chunks = chunk(doc)

    first, second = _sentence_numbers(chunks[0].text), _sentence_numbers(chunks[1].text)
    assert len(set(first) & set(second)) > 0, "adjacent chunks must overlap"

    overlap = len(set(first) & set(second))
    ratio = overlap / len(first)
    # ARCHITECTURE 10.2: 10-15% overlap.
    assert 0.08 <= ratio <= 0.20, f"overlap ratio {ratio:.2f} outside band"


def test_split_does_not_lose_or_reorder_content() -> None:
    doc = ParsedDocument(
        blocks=[Block(kind=BlockKind.PARAGRAPH, text=_sentences(120), section=("Long",))]
    )
    chunks = chunk(doc)
    seen: list[int] = []
    for c in chunks:
        for n in _sentence_numbers(c.text):
            if n not in seen:
                seen.append(n)
    assert seen == sorted(seen), "chunk order must follow document order"
    assert seen == list(range(120)), "no sentence may be dropped"


def test_section_breadcrumb_is_prefixed_so_chunks_are_self_describing() -> None:
    doc = ParsedDocument(
        blocks=[
            Block(kind=BlockKind.HEADING, text="Warranty", level=1, section=("Warranty",)),
            Block(
                kind=BlockKind.PARAGRAPH,
                text="Coverage lasts two years.",
                section=("Warranty",),
            ),
        ]
    )
    chunks = chunk(doc)
    assert chunks[0].text.startswith("Warranty")
    assert chunks[0].metadata["section"] == "Warranty"
    assert "Coverage lasts two years." in chunks[0].text


def test_faq_is_one_chunk_per_pair_and_never_split() -> None:
    long_answer = " ".join(["Because the policy says so."] * 200)
    pairs = [
        Block(kind=BlockKind.FAQ, text="Q: Do you ship abroad?\nA: Yes, to 12 countries."),
        Block(kind=BlockKind.FAQ, text=f"Q: Why?\nA: {long_answer}"),
    ]
    chunks = faq_chunks(pairs)

    assert len(chunks) == 2, "one chunk per Q&A pair, no splitting even when long"
    assert chunks[0].metadata["faq"] is True
    assert chunks[1].token_count > TARGET_MAX, "the long answer stays intact"


def test_table_rows_repeat_the_header_in_every_chunk() -> None:
    rows = "\n".join(f"Item {i} | {i} days" for i in range(40))
    block = Block(
        kind=BlockKind.TABLE,
        text=rows,
        header=("Item", "Days"),
        section=("Shipping",),
    )
    chunks = chunk(ParsedDocument(blocks=[block]))

    assert len(chunks) > 1
    for c in chunks:
        assert c.text.startswith("Item | Days"), "every row-group needs its header"
        assert c.metadata["table"] is True


def test_table_chunk_stays_within_budget() -> None:
    rows = "\n".join(f"Very long product name number {i} | {i} days for delivery" for i in range(300))
    block = Block(kind=BlockKind.TABLE, text=rows, header=("Item", "Days"))
    chunks = chunk(ParsedDocument(blocks=[block]))
    assert all(c.token_count <= TARGET_MAX for c in chunks)


def test_ordinals_are_contiguous_from_zero() -> None:
    doc = ParsedDocument(
        blocks=[
            Block(kind=BlockKind.PARAGRAPH, text=_sentences(60), section=("A",)),
            Block(kind=BlockKind.HEADING, text="B", level=1, section=("B",)),
            Block(kind=BlockKind.PARAGRAPH, text=_sentences(60, 60), section=("B",)),
        ]
    )
    chunks = chunk(doc)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_empty_document_yields_no_chunks() -> None:
    assert chunk(ParsedDocument(blocks=[])) == []
