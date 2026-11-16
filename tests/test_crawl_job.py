"""KNOW-05 (job): a recrawl versions changed pages and skips unchanged ones."""

import asyncio
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from sqlalchemy import create_engine

from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT, _admin_dsn
from app.retrieval.embedding import HashingEmbedder

PAGES = {
    "/": "<html><body><h1>Home</h1><p>We ship worldwide.</p>"
         "<a href='/returns'>Returns</a></body></html>",
    "/returns": "<html><body><h1>Returns</h1><p>Thirty days.</p></body></html>",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/robots.txt":
            body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
        elif self.path in PAGES:
            body, ctype = PAGES[self.path].encode(), "text/html; charset=utf-8"
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


@pytest.fixture
def workspace(migrated_db, monkeypatch, server):
    import app.db.session as db_session

    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+psycopg",
        username=APP_USER,
        password=APP_PASSWORD,
        host=PG_HOST,
        port=PG_PORT,
        database=migrated_db,
    ).render_as_string(hide_password=False)
    engine = create_engine(url)
    monkeypatch.setattr(db_session, "SessionLocal", db_session.make_session_factory(engine))

    import worker.crawl_job as job

    monkeypatch.setattr(job, "_get_provider", lambda: HashingEmbedder(dimension=1024))
    # The crawler is tested separately; keep this test off the network delay path.
    monkeypatch.setattr(job.crawl.__globals__["asyncio"], "sleep", _no_sleep)

    tenant_id, source_id = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s,'crawl-tenant')", (tenant_id,))
        conn.execute(
            "INSERT INTO knowledge_sources (id, tenant_id, type) VALUES (%s,%s,'crawl')",
            (source_id, tenant_id),
        )
    try:
        yield job, tenant_id, source_id, server, migrated_db
    finally:
        with psycopg.connect(_admin_dsn(migrated_db), autocommit=True) as conn:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        engine.dispose()


async def _no_sleep(*_args, **_kwargs) -> None:
    return None


def _documents(migrated_db: str, tenant_id) -> int:
    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        return conn.execute(
            "SELECT count(*) FROM documents WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()[0]


def test_crawl_creates_one_document_per_page(workspace) -> None:
    job, tenant_id, source_id, server_url, migrated_db = workspace

    result = asyncio.run(
        job.crawl_source({}, str(tenant_id), str(source_id), f"{server_url}/")
    )

    assert result["crawled"] == 2
    assert result["created"] == 2
    assert _documents(migrated_db, tenant_id) == 2


def test_recrawl_skips_unchanged_pages(workspace) -> None:
    """A weekly recrawl must not re-embed a site that has not changed."""
    job, tenant_id, source_id, server_url, migrated_db = workspace

    asyncio.run(job.crawl_source({}, str(tenant_id), str(source_id), f"{server_url}/"))
    second = asyncio.run(
        job.crawl_source({}, str(tenant_id), str(source_id), f"{server_url}/")
    )

    assert second["created"] == 0
    assert second["unchanged"] == 2
    assert _documents(migrated_db, tenant_id) == 2, "recrawl must not duplicate documents"


def test_crawl_reuses_the_document_row_for_the_same_url(workspace) -> None:
    job, tenant_id, source_id, server_url, migrated_db = workspace

    asyncio.run(job.crawl_source({}, str(tenant_id), str(source_id), f"{server_url}/"))

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        uris = [
            r[0]
            for r in conn.execute(
                "SELECT uri FROM documents WHERE tenant_id = %s ORDER BY uri", (tenant_id,)
            )
        ]
    assert uris == [f"{server_url}/", f"{server_url}/returns"]


def test_single_page_failure_is_reported_not_fatal(workspace, monkeypatch) -> None:
    job, tenant_id, source_id, server_url, migrated_db = workspace

    real_ingest = job.ingest_html

    def flaky(payload):
        if b"Thirty days" in payload:
            raise RuntimeError("page blew up")
        return real_ingest(payload)

    monkeypatch.setattr(job, "ingest_html", flaky)
    result = asyncio.run(
        job.crawl_source({}, str(tenant_id), str(source_id), f"{server_url}/")
    )

    assert result["created"] == 1
    assert len(result["failed"]) == 1
    assert "/returns" in result["failed"][0]["url"]
