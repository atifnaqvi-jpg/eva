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
    """One piece of a streamed answer.

    ``prompt_tokens``/``completion_tokens`` are set on at most one chunk — the final
    usage frame OpenAI-compatible providers send when the request asks for it — and
    nowhere else. They travel on a chunk rather than a return value because a stream
    has no return, and a counter on the provider object would be shared state across
    concurrent turns.

    ``provider`` is set by the fallback chain on the first chunk it forwards, so the
    turn loop can attribute the turn to the provider that actually served it instead of
    to the chain that wrapped it (OPS-12).
    """

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    provider: str | None = None


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
    """The process's provider chain, built from configured credentials.

    Resolution is by *credential presence*, not by a provider name: whichever of
    ``OLLAMA_API_KEY`` / ``DEEPSEEK_API_KEY`` is set becomes a link in the chain, in
    that order (platform default first, per §11.1). With no key at all the chain is
    a single ``UnconfiguredProvider``, so the turn loop takes its normal degradation
    path — an honest "assistant unavailable" rather than a fabricated answer.

    This is why ``LLM_PROVIDER`` is not simply a provider name: a chain has to
    survive one provider being down, which means it has to be built from everything
    configured rather than from one chosen winner.
    """
    import os

    from app.conversation.providers import PROFILES, OpenAiCompatibleProvider
    from app.conversation.resilience import FallbackChain

    order = ("ollama_cloud", "deepseek")
    providers: list[LlmProvider] = []
    for key in order:
        profile = PROFILES[key]
        api_key = os.environ.get(profile.api_key_env, "").strip()
        if api_key:
            providers.append(OpenAiCompatibleProvider(profile=profile, api_key=api_key))

    if not providers:
        return UnconfiguredProvider()
    if len(providers) == 1:
        return providers[0]
    return FallbackChain(providers=providers)
