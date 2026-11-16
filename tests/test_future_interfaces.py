"""FUT-01/FUT-02: future-ready interfaces exist and stay inert.

These protocols are cheap now and expensive to retrofit, so the tests pin the two
properties that matter: the interfaces are actually usable as protocols, and the
MVP ships no concrete implementations (R21) with write reachable only explicitly.
"""

import inspect

import pytest
from fastapi.testclient import TestClient


def test_voice_provider_protocol_shape() -> None:
    from app.voice.provider import VoiceProvider

    for method in ("start_session", "stream_audio", "interrupt", "end_session"):
        assert hasattr(VoiceProvider, method), f"VoiceProvider must define {method}"

    # The four methods are the contract ARCHITECTURE 15 fixes for V2.
    signature = inspect.signature(VoiceProvider.start_session)
    assert "tenant_id" in signature.parameters
    assert "conversation_id" in signature.parameters


def test_voice_events_cover_partial_turn_end_and_barge_in() -> None:
    from app.voice.provider import VoiceEventType

    assert {e.value for e in VoiceEventType} == {
        "partial_transcript", "turn_end", "barge_in"
    }


def test_connector_read_and_write_are_separate() -> None:
    """Read-only must never silently become read-write (ARCHITECTURE 14)."""
    from app.integrations.connector import Connector

    assert hasattr(Connector, "read")
    assert hasattr(Connector, "write")
    write_params = inspect.signature(Connector.write).parameters
    assert "confirmation" in write_params, "writes require explicit confirmation"
    assert write_params["confirmation"].kind is inspect.Parameter.KEYWORD_ONLY


def test_no_concrete_connectors_ship_in_the_mvp() -> None:
    from app.integrations.connector import registry

    assert registry.keys() == [], "R21: MVP ships zero concrete connectors"


def test_registry_rejects_duplicate_keys() -> None:
    from app.integrations.connector import ConnectorRegistry

    class Dummy:
        key = "dummy"

    local = ConnectorRegistry()
    local.register(Dummy())
    with pytest.raises(ValueError):
        local.register(Dummy())
    assert local.keys() == ["dummy"]


def test_voice_endpoint_is_reserved_and_returns_501() -> None:
    """Publishing the contract now is what avoids a breaking change in V2."""
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/v1/widget/voice-sessions")
    assert response.status_code == 501
