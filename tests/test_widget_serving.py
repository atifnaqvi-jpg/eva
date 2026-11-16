"""The widget's static surface and its demo host page (WIDG-01/02, M3 gate).

These cover the *serving* layer, which the browser-facing tests cannot: the loader
is reachable at the URL the install snippet names, the frame document is served as
HTML, and the demo page substitutes a bot id safely.

The mount is conditional on a built bundle existing, so each test skips rather than
fails when the widget has not been built. A skip here is honest: the bundle is a
build artefact, not source.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DIST = Path(__file__).resolve().parents[1] / "backend" / "api" / "widget" / "dist"

pytestmark = pytest.mark.skipif(
    not (DIST / "loader.js").is_file(),
    reason="widget not built (run `node build.mjs` in backend/api/widget)",
)


@pytest.fixture(scope="module")
def widget_client() -> TestClient:
    """A client against the real app with the built widget mounted.

    `WIDGET_ASSET_DIR` is read at import time, so the module is imported here rather
    than at test-module scope: another test module may already have imported
    `app.main` without the variable set, in which case the mount is absent and this
    fixture would silently test nothing.
    """
    import os
    import sys

    api_dir = Path(__file__).resolve().parents[1] / "backend" / "api"
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    os.environ["WIDGET_ASSET_DIR"] = str(DIST)

    import importlib

    import app.main as main

    importlib.reload(main)
    if not any(getattr(route, "path", None) == "/widget" for route in main.app.routes):
        pytest.skip("widget mount not active in this process")
    return TestClient(main.app)


def test_the_loader_is_served_at_the_url_the_snippet_names(widget_client: TestClient) -> None:
    response = widget_client.get("/widget/loader.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "data-bot" in response.text


def test_the_frame_document_is_served_as_html(widget_client: TestClient) -> None:
    """Regression: a `Mount("/widget")` declared before this route swallowed it and
    answered 404, because Starlette matches routes in declaration order."""
    response = widget_client.get("/widget/app")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/widget/app.js" in response.text


def test_the_frame_document_is_not_cached_forever(widget_client: TestClient) -> None:
    response = widget_client.get("/widget/app")
    assert "max-age=300" in response.headers.get("cache-control", "")


def test_the_app_bundle_is_served(widget_client: TestClient) -> None:
    response = widget_client.get("/widget/app.js")
    assert response.status_code == 200
    assert len(response.content) > 5_000


def test_the_demo_page_substitutes_the_bot_id(widget_client: TestClient) -> None:
    response = widget_client.get("/demo/host", params={"bot": "abc123"})
    assert response.status_code == 200
    assert 'data-bot="abc123"' in response.text
    assert "BOT_PUBLIC_ID" not in response.text


@pytest.mark.parametrize(
    "bot",
    [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "",
        "a" * 65,
        "abc 123",
    ],
)
def test_the_demo_page_refuses_a_bot_id_that_is_not_an_id(
    widget_client: TestClient, bot: str
) -> None:
    """The page interpolates the id into HTML. Anything outside the id charset is
    refused, not escaped: a public id is `[A-Za-z0-9_-]` and nothing else."""
    response = widget_client.get("/demo/host", params={"bot": bot})
    assert response.status_code == 400
    assert "<script>alert(1)</script>" not in response.text
    assert "onerror" not in response.text


def test_no_widget_asset_leaks_the_tenant_or_the_database(widget_client: TestClient) -> None:
    """The loader is public. It must carry no ids, hosts or secrets beyond the ones
    the page supplies through `data-bot`."""
    body = widget_client.get("/widget/loader.js").text
    for forbidden in ("postgres", "password", "changeme", "tenant_id", "service_role"):
        assert forbidden not in body.lower(), f"loader contains {forbidden}"
