"""Cross-platform recursive process kill, with a second-pass re-scan.

Extracted from node_modified.py (lines 1318-1368, 2076-2101).

``kill_process_tree`` is the one low-level primitive used by every runtime
backend (native, docker-shell-wrap) to make sure a task that spawns children
cannot leave orphaned processes behind. The flow is:

1. SIGTERM every descendant discovered on the first walk.
2. Sleep briefly to let well-behaved children exit.
3. Re-query the tree — a process may have forked grandchildren between
   step 1 and now.
4. SIGKILL anything still alive.

Uses ``psutil`` because the stdlib does not give us a portable way to
enumerate children of a ``subprocess.Popen``.
"""

from __future__ import annotations

import asyncio
import logging

import psutil

from nexus.core import STATE

_log = logging.getLogger("nexus.runtime.process_tree")


async def kill_process_tree(proc) -> None:
    """Terminate *proc* and every descendant. Best-effort; never raises."""
    if proc is None:
        return
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            proc.terminate()
        except (ProcessLookupError, psutil.NoSuchProcess, OSError):
            pass
        await asyncio.sleep(3)
        try:
            leftover = psutil.Process(proc.pid).children(recursive=True)
        except (psutil.NoSuchProcess, ProcessLookupError):
            leftover = []
        for child in list(children) + leftover:
            try:
                if child.is_running():
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                pass
        try:
            proc.kill()
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass
    except (psutil.NoSuchProcess, ProcessLookupError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass


async def kill_task_native_proc(task_id: str) -> bool:
    """Kill the native subprocess registered for *task_id*, if any."""
    async with STATE.running_container_lock:
        proc = STATE.running_task_procs.get(task_id)
    if proc is None:
        return False
    try:
        await kill_process_tree(proc)
    except Exception:
        _log.debug("kill_task_native_proc failed", exc_info=True)
    return True


def snapshot_proc_children(proc) -> list[dict]:
    """Return UI-safe dicts describing the descendants of a live proc."""
    if proc is None or getattr(proc, "returncode", None) is not None:
        return []
    try:
        parent = psutil.Process(proc.pid)
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
        return []
    out: list[dict] = []
    for c in descendants:
        try:
            with c.oneshot():
                name = c.name()
                try:
                    cmdline = " ".join(c.cmdline())[:200]
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    cmdline = ""
                try:
                    rss_mb = round(c.memory_info().rss / (1024 * 1024), 1)
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    rss_mb = 0.0
                out.append(
                    {"pid": c.pid, "name": name, "cmdline": cmdline, "rss_mb": rss_mb}
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


__all__ = ["kill_process_tree", "kill_task_native_proc", "snapshot_proc_children"]
