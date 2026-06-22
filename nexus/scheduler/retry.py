"""Background loop that re-queues retrying tasks once their backoff elapses.

Extracted from Phase-1/node_modified.py (lines 5769-5797).

The retry loop polls tasks in the ``retrying`` state and, for each one
whose ``NEXUS_META_NEXT_RETRY_AT`` has passed, transitions it back to
``queued`` and enqueues the id.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from nexus.core import events
from nexus.storage import TaskRecord, get_session
from nexus.tasks.lifecycle import set_task_status
from nexus.tasks.metadata import task_retry_at
from nexus.tasks.queue import enqueue_task
from nexus.telemetry.metrics import incr_metric
from nexus.utils import now_epoch

_log = logging.getLogger("nexus.scheduler.retry")


async def retry_scheduler_loop(poll_seconds: float = 1.5) -> None:
    """Forever: sweep ``retrying`` tasks and re-queue any whose backoff elapsed."""
    while True:
        now = now_epoch()
        queued_now = 0
        async with get_session() as db:
            retrying_tasks = (
                (
                    await db.execute(
                        select(TaskRecord).filter(TaskRecord.status == "retrying")
                    )
                )
                .scalars()
                .all()
            )
            for task in retrying_tasks:
                if task_retry_at(task) > now:
                    continue
                if set_task_status(task, "queued", "Retry backoff elapsed."):
                    await enqueue_task(task.id)
                    incr_metric("tasks_requeued")
                    queued_now += 1
                    _log.info("Retry scheduler: task %s re-queued after backoff.", task.id)
            if queued_now:
                await db.commit()
            else:
                await db.rollback()
        if queued_now:
            events.publish("scheduler.requeued", {"count": queued_now})
        await asyncio.sleep(poll_seconds)


__all__ = ["retry_scheduler_loop"]
