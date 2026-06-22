"""Dead-worker sweep + lease-expired task recovery.

Ported from node_modified.py (``zombie_sweeper`` at lines
5921-6001).

Every 5 seconds:

1. Drops workers that haven't heartbeated in > 15s from
   :data:`STATE.active_workers` and flips their presence to offline.
2. For each dead worker, any ``processing`` task owned by it is either
   re-queued via ``try_schedule_retry`` or marked failed (retry exhausted).
3. Independently, walks every ``processing`` task and re-queues those with
   expired leases (``task_lease_expired``).
4. Broadcasts a single ``task_available`` ping to every connected worker
   if anything was requeued.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from nexus.core import STATE
from nexus.storage import TaskRecord, get_session
from nexus.telemetry import presence
from nexus.telemetry.alerts import push_alert
from nexus.telemetry.metrics import incr_metric
from nexus.utils.time import timestamp

_log = logging.getLogger("nexus.telemetry.zombie_sweeper")


async def zombie_sweeper() -> None:
    """Background loop: reap dead workers and recover their tasks."""
    # Deferred imports to break the telemetry ↔ tasks / networking / scheduler cycle.
    from nexus.networking.connection_manager import ws_manager
    from nexus.scheduler.reliability import record_worker_outcome
    from nexus.tasks.lease import task_lease_expired
    from nexus.tasks.lifecycle import set_task_status, try_schedule_retry

    while True:
        now = time.time()
        async with STATE.worker_state_lock:
            dead_workers = [
                ip
                for ip, data in list(STATE.active_workers.items())
                if now - data["last_seen"] > 15
            ]
            for ip in dead_workers:
                del STATE.active_workers[ip]
        for ip in dead_workers:
            presence.mark_peer_offline(ip, source="timeout")

        requeued_count = 0
        if dead_workers:
            async with get_session() as db:
                for ip in dead_workers:
                    incr_metric("worker_disconnects")
                    push_alert(
                        "warning",
                        "worker_disconnect",
                        f"Worker {ip} disconnected.",
                    )
                    for task in (
                        (
                            await db.execute(
                                select(TaskRecord).filter(
                                    TaskRecord.worker == ip,
                                    TaskRecord.status == "processing",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    ):
                        task.worker = None
                        record_worker_outcome(ip, ok=False)
                        if try_schedule_retry(
                            task, f"Worker {ip} disconnected.", ip
                        ):
                            requeued_count += 1
                        else:
                            set_task_status(
                                task,
                                "failed",
                                f"Worker {ip} disconnected and retry budget "
                                "exhausted.",
                                force=True,
                            )
                            incr_metric("tasks_failed")
                        task.logs = (task.logs or "") + (
                            f"[{timestamp()}] [SYSTEM] Node {ip} crashed. "
                            "Recovery policy applied.\n"
                        )

                # Lease-expired sweep — independent of dead-worker detection
                lease_tasks = (
                    (
                        await db.execute(
                            select(TaskRecord).filter(
                                TaskRecord.status == "processing"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for task in lease_tasks:
                    if not task_lease_expired(task):
                        continue
                    previous_worker = task.worker
                    task.worker = None
                    record_worker_outcome(previous_worker, ok=False)
                    set_task_status(
                        task,
                        "lease_expired",
                        f"Lease expired for worker {previous_worker}.",
                        force=True,
                    )
                    if try_schedule_retry(
                        task, "Lease expired.", previous_worker
                    ):
                        requeued_count += 1
                    else:
                        set_task_status(
                            task,
                            "failed",
                            "Lease expired and retry budget exhausted.",
                            force=True,
                        )
                        incr_metric("tasks_failed")
                await db.commit()

        if requeued_count > 0:
            incr_metric("tasks_recovered")
            await ws_manager.broadcast_ping()
        await asyncio.sleep(5)


__all__ = ["zombie_sweeper"]
