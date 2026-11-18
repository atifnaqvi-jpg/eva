"""Cost and abuse controls: budgets, rate limits, response cache (CONV-14, CONV-15, R36).

Three controls that all answer "how much of this tenant's money can a stranger
spend", but they fail at different timescales and that is why they are separate:

* **Rate limits** (seconds) stop one visitor hammering the widget. Per-visitor-token,
  per-IP and per-tenant, because a single abusive visitor and a botnet are different
  problems: one is a bad actor, the other is a bill, and a script that mints a fresh
  `visitor_session_id` per request defeats the visitor key while sharing one IP.
* **Budgets** (a month) stop a tenant overrunning the plan. The stop is hard — the
  turn is refused with the offline message, not silently downgraded to a cheaper
  model, because a cheaper model changes answer quality and the tenant did not
  agree to that.
* **Response cache** (minutes) is a cost control that is also a quality control:
  the same normalised question within a tenant gets the same answer, so two
  visitors asking "how long is shipping?" do not get two different numbers.

All three are in-process and process-local. That is the honest state for one `api`
container; the seam for horizontal scale is these functions, and the note is in
``PLAN 8.4.5`` rather than left to be discovered.

**Fixed-window rate limiting, not sliding.** A fixed window admits up to 2x the
limit across a boundary. For a support widget that is acceptable — the cost of the
edge case is a few extra LLM calls, while a sliding window means storing a timestamp
list per visitor, which grows without bound and needs its own eviction. Named so the
trade is visible rather than accidental.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# --- rate limiting -----------------------------------------------------------

VISITOR_REQUESTS_PER_MINUTE = 10
TENANT_REQUESTS_PER_MINUTE = 120
# One IP is often a whole office or a mobile carrier NAT, so this is looser than the
# visitor limit: it exists to stop a script minting a new visitor_session_id per
# request, not to police shared addresses.
IP_REQUESTS_PER_MINUTE = 60
USER_REQUESTS_PER_MINUTE = 60

_WINDOW_SECONDS = 60.0


@dataclass
class RateLimiter:
    """Fixed-window counters keyed by (scope, identity, window)."""

    visitor_limit: int = VISITOR_REQUESTS_PER_MINUTE
    tenant_limit: int = TENANT_REQUESTS_PER_MINUTE
    ip_limit: int = IP_REQUESTS_PER_MINUTE
    user_limit: int = USER_REQUESTS_PER_MINUTE
    window_seconds: float = _WINDOW_SECONDS
    _counts: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)
    _window_start: dict[tuple[str, str], float] = field(
        default_factory=dict, repr=False
    )
    # A threading lock, not asyncio's: the critical section is a couple of dict
    # operations, and an asyncio lock would bind the limiter to one event loop —
    # the process has several (TestClient, uvicorn) and the counter is shared.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _hit(self, scope: str, identity: str, limit: int, now: float) -> tuple[bool, int]:
        key = (scope, identity)
        started = self._window_start.get(key, 0.0)
        if now - started >= self.window_seconds:
            self._window_start[key] = now
            self._counts[key] = 0
        self._counts[key] = self._counts.get(key, 0) + 1
        count = self._counts[key]
        return count <= limit, max(0, limit - count)

    def check(
        self,
        *,
        tenant_id: UUID,
        visitor_session_id: str,
        ip: str | None = None,
    ) -> None:
        """Raise ``RateLimited`` if any scope is over its limit (R36, SEC-02).

        Every scope is counted even when an earlier one trips, because a check that
        short-circuits would let one abusive visitor be invisible in the tenant's
        count — and then the first thing the tenant sees is a bill, not an alarm.

        The IP scope is what makes the visitor scope non-gameable: a script can send
        a fresh ``visitor_session_id`` every request, but it cannot shed its address.
        """
        now = time.monotonic()
        with self._lock:
            tenant_ok, tenant_left = self._hit(
                "tenant", str(tenant_id), self.tenant_limit, now
            )
            visitor_ok, visitor_left = self._hit(
                "visitor", f"{tenant_id}:{visitor_session_id}",
                self.visitor_limit, now,
            )
            ip_ok, ip_left = (
                self._hit("ip", ip, self.ip_limit, now) if ip else (True, 0)
            )
        if not visitor_ok:
            raise RateLimited(
                "visitor_rate_limited",
                retry_after=int(self.window_seconds),
                remaining=visitor_left,
            )
        if ip is not None and not ip_ok:
            raise RateLimited(
                "ip_rate_limited",
                retry_after=int(self.window_seconds),
                remaining=ip_left,
            )
        if not tenant_ok:
            raise RateLimited(
                "tenant_rate_limited",
                retry_after=int(self.window_seconds),
                remaining=tenant_left,
            )

    def check_admin(self, *, user_id: UUID) -> None:
        """Per-user limit for the dashboard's own endpoints (SEC-02).

        Per-user rather than per-tenant: one tenant can have several dashboards
        open, and one owner's runaway script should not lock out their colleague.
        """
        now = time.monotonic()
        with self._lock:
            ok, left = self._hit("user", str(user_id), self.user_limit, now)
        if not ok:
            raise RateLimited(
                "user_rate_limited",
                retry_after=int(self.window_seconds),
                remaining=left,
            )

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._window_start.clear()


class RateLimited(Exception):
    def __init__(self, code: str, *, retry_after: int, remaining: int) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.remaining = remaining


_limiter = RateLimiter()


def limiter() -> RateLimiter:
    return _limiter


# --- budgets -----------------------------------------------------------------

DEFAULT_MONTHLY_TOKEN_BUDGET = 1_000_000


class BudgetExceeded(Exception):
    def __init__(self, *, used: int, budget: int) -> None:
        super().__init__("monthly_token_budget_exceeded")
        self.used = used
        self.budget = budget


def monthly_token_usage(session: Session, *, tenant_id: UUID) -> int:
    """Tokens charged to this tenant in the current calendar month.

    Read from ``usage_records`` rather than a counter on ``tenants``: the ledger is
    already the source of truth for cost (R31), and a denormalised counter would
    drift the first time a write failed.
    """
    return int(
        session.execute(
            text(
                """
                SELECT COALESCE(SUM(units), 0)
                  FROM usage_records
                 WHERE tenant_id = :tenant_id
                   AND kind = 'chat'
                   AND created_at >= date_trunc('month', now())
                """
            ),
            {"tenant_id": str(tenant_id)},
        ).scalar_one()
    )


def monthly_budget(session: Session, *, tenant_id: UUID) -> int:
    """Per-tenant budget, defaulting when the tenant has not set one.

    Read from ``tenants.settings`` rather than a new column: the §16.1 schema is
    frozen and this is a policy value, not a structural one. A migration for one
    JSONB key would be churn.
    """
    value = session.execute(
        text(
            "SELECT settings -> 'monthly_token_budget' FROM tenants"
            " WHERE id = :tenant_id"
        ),
        {"tenant_id": str(tenant_id)},
    ).scalar()
    try:
        return int(value) if value is not None else DEFAULT_MONTHLY_TOKEN_BUDGET
    except (TypeError, ValueError):
        return DEFAULT_MONTHLY_TOKEN_BUDGET


def check_budget(session: Session, *, tenant_id: UUID) -> None:
    """Hard stop before the turn spends anything (CONV-14).

    Checked *before* the completion, on the month's usage so far. That means one
    turn can overshoot the budget by its own cost; the alternative — reserving
    estimated tokens up front — needs a lock and an accurate estimate, and
    overshooting by a single answer is a better failure than refusing a turn that
    would have fit.
    """
    budget = monthly_budget(session, tenant_id=tenant_id)
    used = monthly_token_usage(session, tenant_id=tenant_id)
    if used >= budget:
        raise BudgetExceeded(used=used, budget=budget)


def record_usage(
    session: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID | None,
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    provider: str | None = None,
) -> None:
    """Append to the usage ledger (R31). Missing counts are recorded as zero."""
    units = (prompt_tokens or 0) + (completion_tokens or 0)
    session.execute(
        text(
            """
            INSERT INTO usage_records (tenant_id, conversation_id, kind, provider,
                                       model, units)
            VALUES (:tenant_id, :conversation_id, 'chat', :provider, :model, :units)
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "provider": provider,
            "model": model,
            "units": units,
        },
    )


