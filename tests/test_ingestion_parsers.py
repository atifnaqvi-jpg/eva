"""KNOW-02: per-type parsers on synthetic fixtures only (confidentiality charter)."""

import pytest

from fixtures_ingest import make_docx, make_html, make_pdf


def test_pdf_text_and_title() -> None:
    from app.ingestion.parsers import parse

    pdf = make_pdf(
        [
            ["Warranty Terms", "Coverage lasts two years from purchase."],
            ["Exclusions", "Damage from misuse is not covered."],
        ],
        title="Warranty Policy",
    )
    doc = parse("policy.pdf", pdf)

    assert doc.title == "Warranty Policy"
    text = doc.text()
    assert "Coverage lasts two years" in text
    assert "Damage from misuse is not covered" in text
    # Page numbers are preserved: M2 citations quote document + section + page.
    assert {b.page for b in doc.blocks} == {1, 2}


def test_pdf_tables_never_lose_page_text(monkeypatch) -> None:
    """pdfplumber failures must degrade to text, not fail the ingestion."""
    from app.ingestion import parsers

    def boom(_payload):
        raise RuntimeError("table layer unavailable")

    monkeypatch.setattr(parsers, "_pdf_tables", boom)
    doc = parsers.parse("policy.pdf", make_pdf([["Still readable text here."]]))
    assert "Still readable text here" in doc.text()


def test_docx_headings_carry_section_breadcrumbs() -> None:
    from app.ingestion.parsers import parse

    docx = make_docx(
        [
            ("heading1", "Returns"),
            ("para", "Returns are accepted within thirty days."),
            ("heading2", "Exclusions"),
            ("para", "Worn items cannot be returned."),
        ]
    )
    doc = parse("returns.docx", docx)

    assert doc.title == "Returns"
    sections = [b.section for b in doc.blocks if b.kind.value == "paragraph"]
    assert sections[0] == ("Returns",)
    assert sections[1] == ("Returns", "Exclusions")


def test_docx_table_becomes_a_table_block_with_header() -> None:
    from app.ingestion.parsers import parse

    docx = make_docx(
        [("heading1", "Windows"), ("table", "Item|Days\nShoes|30\nHats|14")]
    )
    doc = parse("t.docx", docx)

    tables = [b for b in doc.blocks if b.kind.value == "table"]
    assert len(tables) == 1
    assert tables[0].header == ("Item", "Days")
    assert "Shoes | 30" in tables[0].text


def test_markdown_headings_become_structure() -> None:
    from app.ingestion.parsers import parse

    doc = parse("a.md", b"# Title\n\nBody one.\n\n## Sub\n\nBody two.\n")
    kinds = [(b.kind.value, b.level, b.text) for b in doc.blocks]
    assert kinds[0] == ("heading", 1, "Title")
    assert kinds[2] == ("heading", 2, "Sub")
    assert doc.blocks[3].section == ("Title", "Sub")


def test_html_main_content_extracted_without_nav_or_footer() -> None:
    from app.ingestion.parsers import parse_html_bytes

    doc = parse_html_bytes(make_html())
    text = doc.text()

    assert doc.title == "Shipping and Delivery"
    assert "three to five business days" in text
    assert "All rights reserved" not in text, "footer boilerplate must be dropped"
    assert "About" not in text, "navigation must be dropped"


def test_unsupported_format_is_explicit() -> None:
    from app.ingestion.parsers import parse
    from app.ingestion.types import UnsupportedFormat

    with pytest.raises(UnsupportedFormat) as excinfo:
        parse("deck.pptx", b"PK\x03\x04")
    # PPTX is deferred by ruling, so the message must name what *is* supported.
    assert ".pdf" in str(excinfo.value)


def test_deferred_formats_are_not_registered() -> None:
    """pandas/python-pptx are deliberately not installed (HALT-02 ruling 1A)."""
    from app.ingestion.parsers import SUPPORTED_SUFFIXES

    assert ".pptx" not in SUPPORTED_SUFFIXES
    assert ".csv" not in SUPPORTED_SUFFIXES
    assert ".xlsx" not in SUPPORTED_SUFFIXES
