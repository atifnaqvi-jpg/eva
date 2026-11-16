"""The LLM interface the turn loop depends on (ARCHITECTURE 8.3, module `llm`).

WP-13 owns the real implementation: LiteLLM routing, the BYOK vault, the fallback
chain and the circuit breaker. This file exists first because the turn loop must be
buildable and testable without a provider, and because writing it the other way
round would have meant the loop importing LiteLLM directly — which is exactly the
coupling the module boundary exists to prevent.

Two decisions are load-bearing here:

* **Streaming is the primitive.** A non-streaming answer is a stream with one
  chunk; the reverse is not true without buffering a whole answer before the
  visitor sees anything.
* **A failed completion is a value, not an exception.** ``Completion.error`` lets
  the turn loop degrade to the offline message with the handover form (§11.5) at a
  point where it still owns the SSE stream. An exception thrown mid-stream cannot
  be turned back into a well-formed event sequence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class CompletionChunk:
    text: str


@dataclass(frozen=True)
class Completion:
    """Outcome of one completion: either text or an error, never both."""

    text: str = ""
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None


@runtime_checkable
class LlmProvider(Protocol):
    """A chat-completion backend.

    ``model`` is an opaque identifier (``provider/model``). The turn loop never
    interprets it; routing and fallback are WP-13's business.
    """

    key: str

    def stream(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024,
    ) -> AsyncIterator[CompletionChunk]:
        """Yield answer text as it arrives."""
        ...

    def complete(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024,
    ) -> Completion:
        """Non-streaming completion, used where a stream buys nothing."""
        ...


@dataclass
class UnconfiguredProvider:
    """Stands in when no provider is configured.

    Every call fails with a named error rather than raising, so the turn loop takes
    its normal degradation path and the visitor gets the handover affordance
    instead of a 500. This is the state the MVP ships in until WP-13 wires a real
    provider and a platform key: an honest "assistant unavailable" beats a
    fabricated answer.
    """

    key: str = "unconfigured"
    reason: str = "no_llm_provider_configured"
    calls: list[list[ChatMessage]] = field(default_factory=list)

    def stream(self, messages, *, model, max_tokens=1024) -> AsyncIterator[CompletionChunk]:
        raise RuntimeError("UnconfiguredProvider.stream must not be used in the turn loop")

    def complete(self, messages, *, model, max_tokens=1024) -> Completion:
        self.calls.append(messages)
        return Completion(error=self.reason, model=model)


def default_provider() -> LlmProvider:
    """The process's provider, chosen by ``LLM_PROVIDER``.

    Only ``unconfigured`` exists until WP-13 lands the LiteLLM implementation. The
    factory is here, rather than in the turn loop, so that WP-13 adds a branch to
    one function instead of touching the conversation engine.
    """
    from app.config import LLM_PROVIDER

    if LLM_PROVIDER == "unconfigured":
        return UnconfiguredProvider()
    raise RuntimeError(
        f"LLM_PROVIDER={LLM_PROVIDER!r} has no implementation yet; WP-13 adds it"
    )
