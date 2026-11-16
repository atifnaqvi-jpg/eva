"""Connector protocol and registry (ARCHITECTURE 14, R21).

Interface only — MVP ships **zero** concrete connectors. The registry and the
read/write separation exist now so that V3 write access cannot silently turn a
read-only integration into a read-write one: they are different methods, and
``write`` carries a mandatory confirmation token.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OAuthFlow:
    authorize_url: str
    state: str


@dataclass(frozen=True)
class HealthCheck:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class ReadResult:
    data: object
    truncated: bool = False


@dataclass(frozen=True)
class WriteResult:
    data: object
    idempotency_key: str


@runtime_checkable
class Connector(Protocol):
    key: str  # "shopify", "calendly"

    async def authorize(self, tenant_id: str) -> OAuthFlow:
        ...

    async def test(self, tenant_id: str) -> HealthCheck:
        ...

    async def read(self, tenant_id: str, operation: str, params: dict) -> ReadResult:
        ...

    async def write(
        self,
        tenant_id: str,
        operation: str,
        params: dict,
        *,
        confirmation: str,
    ) -> WriteResult:
        """V3 only. ``confirmation`` is a required visitor-approved token."""
        ...


class ConnectorRegistry:
    """Empty by design in the MVP (R21)."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.key in self._connectors:
            raise ValueError(f"connector {connector.key!r} already registered")
        self._connectors[connector.key] = connector

    def get(self, key: str) -> Connector | None:
        return self._connectors.get(key)

    def keys(self) -> list[str]:
        return sorted(self._connectors)


registry = ConnectorRegistry()
