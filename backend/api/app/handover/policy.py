"""Handover policy: triggers, tenant configuration and business hours (§13).

Two rules shape everything here:

* **The tenant's settings are read, never trusted.** They live in ``tenants.settings``
  (a JSONB policy blob, per §16.1 — see ``conversation.controls.monthly_budget`` for
  the same reasoning), so any key can be missing or the wrong type. Every accessor
  here returns a usable value or the documented default, never raising.
* **A trigger says "a human should see this", not "the AI failed".** Low-confidence
  retrieval already declines with an answer (RAG-07); this module decides whether
  that decline *also* notifies the owner, which is a separate decision with a
  separate failure mode (an SMTP outage must not turn a refusal into an error).
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"

# §13.1's fourth trigger. Deliberately a short, boring default: a tenant who wants
# "speak to a person" to escalate should not have to discover an option to make it.
DEFAULT_KEYWORDS = ("human", "agent", "representative", "someone real", "real person")

# A week of `{ "mon": ["09:00", "17:00"], ... }`; a day absent from the map is closed.
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def settings(tenant_settings: Any) -> dict[str, Any]:
    """Normalise the handover-related slice of ``tenants.settings``.

    Tolerant by construction: this value is operator-supplied JSON, and a malformed
    blob must degrade to defaults rather than break a visitor's conversation.
    """
    raw = tenant_settings if isinstance(tenant_settings, dict) else {}
    handover = raw.get("handover")
    handover = handover if isinstance(handover, dict) else {}

    email = handover.get("email")
    email = email.strip() if isinstance(email, str) and email.strip() else None

    keywords = handover.get("keywords")
    if isinstance(keywords, list):
        cleaned = [k.strip().lower() for k in keywords if isinstance(k, str) and k.strip()]
    else:
        cleaned = []
    # An explicit empty list means "no keyword trigger"; only a missing key falls
    # back to the defaults, so a tenant can switch this off without a special value.
    if "keywords" not in handover:
        cleaned = list(DEFAULT_KEYWORDS)

    return {
        "email": email,
        "keywords": cleaned,
        # Notify on a below-threshold decline. Off by default: a business with a
        # small knowledge base would otherwise get an email per unanswerable
        # question, which trains them to ignore the channel (§13 noise control).
        "notify_on_low_confidence": bool(handover.get("notify_on_low_confidence", False)),
        "notify_on_negative_feedback": bool(
            handover.get("notify_on_negative_feedback", True)
        ),
    }


def hours(tenant_settings: Any) -> dict[str, Any]:
    """Business hours + timezone (R43), or ``{"configured": False}`` when unset.

    ``{"timezone": "Europe/Berlin", "week": {"mon": ["09:00", "17:00"], ...}}``.
    An unconfigured tenant is treated as always online: inventing hours would show
    an offline banner to a business that never asked for one.
    """
    raw = tenant_settings if isinstance(tenant_settings, dict) else {}
    block = raw.get("business_hours")
    if not isinstance(block, dict):
        return {"configured": False}

    week: dict[str, tuple[time, time]] = {}
    raw_week = block.get("week")
    if isinstance(raw_week, dict):
        for day, span in raw_week.items():
            if day not in _WEEKDAYS or not isinstance(span, list) or len(span) != 2:
                continue
            start, end = (_parse_hhmm(span[0]), _parse_hhmm(span[1]))
            if start is None or end is None or start >= end:
                continue
            week[day] = (start, end)

    tz_name = block.get("timezone")
    tz_name = tz_name if isinstance(tz_name, str) else DEFAULT_TIMEZONE
    return {"configured": True, "timezone": tz_name, "week": week}


def is_online(tenant_settings: Any, *, now: datetime | None = None) -> bool:
    """Whether the tenant is inside its business hours right now.

    A tenant with no hours configured is online. A tenant with hours configured but
    *no valid entries* is offline — they asked for a schedule and gave an unusable
    one, and showing the offline message is the safer reading of that.
    """
    config = hours(tenant_settings)
    if not config["configured"]:
        return True
    week = config["week"]
    if not week:
        return False

    tz = _zoneinfo(config["timezone"])
    moment = (now or datetime.now(timezone.utc)).astimezone(tz)
    span = week.get(_WEEKDAYS[moment.weekday()])
    if span is None:
        return False
    start, end = span
    return start <= moment.time().replace(tzinfo=None) < end


def next_open(tenant_settings: Any, *, now: datetime | None = None) -> datetime | None:
    """When the tenant next opens, for the offline message ("back Monday 09:00").

    Returns ``None`` when no schedule is configured. Searches a fortnight ahead,
    which is enough for any weekly pattern and cheap enough to run per session.
    """
    config = hours(tenant_settings)
    # `hours()` omits `week` entirely when nothing is configured, so reading it
    # directly raised on every tenant without a schedule.
    week = config.get("week") or {}
    if not week:
        return None
    tz = _zoneinfo(config["timezone"])
    moment = (now or datetime.now(timezone.utc)).astimezone(tz)
    for offset in range(14):
        day = moment.date() + timedelta(days=offset)
        span = week.get(_WEEKDAYS[day.weekday()])
        if span is None:
            continue
        candidate = datetime.combine(day, span[0], tzinfo=tz)
        if candidate > moment:
            return candidate
    return None


def keyword_match(text: str, keywords: list[str]) -> bool:
    """Whole-word, case-insensitive keyword trigger.

    Substring matching would fire "agent" inside "management" and "human" inside
    "humanity"; for a channel that emails a stranger's transcript to a business,
    false positives are the expensive error.
    """
    if not keywords:
        return False
    lowered = text.lower()
    for word in keywords:
        needle = word.strip().lower()
        if not needle:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", lowered):
            return True
    return False


def _parse_hhmm(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    match = _HHMM.match(value.strip())
    if match is None:
        return None
    return time(hour=int(match.group(1)), minute=int(match.group(2)))


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # An unknown zone is a configuration mistake; UTC keeps the widget usable
        # instead of 500ing every session.
        return ZoneInfo(DEFAULT_TIMEZONE)
