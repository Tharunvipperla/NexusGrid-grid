"""Per-node local worker bookkeeping.

Extracted from node_modified.py:

* ``LOCAL_WORKER_STATE`` dict — lines 473-479
* ``get_local_worker_snapshot`` — lines 2007-2034
* ``mark_local_task_running`` / ``update_local_task_stage`` /
  ``update_local_task_children`` — lines 2037-2073
* ``clear_local_task`` / ``mark_local_task_result`` — lines 2158-2185
* ``register_running_container`` / ``register_running_proc`` and their
  ``unregister_*`` twins — lines 2188-2209

These helpers track what the local node (in its worker role) is currently
doing. The UI calls :func:`get_local_worker_snapshot` each time it polls;
the runtime calls the ``mark_*`` / ``update_*`` helpers as a task
progresses through its stages.

``get_connected_master_peers`` is injected via
:func:`set_connected_masters_hook` so this module does not import
``nexus.networking`` (which sits above runtime in the layering).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from nexus.core import LOCAL_SETTINGS, STATE


# ---------------------------------------------------------------------------
# Local worker state (what *this* node, as a worker, is currently doing)
# ---------------------------------------------------------------------------

_LOCAL_WORKER_STATE: dict[str, Any] = {
    "active_tasks": [],
    "last_update": 0.0,
    "last_result_status": None,
    "last_result_at": 0.0,
    "last_result_master": None,
}

_ConnectedMastersHook = Callable[[], Awaitable[list[str]]]
_connected_masters_hook: _ConnectedMastersHook | None = None


def set_connected_masters_hook(hook: _ConnectedMastersHook | None) -> None:
    """Register a coroutine that returns the list of currently-connected masters.

    Wired from :mod:`nexus.networking` at app startup so runtime does not
    depend on networking.
    """
    global _connected_masters_hook
    _connected_masters_hook = hook


async def _connected_masters() -> list[str]:
    if _connected_masters_hook is None:
        return []
    try:
        return list(await _connected_masters_hook())
    except Exception:
        return []


async def get_local_worker_snapshot() -> dict:
    """Return the UI-facing snapshot of this node's worker activity."""
    async with STATE.worker_state_lock:
        active_tasks = [dict(task) for task in _LOCAL_WORKER_STATE["active_tasks"]]
        last_update = _LOCAL_WORKER_STATE["last_update"]
        last_result_status = _LOCAL_WORKER_STATE["last_result_status"]
        last_result_at = _LOCAL_WORKER_STATE["last_result_at"]
        last_result_master = _LOCAL_WORKER_STATE["last_result_master"]

    serving_masters = sorted({task["master_ip"] for task in active_tasks})
    connected_masters = await _connected_masters()
    from nexus.runtime.idle_detect import is_node_online_effective

    node_online = is_node_online_effective()
    return {
        "status": (
            "offline" if not node_online else ("busy" if active_tasks else "idle")
        ),
        "node_online": node_online,
        "serving_master": serving_masters[0] if serving_masters else None,
        "serving_masters": serving_masters,
        "connected_masters": connected_masters,
        "connected_master_count": len(connected_masters),
        "active_task": active_tasks[0]["task_id"] if active_tasks else None,
        "active_tasks": active_tasks,
        "active_task_count": len(active_tasks),
        "last_update": last_update,
        "last_result_status": last_result_status,
        "last_result_at": last_result_at,
        "last_result_master": last_result_master,
    }


async def mark_local_task_running(master_ip: str, task_id: str) -> None:
    async with STATE.worker_state_lock:
        active_tasks = _LOCAL_WORKER_STATE["active_tasks"]
        if not any(
            t["task_id"] == task_id and t["master_ip"] == master_ip
            for t in active_tasks
        ):
            active_tasks.append(
                {
                    "task_id": task_id,
                    "master_ip": master_ip,
                    "started_at": time.time(),
                    "stage": "queued",
                    "stage_since": time.time(),
                    "children": [],
                }
            )
        _LOCAL_WORKER_STATE["last_update"] = time.time()


async def update_local_task_stage(task_id: str, stage: str) -> None:
    async with STATE.worker_state_lock:
        for t in _LOCAL_WORKER_STATE["active_tasks"]:
            if t["task_id"] == task_id:
                t["stage"] = stage
                t["stage_since"] = time.time()
                break
        _LOCAL_WORKER_STATE["last_update"] = time.time()


async def update_local_task_children(task_id: str, children: list) -> None:
    async with STATE.worker_state_lock:
        for t in _LOCAL_WORKER_STATE["active_tasks"]:
            if t["task_id"] == task_id:
                t["children"] = children
                break
        _LOCAL_WORKER_STATE["last_update"] = time.time()


