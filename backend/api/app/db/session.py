"""Database session with tenant scoping (§16.3).

Every transaction sets ``app.tenant_id`` from the authenticated context before the
first statement, so RLS policies see it. Nothing here derives the tenant from
request input.

The GUC is set with ``set_config(..., true)``, which is transaction-local — the
right default, since a pooled connection must never carry a previous tenant's
context into the next transaction. The consequence is that an explicit
``commit()`` mid-workflow ends that transaction and clears the GUC, after which
RLS silently matches nothing. The ``after_begin`` hook re-applies it to every
transaction the session opens, so partial commits stay correct.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import APP_DATABASE_URL, TENANT_SETTING


def _apply_tenant_setting(session: Session, transaction, connection) -> None:
    tenant_id = session.info.get(TENANT_SETTING)
    if tenant_id is None:
        return
    # set_config accepts a bind parameter, unlike SET, so the tenant id is never
    # string-interpolated into SQL.
    connection.execute(
        text("SELECT set_config(:name, :tid, true)"),
        {"name": TENANT_SETTING, "tid": tenant_id},
    )


def make_session_factory(bind) -> sessionmaker:
    """Session factory that re-applies the tenant GUC on every transaction."""
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    event.listen(factory, "after_begin", _apply_tenant_setting)
    return factory


engine = create_engine(APP_DATABASE_URL, pool_pre_ping=True)
SessionLocal = make_session_factory(engine)


@contextmanager
def tenant_session(tenant_id: UUID) -> Iterator[Session]:
    """Open a session scoped to ``tenant_id`` for the life of the transaction."""
    session = SessionLocal()
    session.info[TENANT_SETTING] = str(tenant_id)
    try:
        session.begin()
        yield session
    finally:
        session.close()
