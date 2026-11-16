import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import router as admin_router
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
from app.conversation.router import router as conversation_router
from app.settings.router import router as settings_router
from app.widget.router import router as widget_router
from app.knowledge.router import router as knowledge_router
from app.voice.router import router as voice_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.security.passwords import BACKEND

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

app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(audit_router, prefix="/api/v1")
app.include_router(voice_router, prefix="/api/v1")
app.include_router(knowledge_router, prefix="/api/v1")
app.include_router(conversation_router, prefix="/api/v1")
app.include_router(widget_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")


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
