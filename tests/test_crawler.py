"""KNOW-05: crawler domain scoping, robots.txt, page cap — against a real server.

A local ``http.server`` serves synthetic pages, so these exercise actual HTTP,
actual robots.txt parsing and actual link extraction rather than a mocked client.
No external network is touched.
"""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PAGES = {
    "/": (
        "<html><body><h1>Support Home</h1>"
        "<p>Standard shipping takes three to five days.</p>"
        "<a href='/shipping'>Shipping</a>"
        "<a href='/returns'>Returns</a>"
        "<a href='/private'>Private</a>"
        "<a href='https://evil.example/offsite'>Offsite</a>"
        "<a href='/brochure.pdf'>PDF</a>"
        "</body></html>"
    ),
    "/shipping": "<html><body><h1>Shipping</h1><p>Express is next day.</p></body></html>",
    "/returns": "<html><body><h1>Returns</h1><p>Thirty days, unused items.</p></body></html>",
    "/private": "<html><body><h1>Secret</h1><p>Should not be crawled.</p></body></html>",
}

ROBOTS = "User-agent: *\nDisallow: /private\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/robots.txt":
            body, ctype = ROBOTS.encode(), "text/plain"
        elif self.path in PAGES:
            body, ctype = PAGES[self.path].encode(), "text/html; charset=utf-8"
        elif self.path == "/brochure.pdf":
            body, ctype = b"%PDF-1.4 binary", "application/pdf"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    yield f"http://{host}:{port}"
    httpd.shutdown()
    thread.join(timeout=5)


def test_crawl_collects_same_site_html_pages(server) -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=10))

    urls = {p.url for p in result.pages}
    assert f"{server}/shipping" in urls
    assert f"{server}/returns" in urls
    assert len(result.pages) == 3


def test_crawl_never_leaves_the_configured_domain(server) -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=10))

    assert all(p.url.startswith(server) for p in result.pages)
    reasons = dict(result.skipped)
    assert reasons.get("https://evil.example/offsite") == "off-domain"


def test_robots_txt_disallow_is_honoured(server) -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=10, respect_robots=True))

    assert not any("/private" in p.url for p in result.pages)
    assert dict(result.skipped).get(f"{server}/private") == "robots-disallow"


def test_robots_can_be_bypassed_only_explicitly(server) -> None:
    """``respect_robots=False`` exists for a site the owner controls."""
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=10, respect_robots=False))
    assert any("/private" in p.url for p in result.pages)


def test_non_html_assets_are_skipped(server) -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=10))
    assert not any(p.url.endswith(".pdf") for p in result.pages)
    assert dict(result.skipped).get(f"{server}/brochure.pdf") == "unsupported-scheme-or-asset"


def test_page_cap_is_enforced(server) -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl(f"{server}/", max_pages=2))
    assert len(result.pages) == 2, "the cap must bound the crawl, not just the queue"


def test_unreadable_robots_fails_closed() -> None:
    """An unreachable robots.txt must not be treated as permission."""
    import httpx

    from app.ingestion.crawler import RobotsCache

    class _FailingClient:
        async def get(self, url):
            raise httpx.ConnectError("network down")

    allowed = asyncio.run(
        RobotsCache().allowed(_FailingClient(), "http://example.test/page")
    )
    assert allowed is False, "an unreadable robots.txt must deny, not permit"


def test_offsite_start_url_is_rejected_before_any_fetch() -> None:
    from app.ingestion.crawler import crawl

    result = asyncio.run(crawl("ftp://example.test/file"))
    assert result.pages == []
    assert result.skipped[0][1] == "unsupported-scheme-or-asset"
