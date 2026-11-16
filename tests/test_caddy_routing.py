"""Regression: the proxy must not rewrite the path (found by the M1 gate).

``handle_path`` strips the matched prefix, so ``/api/v1/health`` reached the API
as ``/v1/health`` and every route 404'd. Nothing in the test suite caught it
because tests call the ASGI app directly. This asserts the Caddyfile contract.
"""

from pathlib import Path

import pytest

CADDYFILE = Path(__file__).resolve().parents[1] / "caddy" / "Caddyfile"


@pytest.fixture(scope="module")
def config() -> str:
    return CADDYFILE.read_text()


def test_api_requests_are_not_path_rewritten(config: str) -> None:
    assert "handle_path /api/*" not in config, (
        "handle_path strips /api, but the api mounts routes at /api/v1/...; "
        "use `handle` instead"
    )
    assert "handle /api/*" in config


def test_api_block_proxies_to_the_api_service(config: str) -> None:
    start = config.index("handle /api/*")
    assert "reverse_proxy api:8000" in config[start : start + 120]


def test_caddyfile_is_pinned_to_plain_http_for_dev(config: str) -> None:
    """The dev site is plain HTTP, which is why the session cookie is not Secure."""
    assert "http://localhost" in config
