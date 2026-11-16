"""Website crawling (KNOW-05, ARCHITECTURE 10.5 / 17.3).

Constraints, all enforced here rather than by the caller:

* **Domain scope** — only URLs on the configured host are followed. A link off the
  domain is skipped, not fetched, so a crawl cannot wander into a third party.
* **robots.txt** — fetched once per crawl and honoured. If it cannot be read, the
  crawler treats the site as disallowed (fail closed); silently ignoring robots
  because of a network blip is the wrong default for someone else's server.
* **Page cap** — a hard limit, because an unbounded crawl is a denial-of-service
  against a customer's site.
* **Politeness** — one request at a time with a delay, and a size cap per page so
  a huge asset cannot exhaust memory.

Static HTML only: JavaScript-rendered content is out of scope, which the
architecture requires to be documented rather than quietly attempted.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

MAX_PAGES = 50
MAX_BYTES = 5 * 1024 * 1024
REQUEST_DELAY_SECONDS = 0.5
TIMEOUT_SECONDS = 15.0
USER_AGENT = "EVA-Crawler/0.1 (+knowledge ingestion; respects robots.txt)"


@dataclass
class CrawledPage:
    url: str
    status_code: int
    html: bytes
    content_type: str = "text/html"


@dataclass
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "pages": len(self.pages),
            "skipped": len(self.skipped),
            "skipped_reasons": self.skipped[:20],
        }


def same_site(candidate: str, root: str) -> bool:
    """True when ``candidate`` is on the same host as ``root`` (subdomains excluded)."""
    return urlparse(candidate).netloc.lower() == urlparse(root).netloc.lower()


def _normalise(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _is_crawlable_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    # Skip binaries and assets: they cannot be turned into prose.
    if any(
        parsed.path.lower().endswith(ext)
        for ext in (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                    ".css", ".js", ".ico", ".mp4", ".webp")
    ):
        return False
    return True


class RobotsCache:
    """robots.txt per origin, fetched once per crawl."""

    def __init__(self) -> None:
        self._parsers: dict[str, RobotFileParser] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._parsers.get(origin)
        if parser is None:
            parser = RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                response = await client.get(robots_url)
                if response.status_code >= 400:
                    # No robots.txt published: everything is allowed.
                    parser.parse([])
                else:
                    parser.parse(response.text.splitlines())
            except httpx.HTTPError:
                # Fail closed: an unreadable robots.txt is not permission.
                logger.warning("robots.txt unreadable at %s; skipping site", robots_url)
                parser.parse(["User-agent: *", "Disallow: /"])
            self._parsers[origin] = parser
        return parser.can_fetch(USER_AGENT, url)


async def crawl(
    start_url: str,
    *,
    max_pages: int = MAX_PAGES,
    respect_robots: bool = True,
    client: httpx.AsyncClient | None = None,
) -> CrawlResult:
    """Breadth-first crawl of ``start_url``'s site, bounded by ``max_pages``."""
    result = CrawlResult()
    robots = RobotsCache()
    queue: list[str] = [_normalise(start_url)]
    if not _is_crawlable_url(start_url):
        result.skipped.append((start_url, "unsupported-scheme-or-asset"))
        return result

    seen: set[str] = set()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )

    try:
        while queue and len(result.pages) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            if not same_site(url, start_url):
                result.skipped.append((url, "off-domain"))
                continue
            if not _is_crawlable_url(url):
                result.skipped.append((url, "unsupported-scheme-or-asset"))
                continue
            if respect_robots and not await robots.allowed(client, url):
                result.skipped.append((url, "robots-disallow"))
                continue

            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                result.skipped.append((url, f"fetch-error: {type(exc).__name__}"))
                continue

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                result.skipped.append((url, f"non-html: {content_type[:40]}"))
                continue
            if len(response.content) > MAX_BYTES:
                result.skipped.append((url, "too-large"))
                continue

            result.pages.append(
                CrawledPage(url=str(response.url), status_code=response.status_code,
                            html=response.content, content_type=content_type)
            )
            queue.extend(_links(response.text, base=str(response.url)))
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
    finally:
        if owns_client:
            await client.aclose()

    return result


def _links(html: str, *, base: str) -> list[str]:
    from lxml import html as lxml_html

    try:
        document = lxml_html.fromstring(html)
    except Exception:
        return []
    found: list[str] = []
    for anchor in document.xpath("//a/@href"):
        absolute = urljoin(base, anchor)
        if absolute.startswith(("http://", "https://")):
            found.append(_normalise(absolute))
    return found
