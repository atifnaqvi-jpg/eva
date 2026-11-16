"""Allowlist validation for the widget's appearance settings (WIDG-09, §12.2).

`bots.appearance` is tenant-supplied JSONB that ends up rendering **inside a third
party's page**. That makes it a different kind of input from anything else a tenant
stores: a value that is harmless in the dashboard (an `avatarUrl`, a `primaryColor`)
is an injection point once the loader and the frame apply it, and the tenant is not
the only party affected — the host page is.

So the widget path serves a *normalised* appearance: only known keys, only validated
values, and anything unrecognised is dropped rather than passed through. A tenant who
writes `{"primaryColor": "red;}</style><script>…"}` gets the default colour, not a
broken or hostile page.

Two rules worth stating:

* **Colour is not free-form CSS.** It is `#rgb`, `#rrggbb`, or a bare `rgb(...)`/
  `hsl(...)` triple. Named colours and `var()` are rejected: names are an unbounded
  surface and `var()` lets one tenant's setting reach into whatever the host page
  defines.
* **Avatar and header text are bounded.** The avatar must be an absolute `https:` URL
  (a `data:` URI would let a tenant put arbitrary bytes in an `<img>`, and `http:`
  would break mixed-content rules on every realistic host page). Text is length
  capped and control characters stripped.
"""

from __future__ import annotations

import re

MAX_TEXT = 60
MAX_URL = 500

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_HSL = re.compile(
    r"^(?:rgb|hsl)\(\s*\d{1,3}\s*,\s*\d{1,3}%?\s*,\s*\d{1,3}%?\s*\)$",
    re.IGNORECASE,
)
_POSITIONS = {"bottom-right", "bottom-left"}

DEFAULT_PRIMARY = "#1f6feb"
DEFAULT_POSITION = "bottom-right"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Control characters would let a name smuggle a newline into a header or a
    # `\u202e` reorder the visible string; both are visible only to the visitor.
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned or len(cleaned) > MAX_TEXT:
        return None
    return cleaned


def _https_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_URL:
        return None
    if not value.lower().startswith("https://"):
        return None
    # Reject credentials and whitespace: both are ways to make the displayed URL
    # differ from the effective one.
    if "@" in value or any(ch.isspace() for ch in value):
        return None
    return value


def _colour(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if _HEX.match(candidate) or _RGB_HSL.match(candidate):
        return candidate
    return None


def normalise(raw: object) -> dict[str, str]:
    """Return only the appearance keys the widget may apply, validated.

    Always returns a usable dict: the widget renders defaults when a key is absent,
    so a tenant whose value was rejected simply sees the default rather than an
    error page inside someone else's site.
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, str] = {}

    colour = _colour(raw.get("primaryColor"))
    if colour:
        out["primaryColor"] = colour

    position = raw.get("position")
    if isinstance(position, str) and position in _POSITIONS:
        out["position"] = position

    avatar = _https_url(raw.get("avatarUrl"))
    if avatar:
        out["avatarUrl"] = avatar

    header = _text(raw.get("headerText"))
    if header:
        out["headerText"] = header

    return out
