import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.router import router as admin_router
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    # DB/Redis checks land in WP-02; stub until the schema exists.
    return {"status": "ok", "database": "stub"}
