"""Database session with tenant scoping (§16.3).

Every transaction sets ``app.tenant_id`` from the authenticated context before the
first statement, so RLS policies see it. Nothing here derives the tenant from
request input.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import APP_DATABASE_URL, TENANT_SETTING

engine = create_engine(APP_DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def tenant_session(tenant_id: UUID) -> Iterator[Session]:
    """Open a session scoped to ``tenant_id`` for the life of the transaction.

    The GUC is transaction-local, so a pooled connection can never carry a
    previous tenant's context into the next request.
    """
    session = SessionLocal()
    try:
        session.begin()
        # set_config(..., true) is transaction-local and, unlike SET, accepts a
        # bind parameter, so the tenant id is never string-interpolated into SQL.
        session.execute(
            text("SELECT set_config(:name, :tid, true)"),
            {"name": TENANT_SETTING, "tid": str(tenant_id)},
        )
        yield session
    finally:
        session.close()