# --- provider outcomes (OPS-12) ----------------------------------------------

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILURE)


def record_provider_outcome(
    session: Session,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    provider: str | None,
    model: str | None,
    outcome: str,
    error_class: str | None = None,
) -> None:
    """One row per turn that attempted a completion (OPS-12, §22).

    This is the numerator and denominator OPS-04's provider error-rate alert needs:
    before it, a provider failure was logged to stdout and then unavailable, so the
    rate had neither a count nor a provider dimension.

    **``error_class`` is a type name, never a message.** ``providers.py`` builds its
    messages from up to 300 characters of the provider's response body, which can echo
    the prompt; this table feeds an alert, not an incident reader who could judge it.
    The caller passes ``type(exc).__name__`` and nothing else.

    ``outcome`` is validated here so a typo fails at the call site with a traceback
    rather than as a violated CHECK constraint after a partially-written turn.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown provider outcome: {outcome!r}")
    session.execute(
        text(
            """
            INSERT INTO provider_outcomes
                   (tenant_id, conversation_id, provider, model, outcome, error_class)
            VALUES (:tenant_id, :conversation_id, :provider, :model, :outcome,
                    :error_class)
            """
        ),
        {
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "provider": provider,
            "model": model,
            "outcome": outcome,
            "error_class": error_class,
        },
    )


# --- response cache ----------------------------------------------------------

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 512

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalise(question: str) -> str:
    """Fold case, punctuation and whitespace so trivial variants share an entry.

    Deliberately *not* stemmed or synonym-expanded: two questions that differ in a
    word may retrieve different chunks, and serving one answer for both would be a
    correctness bug traded for a cache hit.
    """
    text = _PUNCT.sub(" ", question.lower())
    return _WHITESPACE.sub(" ", text).strip()


@dataclass
class ResponseCache:
    """Tenant-scoped, TTL'd, size-capped cache of answers.

    Keyed by (tenant, normalised question) *and nothing else* — not by the visitor,
    so two visitors asking the same thing share the answer, which is the point.
    Conversation history is deliberately not part of the key; a cached answer is
    only served for the first turn of a conversation (see the turn loop), where
    there is no history to differentiate it.
    """

    ttl_seconds: float = CACHE_TTL_SECONDS
    max_entries: int = CACHE_MAX_ENTRIES
    _entries: dict[tuple[str, str], tuple[float, dict]] = field(
        default_factory=dict, repr=False
    )

    @staticmethod
    def key(tenant_id: UUID, question: str) -> tuple[str, str]:
        digest = hashlib.sha256(normalise(question).encode()).hexdigest()
        return (str(tenant_id), digest)

    def get(self, tenant_id: UUID, question: str) -> dict | None:
        entry = self._entries.get(self.key(tenant_id, question))
        if entry is None:
            return None
        stored_at, payload = entry
        if time.monotonic() - stored_at > self.ttl_seconds:
            self._entries.pop(self.key(tenant_id, question), None)
            return None
        return payload

    def put(self, tenant_id: UUID, question: str, payload: dict) -> None:
        if len(self._entries) >= self.max_entries:
            # Evict the oldest entry rather than clearing: a full flush would let a
            # burst of distinct questions evict every hot entry at once.
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[self.key(tenant_id, question)] = (time.monotonic(), payload)

    def invalidate_tenant(self, tenant_id: UUID) -> int:
        """Drop a tenant's entries — called on knowledge publish."""
        keys = [k for k in self._entries if k[0] == str(tenant_id)]
        for key in keys:
            self._entries.pop(key, None)
        return len(keys)

    def clear(self) -> None:
        self._entries.clear()


_cache = ResponseCache()


def cache() -> ResponseCache:
    return _cache


# --- task routing ------------------------------------------------------------

# CONV-12: answer generation wants quality, chips and rewrites want speed. Named
# per task so a tenant's model override can target one task without dragging the
# others with it.
_TASK_MODELS = {
    "answer": "deepseek-v4-pro",
    "suggestions": "deepseek-v4.1-flash",
    "rewrite": "deepseek-v4.1-flash",
    "summary": "deepseek-v4.1-flash",
}


def model_for(task: str, *, override: str | None = None) -> str:
    """Resolve the model string for a task, tenant override winning."""
    if override:
        return override
    try:
        return _TASK_MODELS[task]
    except KeyError:
        raise ValueError(f"unknown llm task {task!r}") from None
