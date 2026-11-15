import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.router import router as admin_router
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.security.passwords import BACKEND

    if BACKEND == "pure-python":
        logger.warning(
            "Password hashing uses the in-repo pure-Python Argon2id. At the "
            "configured cost parameters this takes minutes per hash and needs "
            "~350 MiB peak RSS, so logins are not serviceable. Pending HALT-01 "
            "(PLAN 8.1) for the argon2-cffi runtime dependency."
        )
    yield


app = FastAPI(title="EVA API", version="0.1.0", lifespan=lifespan)

app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(audit_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    # DB/Redis checks land in WP-02; stub until the schema exists.
    return {"status": "ok", "database": "stub"}
