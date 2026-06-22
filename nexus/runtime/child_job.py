"""One app-wide kill-on-close Job Object so no child can outlive the node.

On Windows, every long-lived child the node spawns — the cloudflared relay
tunnel, sandboxed / multi-instance relays, service replicas — is assigned to a
single Job Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The node
holds the only handle for its whole lifetime, so when it exits for **any** reason
(graceful stop, Ctrl+C, console-window close, or a hard taskkill) the OS tears the
job down and kills every assigned child with it. No orphaned tunnels or relays.

Non-Windows is a no-op; those callers rely on their explicit ``stop()`` paths
(and ``atexit``) instead.
"""

from __future__ import annotations

import logging
import platform
import threading
from typing import Optional

_log = logging.getLogger("nexus.runtime.child_job")

# The process-wide job handle. Kept at module scope so it stays open (and the
# kill-on-close guarantee stays armed) for the app's whole lifetime.
_job: Optional[int] = None
_lock = threading.Lock()


def _ensure_job() -> Optional[int]:
    """Create the kill-on-close job once. Caller holds ``_lock``. Windows only."""
    global _job
    if _job is not None:
        return _job
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount",
        )]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _EXTENDED()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
        wintypes.HANDLE(job), _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        return None
    _job = job
    return _job


def bind(proc) -> bool:
    """Assign *proc* to the node's kill-on-close job. No-op off Windows.

    Best-effort: returns ``True`` if bound, ``False`` otherwise; never raises so
    a binding failure can't break the spawn (the caller's ``stop()`` + ``atexit``
    remain the fallback).
    """
    if proc is None or platform.system() != "Windows":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        with _lock:
            job = _ensure_job()
            if not job:
                return False
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return bool(k32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
            ))
    except Exception:
        _log.debug("could not bind child to kill-job", exc_info=True)
        return False


__all__ = ["bind"]
