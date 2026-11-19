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
* **It leaves a durable receipt, because the return value does not last.** arq keeps a
  job result for ``keep_result`` seconds (measured: 3600), so "what did the purge
  remove three days ago" has no answer from the job itself. §27 asks for the purge to be
  *verified* to delete what it claims, which needs a record that outlives the process.
  Each tenant's run therefore writes one ``retention.purged`` row on **the same
  transaction as its delete**, so a delete cannot commit without its receipt and a
  rolled-back delete cannot leave one claiming it happened.

**A receipt is written even when nothing was deleted,** and that is the point of the
report rather than a side effect. A quiet day and a dead cron both produce zero deletions;
only a row saying "this tenant was examined and 0 conversations were past its window"
tells them apart. The cost is stated rather than hidden: ≤50 tenants (§20.2) at one row a
day, so the audit log grows by a bounded trickle that is itself the evidence a compliance
review asks for.

Deleting the conversation row is enough for its own data: ``messages``, ``feedback``,
``handovers`` and ``visitor_files`` all cascade on ``conversation_id``. Two things
deliberately survive: ``usage_records`` (its ``conversation_id`` is set to NULL, so the
tenant's billing history keeps its totals without keeping the conversation) and
``audit_logs`` (§19.3: the audit log is never purged).

**The receipt carries counts, never the data it counts.** ``audit_logs`` is append-only
and never purged, so naming a visitor's session id, address or message text there would
deposit it in the one table nothing can delete — the trap SEC-10's erasure route is built
around, in the place where a purge is most tempted to quote its own work.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.audit.service import Action, record
from app.db.session import tenant_session
from app.observability import logs as obs_logs
from app.retention import policy

logger = logging.getLogger(__name__)

# Which tenant is being purged is a decision for the loop below, not for the caller, so
# this takes no arguments and is safe to schedule blind.
ELIGIBLE_STATUSES = ("closed", "handed_over")

# Counted before the delete, so the receipt can say what went. After the cascade the
# children are gone and "we deleted something" is not evidence — an account of what went
# is. Same shape SEC-10's erasure uses, for the same reason.
_COUNT_CHILDREN = text(
    """
    SELECT (SELECT count(*) FROM conversations
             WHERE tenant_id = :tenant_id
               AND status = ANY(:statuses)
               AND last_activity_at < now() - make_interval(days => :days))
                                                                          AS conversations,
           (SELECT count(*) FROM messages m
             WHERE m.tenant_id = :tenant_id
               AND m.conversation_id IN (
                   SELECT id FROM conversations
                    WHERE tenant_id = :tenant_id
                      AND status = ANY(:statuses)
                      AND last_activity_at < now() - make_interval(days => :days)))
                                                                          AS messages,
           (SELECT count(*) FROM feedback f
              JOIN messages m ON m.id = f.message_id AND m.tenant_id = f.tenant_id
             WHERE f.tenant_id = :tenant_id
               AND m.conversation_id IN (
                   SELECT id FROM conversations
                    WHERE tenant_id = :tenant_id
                      AND status = ANY(:statuses)
                      AND last_activity_at < now() - make_interval(days => :days)))
                                                                          AS feedback,
           (SELECT count(*) FROM handovers h
             WHERE h.tenant_id = :tenant_id
               AND h.conversation_id IN (
                   SELECT id FROM conversations
                    WHERE tenant_id = :tenant_id
                      AND status = ANY(:statuses)
                      AND last_activity_at < now() - make_interval(days => :days)))
                                                                          AS handovers,
           (SELECT count(*) FROM visitor_files vf
             WHERE vf.tenant_id = :tenant_id
               AND vf.conversation_id IN (
                   SELECT id FROM conversations
                    WHERE tenant_id = :tenant_id
                      AND status = ANY(:statuses)
                      AND last_activity_at < now() - make_interval(days => :days)))
                                                                          AS visitor_files
    """
)



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
        # OPS-01: scoped, not bare, so the summary line below is not attributed to the
        # last tenant in the loop.
        with obs_logs.scoped(tenant_id=tenant["id"]):
            with tenant_session(tenant["id"]) as session:
                params = {
                    "tenant_id": tenant["id"],
                    "statuses": list(ELIGIBLE_STATUSES),
                    "days": tenant["days"],
                }
                # Counted before the delete: the receipt is the only durable record of
                # what went, and it cannot be written from the rows afterwards.
                counts = session.execute(_COUNT_CHILDREN, params).mappings().one()
                result = session.execute(
                    text(
                        "DELETE FROM conversations"
                        " WHERE tenant_id = :tenant_id"
                        "   AND status = ANY(:statuses)"
                        "   AND last_activity_at < now() - make_interval(days => :days)"
                    ),
                    params,
                )
                deleted = result.rowcount or 0
                if deleted != counts["conversations"]:
                    # The count and the delete read the same predicate in the same
                    # transaction, so a mismatch means something else is deleting
                    # concurrently. Say so rather than recording a receipt that does not
                    # describe what happened.
                    logger.error(
                        "retention purge: counted %d but deleted %d",
                        counts["conversations"],
                        deleted,
                    )

                # The receipt rides the delete's transaction: committed together, so a
                # crash cannot leave a "purged 12" row beside twelve surviving
                # conversations, nor a deletion with no record that it happened.
                record(
                    session,
                    tenant_id=tenant["id"],
                    action=Action.RETENTION_PURGED,
                    entity_type="retention",
                    detail={
                        "deleted": deleted,
                        "conversations": int(counts["conversations"]),
                        "messages": int(counts["messages"]),
                        "feedback": int(counts["feedback"]),
                        "handovers": int(counts["handovers"]),
                        "visitor_files": int(counts["visitor_files"]),
                        "retention_days": tenant["days"],
                        "eligible_statuses": list(ELIGIBLE_STATUSES),
                    },
                )
                session.commit()
                if deleted:
                    # Inside the scope: a deletion count without the tenant id is the
                    # one line an operator cannot act on.
                    logger.info("retention purge: %d conversation(s) deleted", deleted)

        per_tenant[tenant["id"]] = deleted
        deleted_total += deleted

    report = {"tenants": len(tenants), "deleted": deleted_total, "per_tenant": per_tenant}
    logger.info("retention purge: %s", report)
    return report
