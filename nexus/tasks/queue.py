"""Task queue helpers around :data:`nexus.core.STATE.task_queue`.

Extracted from Phase-1/node_modified.py (line 464 + every ``TASK_QUEUE.put``
call site: 5640, 5743, 5761, 5787, 6066, 7256, 7355, 8188).

The queue itself is a plain :class:`asyncio.Queue` of task-id strings. This
module centralizes mutation so callers don't reach through ``STATE``
directly — that keeps the "task_queue is owned by tasks.queue" invariant
from breaking as more subpackages land.

Readers (scheduler, api) may still read ``STATE.task_queue.qsize()``
directly for diagnostics — that is a non-mutating observation.
"""

from __future__ import annotations

from nexus.core import STATE


async def enqueue_task(task_id: str) -> None:
    """Put *task_id* onto the dispatch queue."""
    await STATE.task_queue.put(task_id)


async def dequeue_task() -> str:
    """Await the next task id off the dispatch queue. Blocks until available."""
    return await STATE.task_queue.get()


def queue_depth() -> int:
    """Return the approximate number of task ids currently queued."""
    return STATE.task_queue.qsize()


def queue_empty() -> bool:
    """Non-blocking check: ``True`` when the dispatch queue is empty."""
    return STATE.task_queue.empty()


__all__ = [
    "enqueue_task",
    "dequeue_task",
    "queue_depth",
    "queue_empty",
]
