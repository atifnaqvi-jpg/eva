import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import router as admin_router
from app.audit.router import router as audit_router
from app.botconfig.router import router as botconfig_router
from app.auth.router import router as auth_router
from app.conversation.router import router as conversation_router
from app.conversation.archive import router as archive_router
from app.conversation.erasure import router as erasure_router
from app.analytics.router import router as analytics_router
from app.settings.router import router as settings_router
from app.handover.webhook_router import router as webhook_router
from app.widget.router import router as widget_router
from app.knowledge.router import router as knowledge_router
from app.voice.router import router as voice_router
from app.observability import logs as obs_logs

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.security.passwords import BACKEND

    # OPS-01. Configured here rather than at import: importing `app.main` in a test
    # must not install a process-wide stdout handler that swallows pytest's output.
    obs_logs.configure()

    if BACKEND != "argon2-cffi":
        logger.warning(
            "Password hashing fell back to the pure-Python Argon2id (%s): "
            "argon2-cffi is not installed. This backend is correct but takes "
            "minutes per hash at production parameters, so logins are not "
            "serviceable. Install the runtime dependency (PLAN 8.1, HALT-01).",
            BACKEND,
        )
    yield


app = FastAPI(title="EVA API", version="0.1.0", lifespan=lifespan)


def _widget_origins() -> list[str]:
    """Origins the widget may call from.

    The widget is embedded on the *tenant's* domain while the API lives on ours, so
    every widget request is cross-origin and the browser needs
    `Access-Control-Allow-Origin` before it will even send it. Without this the
    widget works only on a host page served from the API's own origin -- i.e. on the
    demo page and nowhere a customer could actually put it.

    `*` is the correct value here rather than a tenant allowlist, and it is safe for
    a specific reason: the widget's authorization is a **bearer token**, never a
    cookie. An `Origin: *` policy lets any page *send* a request; it does not let a
    page read a response it has no token for, and there is no ambient credential to
    ride. Whether a given origin may mint a token at all is decided per-tenant by
    `origin_matches()` against `bot_domains` (SEC/WIDG-05), which is the real check.
    Using `allow_credentials=True` with `*` is exactly what the CORS spec forbids, so
    credentials stay off.

    Scoped to the widget paths: the dashboard and admin APIs are same-origin, and a
    blanket CORS policy there would widen the surface for no benefit.
    """
    raw = os.environ.get("WIDGET_ALLOWED_ORIGINS", "*")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class WidgetCORS(CORSMiddleware):
    """CORS for widget paths only.

    Implemented by deciding in `__call__` rather than by mounting a second app: the
    widget routes and the dashboard routes share the same routers, so a path prefix
    is the only clean discriminator.
    """

    def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http" and scope.get("path", "").startswith(
            ("/api/v1/widget", "/widget")
        ):
            return super().__call__(scope, receive, send)
        return self.app(scope, receive, send)


app.add_middleware(
    WidgetCORS,
    allow_origins=_widget_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    # The visitor upload endpoint sends multipart bodies; the response headers a
    # browser may read are limited to what the widget actually inspects.
    expose_headers=["Content-Type"],
    max_age=600,
)

# Outermost, so CORS preflights and 500s from the routers are logged as themselves and
# every request gets a correlation id (OPS-01). Added last because Starlette's
# middleware stack is built in reverse: the last `add_middleware` is the outer one.
app.add_middleware(obs_logs.RequestContextMiddleware)

app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(audit_router, prefix="/api/v1")
app.include_router(voice_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(conversation_router, prefix="/api/v1")
app.include_router(botconfig_router, prefix="/api/v1")
app.include_router(archive_router, prefix="/api/v1")
app.include_router(erasure_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(widget_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
app.include_router(webhook_router, prefix="/api/v1")


# Built widget assets (WIDG-01/02). Served from the API origin rather than a second
# CDN: the iframe is then same-origin with the API it calls, and the MVP has one
# deployable instead of two. Absent in a source checkout, so the mount is
# conditional -- a missing build must not stop the API from booting.
_WIDGET_DIR = Path(os.environ.get("WIDGET_ASSET_DIR", "/srv/widget"))

if (_WIDGET_DIR / "loader.js").is_file():

    # Route before mount, deliberately: Starlette matches in declaration order, so a
    # `Mount("/widget")` declared first would swallow `/widget/app` and 404 it as a
    # missing file called "app". This ordering was a live 404 on the first attempt.
    @app.get("/widget/app", include_in_schema=False)
    def widget_app() -> FileResponse:
        return FileResponse(
            _WIDGET_DIR / "index.html",
            media_type="text/html",
            headers={
                # Short TTL: a release is picked up without a hard refresh, but a
                # stale shell is not served indefinitely.
                "Cache-Control": "public, max-age=300",
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.mount("/widget", StaticFiles(directory=str(_WIDGET_DIR)), name="widget")


if (_WIDGET_DIR / "demo" / "host.html").is_file():

    @app.get("/demo/host", include_in_schema=False)
    def demo_host(request: Request) -> HTMLResponse:
        """The M3 gate's host page, with the bot id from `?bot=` substituted.

        Served by the API rather than a static server so the page's Origin is the one
        the widget's domain check verifies. A demo, not a product surface: it renders
        synthetic fixture content only.
        """
        page = (_WIDGET_DIR / "demo" / "host.html").read_text(encoding="utf-8")
        bot = request.query_params.get("bot", "")
        # Never interpolate unvalidated input into HTML. A public bot id has a
        # restricted charset, so anything else is refused rather than escaped.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", bot):
            return HTMLResponse(
                "<!doctype html><p>Add <code>?bot=PUBLIC_BOT_ID</code> to this URL.</p>",
                status_code=400,
            )
        return HTMLResponse(
            page.replace("BOT_PUBLIC_ID", bot),
            headers={"Cache-Control": "no-store"},
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    # DB/Redis checks land in WP-02; stub until the schema exists.
    return {"status": "ok", "database": "stub"}
