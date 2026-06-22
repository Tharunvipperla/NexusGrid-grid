"""Cross-platform sandbox primitives for the native runtime.

the original implementation's native runtime relied on ``resource.setrlimit`` (Linux only) plus
``start_new_session`` for kill-on-parent-exit. layers two
stronger primitives on top:

* **Linux:** when ``bwrap`` (`bubblewrap`) is on PATH, native tasks run
  inside an unprivileged user-namespace sandbox. The host filesystem is
  read-only, ``/tmp`` is a tmpfs, ``/proc`` is fresh, and ``--die-with-parent``
  guarantees teardown if the worker dies.

* **Windows:** the spawned process is assigned to a Job Object configured
  with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. When the worker exits, the
  Job Object is destroyed and Windows kills every child immediately —
  closing the orphaned-subprocess gap that ``start_new_session`` covers
  on POSIX.

The setting ``native_sandbox_mode`` (``auto``/``strict``/``off``) gates
this behavior. ``strict`` makes the absence of a usable sandbox fatal so
operators can't accidentally run unsandboxed; ``auto`` falls back
gracefully and logs a warning.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from typing import Callable, Sequence


_log = logging.getLogger("nexus.runtime.native_sandbox")


class SandboxUnavailable(RuntimeError):
    """Raised in ``strict`` mode when no sandbox is available."""


def get_sandbox_mode() -> str:
    """Return the active sandbox mode.

    Read late so live settings changes via ``/local/settings`` take effect
    on the next dispatch without a restart.
    """
    from nexus.core import LOCAL_SETTINGS

    raw = str(LOCAL_SETTINGS.get("native_sandbox_mode", "auto") or "auto").lower()
    if raw in ("auto", "strict", "off"):
        return raw
    return "auto"


# ---------------------------------------------------------------------------
# Linux: bubblewrap wrapper
# ---------------------------------------------------------------------------

_BWRAP_BASE_ARGS: tuple[str, ...] = (
    "--die-with-parent",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-user",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
    "--tmpfs", "/var/tmp",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/lib", "/lib",
    "--ro-bind", "/lib64", "/lib64",
    "--ro-bind", "/etc", "/etc",
    "--ro-bind", "/bin", "/bin",
    "--ro-bind", "/sbin", "/sbin",
    "--clearenv",
)


def _bwrap_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("bwrap") is not None


def wrap_command_with_sandbox(
    cmd_parts: Sequence[str],
    *,
    workspace_dir: str,
    profile: str,
    extra_env_passthrough: Sequence[str] = (),
) -> tuple[list[str], str]:
    """Return ``(wrapped_cmd, log_message)`` for *cmd_parts*.

    On Linux with ``bwrap`` available (and not in ``off`` mode), returns
    ``cmd_parts`` prepended with the bubblewrap call. Workspace is bound
    read-write so the task can produce output. Any names in
    *extra_env_passthrough* are forwarded into the sandbox.

    On unsupported platforms or when bwrap is missing, the original
    *cmd_parts* are returned unchanged. ``strict`` mode raises
    :class:`SandboxUnavailable` instead.
    """
    cmd_list = list(cmd_parts)
    mode = get_sandbox_mode()

    if mode == "off":
        return cmd_list, "[SECURITY] Native sandbox disabled (mode=off)."

    if not _bwrap_available():
        if mode == "strict":
            raise SandboxUnavailable(
                "native_sandbox_mode=strict but bwrap is not installed. "
                "Install bubblewrap or set native_sandbox_mode=auto/off."
            )
        return (
            cmd_list,
            "[SECURITY] Note: bwrap not available — native task runs without "
            "filesystem sandbox.",
        )

    workspace_abs = os.path.abspath(workspace_dir)
    bwrap_args: list[str] = ["bwrap", *_BWRAP_BASE_ARGS]
    bwrap_args.extend(["--bind", workspace_abs, workspace_abs])
    bwrap_args.extend(["--chdir", workspace_abs])

    for name in extra_env_passthrough:
        value = os.environ.get(name)
        if value is not None:
            bwrap_args.extend(["--setenv", name, value])

    if profile != "maximum":
        # For non-maximum profiles, allow outbound network resolution by
        # not adding --unshare-net here; the caller may still wrap with
        # `unshare --net` separately if network access is forbidden.
        pass
    else:
        bwrap_args.append("--unshare-net")

    bwrap_args.append("--")
    bwrap_args.extend(cmd_list)
    return (
        bwrap_args,
        f"[SECURITY] Native task wrapped with bwrap (profile={profile}).",
    )


# ---------------------------------------------------------------------------
# Linux: rlimit preexec
# ---------------------------------------------------------------------------

def make_resource_limits(safe_ram_mb: int) -> Callable[[], None] | None:
    """Return a ``preexec_fn`` that applies ``setrlimit`` to the child.

    Returns ``None`` on Windows. Called from
    :func:`asyncio.create_subprocess_exec`.
    """
    if sys.platform == "win32":
        return None
    safe_ram_bytes = max(1, int(safe_ram_mb)) * 1024 * 1024

    def _apply() -> None:
        try:
            import resource

            resource.setrlimit(
                resource.RLIMIT_AS, (safe_ram_bytes, safe_ram_bytes)
            )
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (1024 * 1024 * 1024, 1024 * 1024 * 1024),
            )
        except Exception:
            pass

    return _apply


# ---------------------------------------------------------------------------
# Windows: Job Objects (kill-on-job-close)
# ---------------------------------------------------------------------------

_job_handle_cache: dict[int, object] = {}


def assign_to_job_object(pid: int, ram_limit_mb: int | None = None) -> bool:
    """Place *pid* in a freshly-created Job Object that dies with the parent.

    Returns True on success. Silently no-ops on non-Windows platforms or
    when ``pywin32`` is unavailable. The Job Object handle is cached so
    Python keeps a reference for the lifetime of the worker process; when
    the worker dies the OS frees the handle and kills every assigned
    process via ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.
    """
    if sys.platform != "win32":
        return False
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]
    except ImportError:
        _log.debug("pywin32 not available — Windows Job Object skipped.")
        return False

    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if ram_limit_mb and ram_limit_mb > 0:
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            )
            info["ProcessMemoryLimit"] = int(ram_limit_mb) * 1024 * 1024
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info
        )
        proc_handle = win32api.OpenProcess(
            win32con.PROCESS_ALL_ACCESS, False, pid
        )
        win32job.AssignProcessToJobObject(job, proc_handle)
        _job_handle_cache[pid] = job
        return True
    except Exception as exc:
        _log.warning("Failed to assign pid %d to Job Object: %s", pid, exc)
        return False


def release_job_object(pid: int) -> None:
    """Drop the cached Job Object handle for *pid* (if any)."""
    _job_handle_cache.pop(pid, None)


__all__ = [
    "SandboxUnavailable",
    "assign_to_job_object",
    "get_sandbox_mode",
    "make_resource_limits",
    "release_job_object",
    "wrap_command_with_sandbox",
]
