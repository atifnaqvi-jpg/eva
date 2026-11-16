"""Synthetic fixture builders for ingestion tests.

The confidentiality charter forbids real customer documents and PII, so every
fixture here is generated at test time from made-up content about a fictional
company. PDFs are written by hand (no extra dependency) using a minimal PDF
writer; DOCX uses python-docx, which is a granted runtime dependency.
"""

import zlib
from io import BytesIO


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(pages: list[list[str]], title: str = "Synthetic Policy") -> bytes:
    """Build a small valid PDF with the standard 14 fonts.

    Each page is a list of lines drawn as one text object.
    """
    objects: list[bytes] = []

    def add(payload: bytes) -> int:
        objects.append(payload)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_ids: list[int] = []
    content_ids: list[int] = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "14 TL", "56 760 Td"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj")
            ops.append("T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", errors="replace")
        compressed = zlib.compress(stream)
        content_ids.append(
            add(
                b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
                + compressed
                + b"\nendstream"
            )
        )
        page_ids.append(0)  # placeholder, patched below

    pages_id = len(objects) + 1 + len(pages)  # +1 for the Pages object slot

    # Now emit the page objects (their parent is pages_id).
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            (
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    actual_pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())

    title_id = add(
        ("<< /Title (" + _pdf_escape(title) + ") /Producer (EVA test fixture) >>").encode()
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {actual_pages_id} 0 R >>".encode())
    info_id = title_id

    out = BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, payload in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + payload + b"\nendobj\n")

    xref_at = out.tell()
    total = len(objects) + 1
    out.write(b"xref\n0 %d\n" % total)
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (total, catalog_id, info_id, xref_at)
    )
    return out.getvalue()


def make_docx(blocks: list[tuple[str, str]]) -> bytes:
    """blocks: list of ("heading1"|"heading2"|"para"|"table", text).

    A "table" text is newline-separated rows of pipe-separated cells.
    """
    import docx

    document = docx.Document()
    for kind, text in blocks:
        if kind == "heading1":
            document.add_heading(text, level=1)
        elif kind == "heading2":
            document.add_heading(text, level=2)
        elif kind == "para":
            document.add_paragraph(text)
        elif kind == "table":
            rows = [r.split("|") for r in text.strip().splitlines()]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for r, cells in enumerate(rows):
                for c, value in enumerate(cells):
                    table.cell(r, c).text = value.strip()
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_html() -> bytes:
    return b"""<!doctype html>
<html><head><title>Synthetic Shipping Policy</title></head>
<body>
<nav><a href="/">Home</a> | <a href="/about">About</a> | <a href="/contact">Contact</a></nav>
<header><h1>Shipping and Delivery</h1></header>
<main>
  <p>Standard shipping arrives in three to five business days within the country.</p>
  <p>Express shipping arrives the next business day when ordered before noon.</p>
  <p>We use cookies to remember your cart between visits.</p>
</main>
<footer>All rights reserved. Copyright 2026 Example Corp.</footer>
</body></html>"""
