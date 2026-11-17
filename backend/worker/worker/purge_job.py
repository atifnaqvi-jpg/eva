"""arq cron job: delete conversations past their tenant's retention window (SEC-09).

ARCHITECTURE 19.3 requires a *scheduled* purge and says it must be verified to delete
what it claims, so this job is written to be checkable rather than merely to run:

* **It works tenant by tenant, inside that tenant's RLS context.** The worker connects
  as ``eva_app``, which is subject to row-level security, so a single
  ``DELETE FROM conversations WHERE started_at < ...`` would silently match nothing —
  no error, just a purge that reports success and deletes zero rows forever. Iterating
  the tenants (which is not RLS-protected) and opening a ``tenant_session`` per tenant
  is what makes the delete actually see rows.
* **Only finished conversations are eligible.** An ``active`` conversation is one
  somebody may still be having, so it is kept however old it is; the retention clock
  starts at ``last_activity_at``, not at creation, or a long support thread would be
  deleted mid-conversation.
* **It reports what it deleted, per tenant.** The job's return value is the audit
  trail: a test and an operator can both assert that rows went away, and a purge that
  quietly deletes nothing is indistinguishable from a bug until something is reported.

Deleting the conversation row is enough for its own data: ``messages``, ``feedback``,
``handovers`` and ``visitor_files`` all cascade on ``conversation_id``. Two things
deliberately survive: ``usage_records`` (its ``conversation_id`` is set to NULL, so the
tenant's billing history keeps its totals without keeping the conversation) and
``audit_logs`` (§19.3: the audit log is never purged).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.session import tenant_session
from app.retention import policy

logger = logging.getLogger(__name__)

# Which tenant is being purged is a decision for the loop below, not for the caller, so
# this takes no arguments and is safe to schedule blind.
ELIGIBLE_STATUSES = ("closed", "handed_over")


def _tenants(session) -> list[dict]:
    """Every tenant with its retention window.

    ``tenants`` carries no RLS policy, so this is readable from the app role and is the
    only query here that intentionally spans tenants.
    """
    rows = session.execute(
        text("SELECT id::text AS id, settings FROM tenants ORDER BY created_at")
    ).mappings()
    return [
        {"id": row["id"], "days": policy.settings(row["settings"])["conversation_days"]}
        for row in rows
    ]


async def purge_expired(ctx: dict) -> dict:
    """Delete every conversation older than its tenant's retention permits."""
    from app.db.session import SessionLocal

    with SessionLocal() as session:
        session.begin()
        tenants = _tenants(session)
        session.commit()

    deleted_total = 0
    per_tenant: dict[str, int] = {}

    for tenant in tenants:
        with tenant_session(tenant["id"]) as session:
            result = session.execute(
                text(
                    "DELETE FROM conversations"
                    " WHERE status = ANY(:statuses)"
                    "   AND last_activity_at < now() - make_interval(days => :days)"
                ),
                {"statuses": list(ELIGIBLE_STATUSES), "days": tenant["days"]},
            )
            deleted = result.rowcount or 0
            session.commit()

        per_tenant[tenant["id"]] = deleted
        deleted_total += deleted

    report = {"tenants": len(tenants), "deleted": deleted_total, "per_tenant": per_tenant}
    logger.info("retention purge: %s", report)
    return report
