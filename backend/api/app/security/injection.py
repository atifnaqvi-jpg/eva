"""Prompt-injection detection for visitor messages (SEC-05, ARCHITECTURE §19.2).

Two things this module is *not*:

* It is not the defence. The structural defence lives in ``conversation.prompt``
  — nonce delimiters, the untrusted-data reminder stated after the content, and no
  tool definitions anywhere. Detection is a *second* layer that decides, cheaply and
  before any spend, that this message should not be answered at all.
* It is not a classifier. It is a small set of high-specificity patterns. A pattern
  here must not fire on ordinary support traffic ("how do I ignore a previous
  order?"), because a false positive refuses a paying customer's real question.

Specificity is bought by requiring an *object* to the verb: "ignore previous
instructions" matches, "ignore previous order" does not.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# The kinds of object a jailbreak tries to displace. Requiring one of these after
# a displacement verb is what keeps "disregard the packaging" from matching.
_INSTRUCTION_OBJECT = (
    r"(?:instructions?|rules?|prompts?|directions?|guidelines?|guardrails?|"
    r"restrictions?|programming|polic(?:y|ies)|constraints?|training)"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            rf"\b(?:ignore|disregard|forget|bypass|override|overrule)\b"
            rf"(?:\s+\w+){{0,4}}?\s+{_INSTRUCTION_OBJECT}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "references_prior_instructions",
        # Catches the attempt even when the *verb* is disguised ("Ign<U+0456>re", "1gnore",
        # "ign0re"): the phrase "previous/prior/above instructions" has no benign reading in
        # a support question, so it carries the signal on its own. Measured: none of the 25
        # benign corpus cases contains it.
        re.compile(
            r"\b(?:previous|prior|above|earlier|preceding|foregoing|original)\s+"
            r"(?:(?:system|safety|hidden)\s+)?(?:instructions?|prompts?|rules?|directives?)\b"
            r"|\ball\s+(?:the\s+)?(?:previous|prior|above)\s+\w+",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        # Only vocabulary that cannot be a business word. "What are your rules for
        # returns?" is a real question and was a measured false positive when `rules`
        # and `guidelines` were in this list; they are policy words, not prompt words.
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|display|tell\s+me|what\s+(?:is|are))\b"
            r"(?:\s+\w+){0,4}?\s+(?:your|the)\s+"
            r"(?:system\s+prompt|initial\s+prompt|hidden\s+prompt|"
            r"prompt\s+text|pre-?prompt|"
            r"(?:original|initial|internal|hidden|secret)\s+(?:instructions?|rules?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "persona_override",
        # `act as a/an` is deliberately absent: "act as a translator for my message" is
        # a measured legitimate request. These forms all assert a *replacement* identity.
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on\s+you(?:\s+are|\s+will)|"
            r"pretend\s+(?:to\s+be|you\s+are)|roleplay\s+as|"
            r"imagine\s+you\s+are|your\s+new\s+(?:role|identity|persona))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unrestricted_mode",
        re.compile(
            r"\b(?:developer\s+mode|jailbreak|dan\s+mode|do\s+anything\s+now|"
            r"no\s+restrictions?|unrestricted\s+(?:mode|assistant)|"
            r"without\s+(?:any\s+)?(?:restrictions?|limits|filters))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration_probe",
        re.compile(
            r"\b(?:list|show|dump|print|reveal)\b(?:\s+\w+){0,4}?\s+"
            r"(?:other\s+(?:customers?|tenants?|users?)|"
            r"all\s+(?:customers?|tenants?|users?|conversations?)|"
            r"api\s+keys?|credentials?|secrets?|passwords?|tokens?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_probe",
        # The "what is the secret api key" shape has no reveal verb, so the pattern
        # above misses it. Bare `password` is excluded on purpose: "how do I reset my
        # password" is ordinary support traffic. Only platform-side secret nouns.
        re.compile(
            r"\b(?:secret|private|internal|master|admin)\s+(?:key|keys|token|tokens)\b"
            r"|\bapi\s+keys?\b"
            r"|\baccess\s+tokens?\b"
            r"|\benv(?:ironment)?\s+variables?\b",
            re.IGNORECASE,
        ),
    ),
)

# Zero-width and bidirectional control characters are how an instruction hides in
# plain sight. They carry no meaning for a support question, so removing them is
# lossless for the visitor and fatal for the disguise.
_INVISIBLE = frozenset(
    "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\ufeff"
)


@dataclass(frozen=True)
class Detection:
    """Which pattern fired, and the normalised text it fired on."""

    pattern: str
    text: str


# Letters from other scripts that render as Latin ones. NFKC does *not* fold these:
# Cyrillic "і" (U+0456) is a distinct letter, not a compatibility variant of "i", so an
# attacker writing "Ign<U+0456>re previous instructions" defeated NFKC alone — measured.
_CONFUSABLES = str.maketrans({
    # Cyrillic
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0443": "y", "\u0445": "x", "\u0456": "i", "\u0457": "i", "\u0458": "j",
    "\u0455": "s", "\u0461": "w", "\u04bb": "h", "\u0501": "d", "\u051b": "q",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M",
    "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T",
    "\u0425": "X", "\u0406": "I", "\u0408": "J", "\u0405": "S",
    # Greek
    "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03bd": "v",
    "\u03bf": "o", "\u03c1": "p", "\u03c4": "t", "\u03c5": "u", "\u03c7": "x",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H",
    "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O",
    "\u03a1": "P", "\u03a4": "T", "\u03a5": "Y", "\u03a7": "X",
})


def normalise(text: str) -> str:
    """Fold away the characters an attacker uses to break up keywords.

    Three steps, because each defeats a different disguise: NFKC for compatibility
    forms and full-width characters, the confusable map for cross-script lookalikes,
    and the zero-width strip for text that is split mid-word.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_CONFUSABLES)
    without_invisible = "".join(ch for ch in folded if ch not in _INVISIBLE)
    # Collapse runs of whitespace so "ignore\n\nprevious" reads as one phrase.
    return re.sub(r"\s+", " ", without_invisible).strip()


def scan_message(text: str) -> Detection | None:
    """Return the first pattern that fires, or ``None`` for ordinary traffic."""
    if not text or not text.strip():
        return None
    normalised = normalise(text)
    for name, pattern in _PATTERNS:
        if pattern.search(normalised):
            return Detection(pattern=name, text=normalised)
    return None


def strip_invisibles(text: str) -> str:
    """Remove zero-width/bidi characters, keeping the rest of the text verbatim."""
    return "".join(ch for ch in text if ch not in _INVISIBLE)
