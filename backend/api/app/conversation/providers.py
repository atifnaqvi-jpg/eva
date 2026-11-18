"""OpenAI-compatible chat provider on `httpx` (CONV-11, ARCHITECTURE §11.1/§11.2).

ARCHITECTURE §11.1 names the LiteLLM SDK. LiteLLM is not an approved runtime
dependency, and it is a router + model-price database + proxy client for what this
MVP needs from two providers. Both MVP providers — Ollama Cloud and DeepSeek —
expose the OpenAI chat-completions wire format, and `httpx` is already a dependency,
so this module implements the `LlmProvider` protocol directly. The deviation is
recorded in PLAN §8.4.5 (HALT-05, recommendation B).

What that costs, stated rather than glossed: there is no bundled model-price table,
so `cost_micros` stays 0 until prices are configured; and adding a third provider
means adding it to `PROFILES` rather than to a LiteLLM config file. What it buys:
one fewer large dependency in a 512 MB image, and a request path small enough to
read end to end.

**Why the SSE parsing is hand-written.** Streaming is where provider adapters
usually break, and the breakage is subtle: a chunk split across TCP reads, a
`data: [DONE]` terminator, a comment line, an error object delivered with a 200
status. The parser below is fed byte-by-byte in the tests specifically so those
cases are exercised rather than assumed.

**Timeouts are mandatory.** A provider that accepts a connection and then never
sends a byte is the failure the circuit breaker exists for; without an explicit
timeout the breaker would never trip because the call would never return.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.conversation.llm import ChatMessage, Completion, CompletionChunk

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0


@dataclass(frozen=True)
class ProviderProfile:
    """Where a provider lives and what it calls itself.

    ``model_prefix`` lets the chain route an opaque ``provider/model`` string to the
    right endpoint without the turn loop knowing anything about providers.
    """

    key: str
    base_url: str
    model_prefix: str
    api_key_env: str
    # Ollama Cloud wants a bearer token like the OpenAI API; DeepSeek the same.
    # Kept as a field because a future local Ollama needs no auth at all.
    auth_required: bool = True


PROFILES: dict[str, ProviderProfile] = {
    "ollama_cloud": ProviderProfile(
        key="ollama_cloud",
        base_url="https://ollama.com/v1",
        model_prefix="ollama_cloud/",
        api_key_env="OLLAMA_API_KEY",
    ),
    "deepseek": ProviderProfile(
        key="deepseek",
        base_url="https://api.deepseek.com/v1",
        model_prefix="deepseek/",
        api_key_env="DEEPSEEK_API_KEY",
    ),
}


def strip_prefix(model: str, profile: ProviderProfile) -> str:
    """`deepseek/deepseek-v4-flash` -> `deepseek-v4-flash`.

    A model string without the prefix is passed through unchanged: some deployments
    configure a single provider and omit it, and rejecting that would be pedantry
    with no safety benefit.
    """
    if model.startswith(profile.model_prefix):
        return model[len(profile.model_prefix):]
    return model


def _payload(
    messages: list[ChatMessage], *, model: str, max_tokens: int, stream: bool
) -> dict:
    body = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "max_tokens": max_tokens,
        "stream": stream,
        # The bot's answers come from retrieved passages, so a high temperature can
        # only add invention. This is a product decision, not a default inherited
        # from the provider.
        "temperature": 0.2,
    }
    if stream:
        # OPS-12. Without this an OpenAI-compatible stream never reports token counts,
        # which is why the ledger and the budget stop were structurally zero. Measured
        # against Ollama Cloud: the request is accepted and the stream ends with a
        # `choices: []` frame carrying `usage`. A provider that ignores the option
        # simply omits the frame, and the caller falls back to a labelled estimate.
        body["stream_options"] = {"include_usage": True}
    return body


def parse_sse_line(line: str) -> tuple[str, object] | None:
    """One SSE line -> (kind, payload), or None for lines that carry nothing.

    Kinds: ``delta`` (answer text), ``done`` (terminator), ``error`` (provider error
    delivered inside the stream), ``usage`` (the trailing token counts). Returning a
    tagged kind rather than a bare string is what lets the caller distinguish "no more
    tokens" from "the provider just told us it failed mid-answer".
    """
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data:
        return None
    if data == "[DONE]":
        return ("done", None)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        # A truncated frame. Ignoring it loses at most a partial token burst; raising
        # would abort an otherwise healthy answer.
        logger.debug("unparseable SSE frame: %r", data[:120])
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("error"):
        error = parsed["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return ("error", message or "provider_error")
    usage = _usage(parsed)
    choices = parsed.get("choices") or []
    if not choices:
        # The usage frame OpenAI-compatible providers append when asked for it has an
        # empty `choices` list, so it must be read before the early return below —
        # otherwise it is indistinguishable from a keep-alive and the counts are lost.
        if usage != (None, None):
            return ("usage", usage)
        return None
    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    text = delta.get("content")
    if text is None:
        # Non-streaming shape, in case a provider ignores `stream: true`.
        text = (choice.get("message") or {}).get("content")
    if text:
        return ("delta", text)
    # A final chunk may carry both the last delta and the usage.
    if usage != (None, None):
        return ("usage", usage)
    return None


def _usage(parsed: dict) -> tuple[int | None, int | None]:
    usage = parsed.get("usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


@dataclass
class OpenAiCompatibleProvider:
    """One provider endpoint. Stateless apart from its HTTP client."""

    profile: ProviderProfile
    # repr=False: a dataclass repr ends up in tracebacks and log lines, and this
    # field is the tenant's or the platform's credential.
    api_key: str = field(repr=False)
    # Injected in tests so a real local server can stand in for the vendor.
    transport: httpx.AsyncBaseTransport | None = None
    connect_timeout: float = CONNECT_TIMEOUT
    read_timeout: float = READ_TIMEOUT
    _client: httpx.AsyncClient | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return self.profile.key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.profile.auth_required:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client_(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.profile.base_url,
                timeout=httpx.Timeout(
                    self.read_timeout, connect=self.connect_timeout
                ),
                transport=self.transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def stream(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[CompletionChunk]:
        return self._stream(messages, model=model, max_tokens=max_tokens)

    async def _stream(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> AsyncIterator[CompletionChunk]:
        client = self._client_()
        body = _payload(
            messages,
            model=strip_prefix(model, self.profile),
            max_tokens=max_tokens,
            stream=True,
        )
        usage: tuple[int | None, int | None] = (None, None)
        async with client.stream(
            "POST", "/chat/completions", json=body, headers=self._headers()
        ) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors="replace")[:300]
                raise RuntimeError(
                    f"{self.profile.key} returned {response.status_code}: {detail}"
                )
            async for line in response.aiter_lines():
                event = parse_sse_line(line)
                if event is None:
                    continue
                kind, payload = event
                if kind == "done":
                    break
                if kind == "error":
                    raise RuntimeError(f"{self.profile.key}: {payload}")
                if kind == "usage":
                    usage = payload  # type: ignore[assignment]
                    continue
                yield CompletionChunk(text=str(payload))
        if usage != (None, None):
            # Carried on a trailing empty-text chunk: the counts arrive *after* the last
            # token, so there is no earlier chunk to attach them to, and a stream has no
            # return value. The turn loop ignores a chunk whose text is empty.
            yield CompletionChunk(
                text="", prompt_tokens=usage[0], completion_tokens=usage[1]
            )

    def complete(
        self, messages: list[ChatMessage], *, model: str, max_tokens: int = 1024
    ) -> Completion:
        body = _payload(
            messages,
            model=strip_prefix(model, self.profile),
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            with httpx.Client(
                base_url=self.profile.base_url,
                timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
                transport=self.transport,
            ) as client:
                response = client.post(
                    "/chat/completions", json=body, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            return Completion(error=f"{self.profile.key}: {exc}", model=model)
        if response.status_code >= 400:
            return Completion(
                error=f"{self.profile.key}: HTTP {response.status_code}",
                model=model,
            )
        try:
            parsed = response.json()
        except ValueError:
            return Completion(error=f"{self.profile.key}: invalid JSON", model=model)
        if parsed.get("error"):
            error = parsed["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            return Completion(error=f"{self.profile.key}: {message}", model=model)
        choices = parsed.get("choices") or []
        text = ""
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
        prompt_tokens, completion_tokens = _usage(parsed)
        return Completion(
            text=text,
            model=parsed.get("model") or model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
