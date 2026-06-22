"""Per-task rolling log buffers.

Extracted from node_modified.py (lines 482-484, 2104-2175).

Each running task gets an in-memory ``deque`` capped at
:data:`nexus.core.constants.MAX_LOG_LINES` lines. The UI tails the buffer
over the WebSocket channel using a ``since`` cursor so it only receives new
lines since its last poll. Buffers are dropped 30 s after the task's result
is submitted (see :func:`clear_local_task_log`) so idle RAM doesn't grow
unbounded in long-lived masters.

:class:`LogStream` preserves the the original implementation ``elastic_log += "..."`` pattern so
callers don't have to rewrite their string concatenations when they adopt
structured logging later.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.core.constants import MAX_LOG_LINES
from nexus.core.state import STATE

_log = logging.getLogger("nexus.telemetry.logs")


class LogStream:
    """Mutable log accumulator that mirrors writes into the shared buffer.

    Use as a drop-in replacement for a plain ``str`` that is concatenated
    with ``+=``::

        elog = LogStream(task_id)
        elog += "starting container\\n"
        elog += stdout
        final_text = str(elog)
    """

    __slots__ = ("task_id", "parts")

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.parts: list[str] = []

    def __iadd__(self, msg: Any) -> "LogStream":
        if msg is None:
            return self
        if not isinstance(msg, str):
            msg = str(msg)
        self.parts.append(msg)
        try:
            asyncio.get_event_loop().create_task(task_log_append(self.task_id, msg))
        except RuntimeError:
            pass
        # Batch D1: if this LogStream is owned by a worker executing for a
        # remote master, forward the chunk so the master's live tail
        # endpoint can show output in real time. Best-effort, never raises.
        try:
            from nexus.networking import log_forwarder

            log_forwarder.enqueue_chunk(self.task_id, msg)
        except Exception:
            pass
        return self

    def __str__(self) -> str:
        return "".join(self.parts)

    def __add__(self, other: Any) -> str:
        return str(self) + (other if isinstance(other, str) else str(other))

    def __radd__(self, other: Any) -> str:
        return (other if isinstance(other, str) else str(other)) + str(self)

    def __len__(self) -> int:
        return sum(len(p) for p in self.parts)


def unstreamed_tail(full: bytes, already: int) -> tuple[bytes, int]:
    """Incremental live-tail of a growing capture file.

    The Docker runner redirects a task's stdout to a file inside the container
    and re-reads it every poll. Given the *full* current contents and how many
    bytes were *already* emitted, return the not-yet-emitted tail plus the new
    high-water mark. Resyncs from 0 if the file shrank (rotation/truncation),
    so a reset never drops us into a negative slice.
    """
    if already < 0 or already > len(full):
        already = 0
    return full[already:], len(full)


async def task_log_append(task_id: str, chunk: str) -> None:
    """Append *chunk* (can span many lines) to the task's buffer.

    Trims the buffer to :data:`MAX_LOG_LINES` after insert.
    """
    if not chunk:
        return
    async with STATE.task_log_lock:
        buf = STATE.task_log_buffers.setdefault(task_id, [])
        for line in chunk.splitlines():
            buf.append(line)
        if len(buf) > MAX_LOG_LINES:
            del buf[: len(buf) - MAX_LOG_LINES]


async def task_log_tail(task_id: str, since: int) -> tuple[list[str], int]:
    """Return the lines appended after *since* and the current cursor.

    ``since`` is treated as a 0-based line index; values outside
    ``[0, len(buf)]`` are clamped to 0 so a disconnected client always
    resyncs to the full buffer on reconnect.
    """
    async with STATE.task_log_lock:
        buf = STATE.task_log_buffers.get(task_id, [])
        cursor = len(buf)
        if since < 0 or since > cursor:
            since = 0
        return list(buf[since:]), cursor


async def clear_local_task_log(task_id: str, delay: float = 30.0) -> None:
    """Schedule the task's log buffer to be dropped after *delay* seconds."""

    async def _delayed_drop() -> None:
        await asyncio.sleep(delay)
        async with STATE.task_log_lock:
            STATE.task_log_buffers.pop(task_id, None)

    try:
        asyncio.create_task(_delayed_drop())
    except RuntimeError:
        _log.debug("clear_local_task_log: no running loop; dropping immediately")
        STATE.task_log_buffers.pop(task_id, None)
