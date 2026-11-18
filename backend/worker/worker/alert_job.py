"""arq cron job: evaluate the five §22 alerts and email the ones that transition (OPS-04).

The poller is a *second* cron beside ``purge_expired`` rather than a compose service:
HALT-09 measured that everything it needs is already in the worker image (``redis`` is a
hard ``Requires-Dist`` of the pinned ``arq``, ``shutil.disk_usage`` is stdlib, and OPS-09
writes its manifests to a directory the worker can mount), so a new container would be a
new supply-chain surface for one loop that runs in a minute.

**The decisions live in ``app.observability.alerts``, not here.** This file is the schedule
and the wiring — thresholds, the transition rule and the delivery channel are in the API
package, so the API and the poller cannot answer the same question differently.

**Per-process state, stated rather than hidden.** ``alerts._STATE`` remembers what was last
announced, so a still-active warning is not re-mailed every minute. It lives in the worker
process, so a restart re-announces an active alert and restarts the queue-backlog window.
Redis could hold it; using Redis would mean an alerting loop that cannot report a Redis
outage because it needs Redis to report it. OPS-03's counters make the same trade.
"""

from __future__ import annotations

import asyncio
import logging

from app.observability import alerts

logger = logging.getLogger(__name__)


async def check_alerts(ctx: dict) -> dict:
    """One sweep. The report is the job's stored result, so a quiet system is checkable.

    The sweep itself is sync — ``shutil.disk_usage``, SQL through the tenant sessions,
    SMTP — so it runs in a thread. Inline would block the worker's event loop, and
    therefore ingestion, for the length of an SMTP timeout.
    """
    report = await asyncio.to_thread(alerts.run_once)
    logger.info(
        "alert sweep complete: worst=%s sent=%d recovered=%d",
        report["worst"],
        len(report["sent"]),
        len(report["recovered"]),
    )
    return report
