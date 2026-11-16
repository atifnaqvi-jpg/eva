"""Cleaning and de-duplication (KNOW-03, ARCHITECTURE 10.1/10.5).

Two jobs, both deliberately conservative — losing real content is worse than
ingesting a little boilerplate:

* **Boilerplate removal** — page furniture that repeats across a site (cookie
  banners, "All rights reserved", nav/copyright lines). Matching is on whole
  lines against tight patterns, so a paragraph that merely *mentions* "cookies"
  survives.
* **De-duplication** — identical blocks collapse to one, and a whole document
  that hashes to content already stored is skipped upstream. The hash is
  normalised (whitespace collapsed, lowercase) so reflowed text still matches.
"""

import hashlib
import re

from app.ingestion.types import Block, BlockKind, ParsedDocument

# Whole-line patterns, anchored. Kept narrow on purpose.
_BOILERPLATE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^all rights reserved\.?$",
        r"^©.*$",
        r"^copyright\s+©?.*$",
        r"^cookie(s)? (policy|notice|settings?)[.!]?$",
        # Consent-banner language only. "We use cookies to remember your cart,
        # and you can disable them" is real content and must survive, so the
        # match keys on banner vocabulary rather than on the topic.
        r"\bwe use cookies\b.*\b(agree|accept|consent|experience|browsing)\b.*$",
        r"^this (web)?site uses cookies\b.*$",
        r"^by (continuing|using this (web)?site)\b.*$",
        r"^(privacy|terms)( policy| of service| and conditions)?[.!]?$",
        r"^(share|tweet|subscribe|sign up for our newsletter)\b.*$",
        r"^(skip to (main )?content|back to top)[.!]?$",
        r"^(home|about|contact|blog|careers|login|sign in)(\s*[/|]\s*\w+)*$",
        r"^\d+$",  # bare page numbers
    )
]

_MIN_BLOCK_CHARS = 2


def is_boilerplate(text: str) -> bool:
    stripped = " ".join(text.split())
    return any(p.match(stripped) for p in _BOILERPLATE)


def clean(doc: ParsedDocument) -> ParsedDocument:
    """Drop boilerplate and exact-duplicate blocks, preserving order and structure."""
    seen: set[str] = set()
    kept: list[Block] = []

    for block in doc.blocks:
        text = " ".join(block.text.split())
        if len(text) < _MIN_BLOCK_CHARS:
            continue
        if block.kind is not BlockKind.TABLE and block.kind is not BlockKind.FAQ:
            if is_boilerplate(text):
                continue
        fingerprint = _fingerprint(text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        kept.append(
            Block(
                kind=block.kind,
                text=text if block.kind is not BlockKind.TABLE else block.text,
                level=block.level,
                section=block.section,
                page=block.page,
                header=block.header,
            )
        )

    return ParsedDocument(blocks=kept, title=doc.title)


def _fingerprint(text: str) -> str:
    normalised = " ".join(text.split()).lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """Stable hash of whole-document text, for skipping unchanged re-ingestion."""
    normalised = " ".join(text.split()).lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
