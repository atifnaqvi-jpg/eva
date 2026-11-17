"""Per-tenant retention for conversations (SEC-09, §19.3).

ARCHITECTURE 19.3: *"Retention: configurable per tenant, default 90 days for
conversations, enforced by a scheduled purge job."*

Three rules shape this module:

* **The default is the promise, not a starting point.** A tenant that never opens the
  settings screen still has the 90 days we told their visitors about, so a missing or
  unreadable blob resolves to :data:`DEFAULT_RETENTION_DAYS` rather than to "keep
  forever". Retention is a privacy commitment, so the failure mode has to be deletion.
* **A setting is only trusted once it is bounded.** ``tenants.settings`` is JSON an
  operator can edit, so a value is validated on write and re-validated on read: a
  hand-edited ``0`` would otherwise look like "keep nothing", and a string would make
  the purge query mean something nobody intended.
* **Retention is a floor on keeping, not a target.** The floor is one day: a tenant may
  choose short retention but not "delete immediately", which would empty an active
  conversation out from under the person using it.

The purge itself lives in the worker (``worker/purge_job.py``), because it is a
scheduled job. This module holds only the decisions, so the job and the settings screen
agree on what the number means.
"""

from __future__ import annotations

from typing import Any

DEFAULT_RETENTION_DAYS = 90

# Below this the setting stops being retention and becomes data loss for a live
# conversation, so it is refused rather than honoured.
MIN_RETENTION_DAYS = 1

# An upper bound exists so a mistyped "9000" is refused instead of silently promising
# to keep data forever: this is a data-minimisation control, and "forever" is not one
# of the choices a tenant is offered.
MAX_RETENTION_DAYS = 730


def settings(tenant_settings: Any) -> dict[str, Any]:
    """Normalise the retention slice of ``tenants.settings``.

    Tolerant like the other policy readers: an unreadable blob degrades to the default
    rather than raising, and the purge always gets a number it can act on.
    """
    raw = tenant_settings if isinstance(tenant_settings, dict) else {}
    block = raw.get("retention")
    block = block if isinstance(block, dict) else {}

    days = block.get("conversation_days", DEFAULT_RETENTION_DAYS)
    # `bool` is an `int` subclass in Python, and `True` as a retention period is a
    # mistake worth refusing rather than treating as one day.
    if isinstance(days, bool) or not isinstance(days, int):
        days = DEFAULT_RETENTION_DAYS
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        days = DEFAULT_RETENTION_DAYS

    return {"conversation_days": days}


def validate(patch: Any) -> dict[str, int]:
    """Validate a requested change, raising ``ValueError`` with a usable message.

    The reader is deliberately forgiving, which is exactly why the writer is not: a
    value the reader would silently replace must be refused here, or the owner saves a
    number and the screen then shows a different one.
    """
    if not isinstance(patch, dict):
        raise ValueError("retention must be an object")
    unknown = set(patch) - {"conversation_days"}
    if unknown:
        raise ValueError(f"unknown retention setting: {', '.join(sorted(unknown))}")

    if "conversation_days" not in patch:
        raise ValueError("conversation_days is required")

    days = patch["conversation_days"]
    if isinstance(days, bool) or not isinstance(days, int):
        raise ValueError("conversation_days must be a whole number of days")
    if days < MIN_RETENTION_DAYS:
        raise ValueError(
            f"conversation_days must be at least {MIN_RETENTION_DAYS}; "
            "shorter retention would delete conversations people are still having"
        )
    if days > MAX_RETENTION_DAYS:
        raise ValueError(
            f"conversation_days must be at most {MAX_RETENTION_DAYS}; "
            "longer retention is not offered because this is a data-minimisation control"
        )
    return {"conversation_days": days}
