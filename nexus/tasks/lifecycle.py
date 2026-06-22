"""Task status transitions, timeline events, retry, preemption.

Extracted from Phase-1/node_modified.py:

* ``add_task_timeline_event`` — lines 1679-1686
* ``set_task_status`` — lines 1689-1720
* ``try_schedule_retry`` — lines 1763-1797
* ``mark_task_interrupted`` / ``mark_task_preempted`` / ``is_task_*``
  — lines 2212-2265

This module owns *any* transition on ``TaskRecord.status`` and any
append-only write to the task timeline. It is the single place where
``ALLOWED_TRANSITIONS`` is enforced — callers never touch ``task.status``
directly.

The module is deliberately synchronous for in-memory fields; the
``mark_task_*`` helpers are async only because they take
``STATE.running_container_lock``.
"""

from __future__ import annotations

import logging

from nexus.core import (
    ALLOWED_TRANSITIONS,
    LOCAL_SETTINGS,
    STATE,
    TASK_STATES,
    TERMINAL_STATES,
)
from nexus.core import events
from nexus.storage import TaskRecord
from nexus.tasks.metadata import (
    get_retry_policy,
    parse_task_env,
    set_retry_policy,
    write_task_env,
)
from nexus.telemetry import incr_metric
from nexus.utils import now_epoch, timestamp

_log = logging.getLogger("nexus.tasks.lifecycle")


# ---------------------------------------------------------------------------
# Timeline + status transitions
# ---------------------------------------------------------------------------

def add_task_timeline_event(
    task: TaskRecord, event: str, details: str = ""
) -> None:
    """Append a timeline entry (capped at 250 per task)."""
    env = parse_task_env(task)
    timeline = env.get("NEXUS_META_TIMELINE", [])
    if not isinstance(timeline, list):
        timeline = []
    timeline.append({"ts": now_epoch(), "event": event, "details": details})
    env["NEXUS_META_TIMELINE"] = timeline[-250:]
    write_task_env(task, env)


def set_task_status(
    task: TaskRecord,
    new_status: str,
    reason: str = "",
    *,
    force: bool = False,
) -> bool:
    """Attempt to move *task* to *new_status*. Return ``True`` on success.

    * Refuses transitions not listed in
      :data:`nexus.core.constants.ALLOWED_TRANSITIONS` unless ``force=True``.
    * Appends a ``[STATE]`` line to ``task.logs``.
    * Appends a timeline event via :func:`add_task_timeline_event`.
    * Sets ``NEXUS_META_QUEUED_AT`` when entering the ``queued`` state so
      queue-timeout logic can measure wait time.
    * On terminal transition, clears any ``consent_strikes`` for the task
      (shared state owned by :mod:`nexus.networking.peer` once wired).
    * Publishes ``task.status_changed`` on the event bus so telemetry / UI
      layers can react without importing this module.
    """
    old_status = str(task.status or "").lower()
    target_status = str(new_status or "").lower()
    if target_status not in TASK_STATES:
        return False
    if not force and target_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        return False
    if old_status == target_status:
        return True
    task.status = target_status
    transition_msg = (
        f"[{timestamp()}] [STATE] {old_status.upper()} -> {target_status.upper()}."
    )
    if reason:
        transition_msg += f" {reason}"
    task.logs = (task.logs or "") + transition_msg + "\n"
    add_task_timeline_event(
        task, "status_transition", f"{old_status}->{target_status}: {reason}"
    )
    if target_status == "queued":
        env = parse_task_env(task)
        env["NEXUS_META_QUEUED_AT"] = now_epoch()
        write_task_env(task, env)
    # Stamp run start/end so the UI can show how long a task took. Started is
    # set once, on first entry to an executing state; completed on terminal.
    if target_status in ("processing", "serving"):
        env = parse_task_env(task)
        if not env.get("NEXUS_META_STARTED_AT"):
            env["NEXUS_META_STARTED_AT"] = now_epoch()
            write_task_env(task, env)
    if target_status in TERMINAL_STATES:
        env = parse_task_env(task)
        env["NEXUS_META_COMPLETED_AT"] = now_epoch()
        write_task_env(task, env)
        stale_keys = [k for k in STATE.consent_strikes if k[0] == task.id]
        for k in stale_keys:
            del STATE.consent_strikes[k]
    events.publish(
        "task.status_changed",
        {
            "task_id": task.id,
            "old_status": old_status,
            "new_status": target_status,
            "reason": reason,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Retry scheduling
# ---------------------------------------------------------------------------

def try_schedule_retry(
    task: TaskRecord,
    reason: str,
    failed_worker: str | None = None,
) -> bool:
    """Schedule a retry if the task's policy permits. Return ``True`` on success.

    Applies exponential backoff (``base * 2**retry_count``), tracks failed
    workers so the scheduler can avoid the same broken host, and puts the
    failed worker into a cooldown window via
    :data:`STATE.worker_cooldown_until`.
    """
    retry_max, retry_count, backoff_base = get_retry_policy(task)
    if retry_count >= retry_max:
        return False
    next_retry_at = now_epoch() + (backoff_base * (2 ** max(0, retry_count)))
    set_retry_policy(
        task,
        retry_max=retry_max,
        retry_count=retry_count + 1,
        next_retry_at=next_retry_at,
        backoff_base=backoff_base,
    )
    if failed_worker:
        env = parse_task_env(task)
        failed_list = env.get("NEXUS_META_FAILED_WORKERS", [])
        if not isinstance(failed_list, list):
            failed_list = []
        if failed_worker not in failed_list:
            failed_list.append(failed_worker)
        env["NEXUS_META_FAILED_WORKERS"] = failed_list
        write_task_env(task, env)
    # force=True: guarantees the retry is recorded even if the transition
    # matrix would otherwise reject it (e.g. from `disrupted`).
    set_task_status(
        task,
        "retrying",
        f"{reason} Retry {retry_count + 1}/{retry_max}.",
        force=True,
    )
    if failed_worker:
        STATE.worker_cooldown_until[failed_worker] = now_epoch() + float(
            LOCAL_SETTINGS["worker_cooldown_sec"]
        )
    _log.info(
        "Task %s: retry %d/%d scheduled (failed_worker=%s)",
        task.id,
        retry_count + 1,
        retry_max,
        failed_worker or "none",
    )
    incr_metric("task_retries")
    return True


# ---------------------------------------------------------------------------
# Preemption / interruption markers (runtime-facing)
# ---------------------------------------------------------------------------

async def mark_task_interrupted(task_id: str) -> bool:
    """Mark *task_id* as interrupted; runtime loops check this to abort work.

    Returns ``True`` if the flag was newly set, ``False`` if already set.
    """
    async with STATE.running_container_lock:
        if task_id in STATE.interrupted_task_ids:
            return False
        STATE.interrupted_task_ids.add(task_id)
    return True


async def mark_task_preempted(task_id: str) -> bool:
    """Mark *task_id* as preempted (a higher-priority task bumped it)."""
    async with STATE.running_container_lock:
        if task_id in STATE.preempted_task_ids:
            return False
        STATE.preempted_task_ids.add(task_id)
    return True


async def is_task_interrupted(task_id: str) -> bool:
    async with STATE.running_container_lock:
        return task_id in STATE.interrupted_task_ids


async def is_task_preempted(task_id: str) -> bool:
    async with STATE.running_container_lock:
        return task_id in STATE.preempted_task_ids


__all__ = [
    "add_task_timeline_event",
    "set_task_status",
    "try_schedule_retry",
    "mark_task_interrupted",
    "mark_task_preempted",
    "is_task_interrupted",
    "is_task_preempted",
]
