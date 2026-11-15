from fastapi import FastAPI

from app.admin.router import router as admin_router
from app.auth.router import router as auth_router

app = FastAPI(title="EVA API", version="0.1.0")

app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    # DB/Redis checks land in WP-02; stub until the schema exists.
    return {"status": "ok", "database": "stub"}
