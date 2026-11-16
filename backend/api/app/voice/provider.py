"""Voice provider protocol (ARCHITECTURE 15, R16).

Interface only — MVP is text-only. The protocol exists now so that adding voice in
V2 is not a breaking change, and so the planned implementation (ElevenLabs
Conversational AI, R17) cannot lock the conversation engine to one vendor.

Voice reuses the same conversation engine as text, with a separate transport
adapter for WebRTC/WebSocket audio, VAD and barge-in. That decision is why these
types speak in terms of turns and events rather than audio formats.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class VoiceEventType(str, Enum):
    PARTIAL_TRANSCRIPT = "partial_transcript"
    TURN_END = "turn_end"
    BARGE_IN = "barge_in"


@dataclass(frozen=True)
class VoiceEvent:
    """Something the provider reports mid-session."""

    type: VoiceEventType
    text: str | None = None


@dataclass(frozen=True)
class VoiceSession:
    id: str
    provider: str


@runtime_checkable
class VoiceProvider(Protocol):
    """A realtime audio transport for a conversation turn."""

    key: str

    async def start_session(self, *, tenant_id: str, conversation_id: str) -> VoiceSession:
        """Open a voice session bound to an existing conversation."""
        ...

    def stream_audio(self, session: VoiceSession) -> AsyncIterator[bytes]:
        """Provider audio for the visitor, chunk by chunk."""
        ...

    async def interrupt(self, session: VoiceSession) -> None:
        """Stop playback immediately (visitor barged in)."""
        ...

    async def end_session(self, session: VoiceSession) -> None:
        """Close the session and release transport resources."""
        ...
