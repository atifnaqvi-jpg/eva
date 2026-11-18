"""Circuit breaker and provider fallback chain (CONV-13, ARCHITECTURE §11.5).

The failure this exists to prevent is not "the provider errored" — that is normal
and the visitor never notices. It is **slow failure**: a provider that accepts
connections and then hangs for 30 s per request. A retry-every-time policy spends
every visitor's patience on a provider that is already known to be down, and the
support widget degrades from "answering" to "spinner" for everyone at once.

So the breaker counts consecutive failures per provider and, once tripped, refuses
to even attempt that provider until a cooldown elapses. The cooldown is the point:
tripping without an automatic half-open retry would mean an operator has to restart
the process after a two-minute outage.

Three states, the standard shape:

* ``closed`` — normal. Failures increment; ``failure_threshold`` consecutive
  failures opens the circuit.
* ``open`` — calls fail immediately with ``CircuitOpen``. No provider traffic.
* ``half_open`` — after ``cooldown_seconds``, exactly one probe is allowed through.
  Success closes the circuit; failure re-opens it and restarts the cooldown.

State lives in-process (a dict guarded by a lock). That is honest for the current
single-`api`-container deployment: each container keeps its own view, which is the
right granularity anyway — a breaker is a local backpressure signal, not a global
consensus fact. Multi-replica deployments would want Redis, and the interface here
is the seam for that.

**The chain's contract:** try providers in order, skip tripped ones, and return the
first success. If every provider fails, return a ``Completion`` with an error rather
than raising — the turn loop already knows how to turn that into the offline
message and handover (WP-11), and it must stay able to do so.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from enum import Enum

from app.observability import tracing
from app.conversation.llm import ChatMessage, Completion, CompletionChunk, LlmProvider

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(RuntimeError):
    """The provider is tripped; the call did not reach the network."""


@dataclass
class CircuitBreaker:
    """Consecutive-failure breaker with a half-open probe."""

    name: str
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    _state: CircuitState = CircuitState.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state is CircuitState.OPEN and (
            time.monotonic() - self._opened_at >= self.cooldown_seconds
        ):
            self._state = CircuitState.HALF_OPEN

    def check(self) -> None:
        """Raise ``CircuitOpen`` if a call must not be attempted."""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                remaining = self.cooldown_seconds - (time.monotonic() - self._opened_at)
                raise CircuitOpen(
                    f"{self.name}: circuit open, retry in {remaining:.1f}s"
                )

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            # A failed probe re-opens immediately: half-open allows exactly one
            # trial, and it just failed.
            if self._state is CircuitState.HALF_OPEN or (
                self._failures >= self.failure_threshold
            ):
                if self._state is not CircuitState.OPEN:
                    logger.warning(
                        "circuit opened for provider %s after %d consecutive failures",
                        self.name, self._failures,
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = 0.0


@dataclass
class FallbackChain:
    """Ordered providers, each with its own breaker.

    ``key`` and ``model_prefix`` exist because the chain is itself an
    ``LlmProvider``: the turn loop hands it one model string and the chain resolves
    which provider can serve it. A model string that no provider claims is a
    configuration error, and it is reported as one rather than silently routed to
    whichever provider happens to be first.
    """

    providers: list[LlmProvider]
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    key: str = "fallback-chain"
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        for provider in self.providers:
            self.breakers.setdefault(
                provider.key,
                CircuitBreaker(
                    name=provider.key,
                    failure_threshold=self.failure_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                ),
            )

    def breaker(self, key: str) -> CircuitBreaker:
        return self.breakers[key]

    def complete(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> Completion:
        """First provider that succeeds wins; all-fail returns an error value."""
        errors: list[str] = []
        for provider in self.providers:
            breaker = self.breakers[provider.key]
            try:
                breaker.check()
            except CircuitOpen as exc:
                errors.append(str(exc))
                tracing.event(
                    "provider.skipped", provider_key=provider.key, reason="circuit_open"
                )
                continue
            try:
                completion = provider.complete(messages, model=model, max_tokens=max_tokens)
            except Exception as exc:  # noqa: BLE001 - one provider down is expected
                breaker.record_failure()
                errors.append(f"{provider.key}: {exc}")
                tracing.error_event(exc, provider_key=provider.key)
                logger.warning("provider %s failed: %s", provider.key, exc)
                continue
            if completion.failed:
                breaker.record_failure()
                errors.append(f"{provider.key}: {completion.error}")
                # The message is provider text and possibly prompt text, so only its
                # presence is recorded — the type is what an operator groups by.
                tracing.event(
                    "provider.error",
                    provider_key=provider.key,
                    error_type="CompletionError",
                )
                continue
            breaker.record_success()
            return completion
        return Completion(
            error="all_providers_failed: " + "; ".join(errors) if errors else "no_providers",
            model=model,
        )

    def stream(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[CompletionChunk]:
        return self._stream(messages, model=model, max_tokens=max_tokens)

    async def _stream(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[CompletionChunk]:
        """Stream from the first provider that yields anything.

        Fallback is only possible *before* the first token reaches the visitor: once
        bytes are on the wire, switching providers mid-sentence would splice two
        different answers together. So a provider that raises before its first chunk
        is skipped, and one that raises after it propagates — the turn loop turns
        that into the offline message, which is the honest outcome.
        """
        errors: list[str] = []
        for provider in self.providers:
            breaker = self.breakers[provider.key]
            try:
                breaker.check()
            except CircuitOpen as exc:
                errors.append(str(exc))
                tracing.event(
                    "provider.skipped", provider_key=provider.key, reason="circuit_open"
                )
                continue

            yielded = False
            try:
                async for chunk in provider.stream(
                    messages, model=model, max_tokens=max_tokens
                ):
                    # A chunk with no text is the trailing usage frame, not an answer
                    # token: counting it as "yielded" would turn an empty stream into a
                    # success and suppress the fallback that exists for exactly that.
                    if chunk.text and not yielded:
                        breaker.record_success()
                        yielded = True
                    # OPS-12: the caller needs to know *which* provider served the turn,
                    # and the chain is itself an LlmProvider whose own `key` names no
                    # provider. Stamped on every chunk because a chunk is frozen; the
                    # turn loop reads the first non-empty one.
                    if chunk.provider is None:
                        chunk = replace(chunk, provider=provider.key)
                    yield chunk
            except Exception as exc:  # noqa: BLE001
                breaker.record_failure()
                tracing.error_event(exc, provider_key=provider.key, yielded=yielded)
                logger.warning("provider %s stream failed: %s", provider.key, exc)
                if yielded:
                    raise
                errors.append(f"{provider.key}: {exc}")
                continue
            if yielded:
                return
            breaker.record_failure()
            errors.append(f"{provider.key}: empty stream")
            tracing.event(
                "provider.error", provider_key=provider.key, error_type="EmptyStream"
            )
        raise RuntimeError("all_providers_failed: " + ("; ".join(errors) or "no_providers"))
