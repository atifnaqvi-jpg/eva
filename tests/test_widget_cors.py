"""CORS for the widget API (WIDG-05/SEC-08).

The widget is embedded on the tenant's domain while the API is served from ours, so
every widget call is cross-origin. Without `Access-Control-Allow-Origin` the browser
blocks the request before it is sent, and the widget works *only* on a host page
served from the API's own origin -- which is exactly what the demo page is, and
exactly why this went unnoticed: `curl` does not enforce CORS.

These tests exist because the bug was real and shipped. They are the browser-free
half; `widget/test/browser.test.mjs` proves the same thing in Chromium.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

TENANT_ORIGIN = "https://shop.example.com"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_preflight_on_the_session_endpoint_is_allowed(client: TestClient) -> None:
    """The browser sends OPTIONS first; a 405 here blocks every widget call."""
    response = client.options(
        "/api/v1/widget/session",
        headers={
            "Origin": TENANT_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_a_real_widget_request_gets_the_header(client: TestClient) -> None:
    response = client.post(
        "/api/v1/widget/session",
        json={"bot_public_id": "does-not-matter-for-this-check"},
        headers={"Origin": TENANT_ORIGIN},
    )
    # Any status is fine here -- what matters is that the browser is allowed to read
    # the response at all. The origin *policy* is decided separately, by bot_domains.
    assert "access-control-allow-origin" in response.headers


@pytest.mark.parametrize(
    "path",
    ["/api/v1/widget/session", "/api/v1/widget/conversations", "/widget/app"],
)
def test_every_widget_path_the_browser_calls_is_covered(
    client: TestClient, path: str
) -> None:
    response = client.options(
        path,
        headers={
            "Origin": TENANT_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200, f"{path} does not answer preflight"
    assert "access-control-allow-origin" in response.headers


def test_the_dashboard_api_is_not_cors_enabled(client: TestClient) -> None:
    """`*` on the admin surface would be a needless widening.

    The dashboard is same-origin, so it needs no CORS at all; only the widget, which
    is embedded elsewhere by design, does.
    """
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_credentials_are_not_allowed_with_a_wildcard(client: TestClient) -> None:
    """`allow_credentials=True` with `*` is forbidden by the CORS spec and would be
    the one combination that turns a wildcard into an actual risk. The widget
    authenticates with a bearer token, so there is no cookie to send."""
    response = client.options(
        "/api/v1/widget/session",
        headers={
            "Origin": TENANT_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-credentials" not in response.headers


def test_a_restricted_origin_list_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that wants to pin origins can, via WIDGET_ALLOWED_ORIGINS."""
    import importlib

    import app.main as main_module

    monkeypatch.setenv("WIDGET_ALLOWED_ORIGINS", "https://a.example, https://b.example")
    reloaded = importlib.reload(main_module)
    try:
        client = TestClient(reloaded.app)
        allowed = client.options(
            "/api/v1/widget/session",
            headers={
                "Origin": "https://a.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert allowed.headers["access-control-allow-origin"] == "https://a.example"

        refused = client.options(
            "/api/v1/widget/session",
            headers={
                "Origin": "https://c.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in refused.headers
    finally:
        monkeypatch.undo()
        importlib.reload(main_module)
