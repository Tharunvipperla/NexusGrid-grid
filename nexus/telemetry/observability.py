"""Periodic queue / metric / alert / retention sampling loop.

Ported from node_modified.py (``observability_loop`` at lines
5800-5918).

Runs every 5 seconds. Each tick:

1. Samples queue depth + processing depth + active-worker count into
   :data:`nexus.core.STATE.metrics`.
2. Emits a ``queue_stall`` alert when there are queued tasks but no active
   workers and the oldest queued task is over 20 seconds old.
3. Enforces ``NEXUS_META_QUEUE_TIMEOUT`` — fails tasks queued too long.
4. Expires stale consent-mode offers and bumps the worker's strike count.
5. Prunes ``AuditEvent`` + ``PresenceEvent`` rows older than
   ``audit_retention_days``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, func, select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.storage import AuditEvent, PresenceEvent, TaskRecord, get_session
from nexus.telemetry import presence
from nexus.telemetry.alerts import push_alert
from nexus.telemetry.metrics import incr_metric
from nexus.utils.time import now_epoch, timestamp

_log = logging.getLogger("nexus.telemetry.observability")


def _active_workers_metric_value() -> int:
    """P3: live count of workers we can actually reach right now.

    ``STATE.active_workers`` is reaped on a 15 s interval by the zombie
    sweeper, so a stale entry can linger across one or two observability
    ticks after a worker drops. Filtering by presence (instant on bye /
    WS-disconnect) and by ``last_seen`` age keeps the metric honest
    without waiting for the sweeper.
    """
    now_ts = time.time()
    return sum(
        1
        for ip, data in STATE.active_workers.items()
        if not presence.is_peer_offline(ip)
        and (now_ts - float(data.get("last_seen", 0) or 0)) <= 15
    )


async def observability_loop() -> None:
    """Background loop: sample metrics, expire offers, prune retention."""
    # Deferred import — ``nexus.tasks`` depends on ``nexus.telemetry`` (for
    # ``incr_metric``), so importing at module load would form a cycle when
    # ``telemetry.__init__`` pulls us in.
    from nexus.tasks.lifecycle import add_task_timeline_event, set_task_status
    from nexus.tasks.metadata import parse_task_env, task_created_at

    while True:
        try:
            async with get_session() as db:
                queued_count = (
                    await db.execute(
                        select(func.count(TaskRecord.id)).filter(
                            TaskRecord.status == "queued"
                        )
                    )
                ).scalar() or 0
                processing_count = (
                    await db.execute(
                        select(func.count(TaskRecord.id)).filter(
                            TaskRecord.status == "processing"
                        )
                    )
                ).scalar() or 0
            STATE.metrics["queue_depth"] = queued_count
            STATE.metrics["processing_depth"] = processing_count
            STATE.metrics["active_workers"] = _active_workers_metric_value()

            if queued_count > 0:
                now_ts_q = now_epoch()
                async with get_session() as db:
                    queued_tasks = (
                        (
                            await db.execute(
                                select(TaskRecord).filter(
                                    TaskRecord.status == "queued"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not STATE.active_workers and queued_tasks:
                        oldest_age = max(
                            0,
                            now_ts_q
                            - min(
                                task_created_at(t) or now_ts_q
                                for t in queued_tasks
                            ),
                        )
                        if oldest_age > 20:
                            push_alert(
                                "warning",
                                "queue_stall",
                                f"Queue stalled for {int(oldest_age)}s with no "
                                "active workers.",
                            )
                    # Queue timeout: fail tasks queued too long
                    timed_out = 0
                    for task in queued_tasks:
                        queue_timeout = float(
                            parse_task_env(task).get("NEXUS_META_QUEUE_TIMEOUT", 0)
                            or 0
                        )
                        if queue_timeout <= 0:
                            continue
                        queued_at = float(
                            parse_task_env(task).get("NEXUS_META_QUEUED_AT", 0)
                            or 0
                        )
                        if queued_at <= 0:
                            continue
                        elapsed = now_ts_q - queued_at
                        if elapsed > queue_timeout:
                            set_task_status(
                                task,
                                "failed",
                                f"Queue timeout ({int(queue_timeout)}s) "
                                "exceeded — no worker picked up the task.",
                                force=True,
                            )
                            task.logs = (task.logs or "") + (
                                f"[{timestamp()}] [MASTER] Task timed out in "
                                f"queue after {int(elapsed)}s. Re-queue manually "
                                "if needed.\n"
                            )
                            add_task_timeline_event(
                                task,
                                "queue_timeout",
                                f"{int(queue_timeout)}s elapsed",
                            )
                            timed_out += 1
                    if timed_out:
                        await db.commit()
                        incr_metric("tasks_queue_timeout")

            # Expire stale consent offers
            now_ts = time.time()
            async with STATE.pending_offers_lock:
                expired_offers = [
                    (tid, offer)
                    for tid, offer in STATE.pending_task_offers.items()
                    if now_ts - offer["offered_at"] > offer["timeout"]
                ]
                for tid, offer in expired_offers:
                    del STATE.pending_task_offers[tid]
            if expired_offers:
                max_strikes = int(LOCAL_SETTINGS.get("consent_max_strikes", 3) or 3)
                async with get_session() as db:
                    for tid, offer in expired_offers:
                        wid = offer["worker_id"]
                        STATE.consent_strikes[(tid, wid)] = (
                            STATE.consent_strikes.get((tid, wid), 0) + 1
                        )
                        strikes = STATE.consent_strikes[(tid, wid)]
                        task = (
                            await db.execute(
                                select(TaskRecord).filter(TaskRecord.id == tid)
                            )
                        ).scalar_one_or_none()
                        if task and task.status == "queued":
                            strike_note = (
                                f" (strike {strikes}/{max_strikes})"
                                if max_strikes > 0
                                else ""
                            )
                            task.logs = (task.logs or "") + (
                                f"[{timestamp()}] [DISPATCH] Offer to {wid} "
                                f"expired after {offer['timeout']}s"
                                f"{strike_note}. Re-entering pool.\n"
                            )
                            add_task_timeline_event(task, "offer_expired", wid)
                    await db.commit()
                incr_metric("task_offers_expired")

            # Retention cleanup
            retention_days = int(
                LOCAL_SETTINGS.get("audit_retention_days", 7) or 7
            )
            cutoff = str(now_epoch() - retention_days * 86400)
            async with get_session() as db:
                await db.execute(delete(AuditEvent).where(AuditEvent.ts < cutoff))
                await db.execute(
                    delete(PresenceEvent).where(PresenceEvent.ts < cutoff)
                )
                await db.commit()
        except Exception:
            _log.debug("Observability loop error", exc_info=True)
        await asyncio.sleep(5)


__all__ = ["observability_loop"]
