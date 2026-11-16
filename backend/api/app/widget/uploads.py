"""Visitor file uploads (WIDG-14, §12.2, R45).

An image or PDF, ≤ 5 MB, type-sniffed server-side, stored tenant-scoped. The scope
is deliberately narrow: this is a support chat, not a file transfer service, and
every format added here is a parser an attacker can aim at.

**Type detection is by magic bytes, never by the filename or the client's
`Content-Type`.** Both are attacker-controlled: `invoice.pdf` can be a polyglot
that opens as HTML in a browser, and a `Content-Type: image/png` header costs the
client nothing to lie about. The extension is recorded for display only, after the
sniffed type has been decided.

**Nothing is parsed here.** The bytes are stored, and that is all: no thumbnailer,
no PDF text extraction on the upload path, no image resize. Parsing untrusted
visitor files is a separate problem in a separate process (WIDG-14's later half and
the M6 fuzzing work), and doing it inline would turn a chat message into an RCE
surface with the API's credentials.

**Storage is the database, not the filesystem.** A tenant-scoped row cannot be
addressed by a guessed path: there is no path traversal to defend against, and the
row is already inside the RLS boundary that the rest of the tenant data lives in.
The trade is that a large attachment set grows the database — acceptable at a 5 MB
cap, and the reason the cap is enforced before a byte is buffered.
"""

from __future__ import annotations

import uuid

# PDF: %PDF- at offset 0. The trailing bytes vary by version, so only the marker is
# checked. `%PDF` inside an HTML page does not match at offset 0.
_PDF = b"%PDF-"

# (mime, magic prefix, max offset to check). PNG, JPEG and GIF are checked at 0;
# WebP needs RIFF....WEBP where the size field sits between, handled below.
_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
)

VISITOR_MIME_BY_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


def sniff(payload: bytes) -> str | None:
    """Return the MIME type the bytes actually are, or ``None`` if not allowed.

    Returning ``None`` rather than raising keeps the caller in charge of the
    status code, and makes "sniffed but rejected" impossible to confuse with
    "sniffed as something harmless".
    """
    if payload.startswith(_PDF):
        return "application/pdf"
    for mime, magic in _SIGNATURES:
        if payload.startswith(magic):
            return mime
    # WebP: "RIFF" + 4-byte little-endian size + "WEBP". Checking the 4-byte size
    # field would be wrong (it varies), so the two fixed markers are compared.
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def looks_like_html(payload: bytes) -> bool:
    """True if the bytes are plausibly HTML/XML/script.

    A separate check because it answers a different question: the format is
    rejected either way, but an HTML-in-PDF polyglot is worth logging as the
    deliberate probe it is rather than as a user error.
    """
    head = payload[:1024].lstrip().lower()
    return head.startswith((b"<html", b"<!doctype", b"<script", b"<?xml", b"<svg"))


class UploadRejected(Exception):
    def __init__(self, code: str, message: str, status_code: int = 415) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def validate(payload: bytes, *, max_bytes: int) -> str:
    """Enforce the cap and sniff the type. Returns the detected MIME."""
    if not payload:
        raise UploadRejected("empty_file", "the file is empty", 400)
    if len(payload) > max_bytes:
        # The route also enforces this while streaming; here it is the backstop for
        # a caller that hands over an already-buffered body.
        raise UploadRejected(
            "file_too_large", f"the limit is {max_bytes} bytes", 413
        )
    mime = sniff(payload)
    if mime is None:
        if looks_like_html(payload):
            raise UploadRejected(
                "unsupported_format",
                "HTML and scripts cannot be uploaded",
            )
        raise UploadRejected(
            "unsupported_format",
            "only PNG, JPEG, GIF, WebP images and PDFs are accepted",
        )
    return mime


def storage_key(*, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    """An opaque storage path. Never derived from the visitor's filename.

    The filename is untrusted text from a stranger; putting it in a path invites
    traversal if storage ever moves to a filesystem, and invites a collision if two
    visitors upload `receipt.pdf`. The key carries only server-chosen ids.
    """
    return f"{tenant_id}/{conversation_id}/{uuid.uuid4().hex}"