async def clear_local_task(master_ip: str, task_id: str) -> None:
    """Remove *task_id* from the active list. Log buffer drops after 30s."""
    async with STATE.worker_state_lock:
        _LOCAL_WORKER_STATE["active_tasks"] = [
            t for t in _LOCAL_WORKER_STATE["active_tasks"]
            if not (t["task_id"] == task_id and t["master_ip"] == master_ip)
        ]
        _LOCAL_WORKER_STATE["last_update"] = time.time()

    async def _delayed_drop():
        await asyncio.sleep(30)
        async with STATE.task_log_lock:
            STATE.task_log_buffers.pop(task_id, None)

    try:
        asyncio.create_task(_delayed_drop())
    except RuntimeError:
        pass


async def mark_local_task_result(master_ip: str, result_status: str) -> None:
    async with STATE.worker_state_lock:
        (
            _LOCAL_WORKER_STATE["last_result_status"],
            _LOCAL_WORKER_STATE["last_result_at"],
            _LOCAL_WORKER_STATE["last_result_master"],
            _LOCAL_WORKER_STATE["last_update"],
        ) = (result_status, time.time(), master_ip, time.time())


# ---------------------------------------------------------------------------
# Container / native-proc registration (used by runtime backends)
# ---------------------------------------------------------------------------

async def register_running_container(task_id: str, container) -> None:
    async with STATE.running_container_lock:
        STATE.running_task_containers[task_id] = container
        STATE.interrupted_task_ids.discard(task_id)
        STATE.preempted_task_ids.discard(task_id)


async def unregister_running_container(task_id: str) -> None:
    async with STATE.running_container_lock:
        STATE.running_task_containers.pop(task_id, None)
        STATE.interrupted_task_ids.discard(task_id)
        STATE.preempted_task_ids.discard(task_id)
        cleanup_dir = STATE.running_task_cleanup_dirs.pop(task_id, None)
    if cleanup_dir:
        import shutil
        await asyncio.to_thread(shutil.rmtree, cleanup_dir, ignore_errors=True)


async def register_running_proc(task_id: str, proc) -> None:
    async with STATE.running_container_lock:
        STATE.running_task_procs[task_id] = proc


async def unregister_running_proc(task_id: str) -> None:
    async with STATE.running_container_lock:
        STATE.running_task_procs.pop(task_id, None)


# ---------------------------------------------------------------------------
# Interrupt / preempt: combine the kill side with the flag side
# ---------------------------------------------------------------------------

async def interrupt_running_task(task_id: str) -> bool:
    """Stop container or kill native proc, and flag the task as interrupted.

    Returns ``True`` if either a container or a native proc was actually
    acted upon. The corresponding status transition is done by the caller.
    """
    from nexus.runtime.process_tree import kill_process_tree  # local: avoid cycle

    container = None
    native_proc = None
    async with STATE.running_container_lock:
        STATE.interrupted_task_ids.add(task_id)
        container = STATE.running_task_containers.get(task_id)
        native_proc = STATE.running_task_procs.get(task_id)
    handled = False
    if container:
        try:
            await asyncio.to_thread(container.stop, timeout=1)
        except Exception:
            pass
        handled = True
    if native_proc is not None:
        try:
            await kill_process_tree(native_proc)
        except Exception:
            pass
        handled = True
    return handled


async def preempt_running_task(task_id: str) -> bool:
    """Same as :func:`interrupt_running_task` but flags as preempted."""
    from nexus.runtime.process_tree import kill_process_tree  # local: avoid cycle

    container = None
    native_proc = None
    async with STATE.running_container_lock:
        STATE.preempted_task_ids.add(task_id)
        container = STATE.running_task_containers.get(task_id)
        native_proc = STATE.running_task_procs.get(task_id)
    handled = False
    if container:
        try:
            await asyncio.to_thread(container.stop, timeout=1)
        except Exception:
            pass
        handled = True
    if native_proc is not None:
        try:
            await kill_process_tree(native_proc)
        except Exception:
            pass
        handled = True
    return handled


__all__ = [
    "set_connected_masters_hook",
    "get_local_worker_snapshot",
    "mark_local_task_running",
    "update_local_task_stage",
    "update_local_task_children",
    "clear_local_task",
    "mark_local_task_result",
    "register_running_container",
    "unregister_running_container",
    "register_running_proc",
    "unregister_running_proc",
    "interrupt_running_task",
    "preempt_running_task",
]
