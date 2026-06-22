"""Cross-platform idle-input detection.

When ``LOCAL_SETTINGS["idle_auto_accept"]`` is enabled, the node only
participates in dispatch while the user has been idle for at least
``idle_threshold_sec`` seconds. The user's explicit ``node_online``
preference is *not* overwritten — readers consult
:func:`is_node_online_effective` instead, which combines the preference
with the live idle signal.

Scope: the gate only blocks *new* task pulls. Already-running tasks —
including long-lived service-runtime containers — keep
running regardless of idle state. Stopping a service mid-flight on
keystroke would be hostile; explicit preemption goes through
``preempt_running_task`` instead.

Platform support:

* **Windows** — ``user32.GetLastInputInfo`` returns ms since last input.
* **Linux/X11** — invoke ``xprintidle`` if available. Wayland and headless
  environments fall back to "no signal" (always-on by user pref).
* **macOS** — ``ioreg -c IOHIDSystem`` exposes ``HIDIdleTime`` in ns.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from typing import Optional

from nexus.core import LOCAL_SETTINGS

_log = logging.getLogger("nexus.runtime.idle_detect")


def seconds_since_input() -> Optional[float]:
    """Return seconds since last user input, or ``None`` if undetectable."""
    if sys.platform == "win32":
        return _windows_idle()
    if sys.platform == "darwin":
        return _macos_idle()
    if sys.platform.startswith("linux"):
        return _linux_idle()
    return None


def is_idle(threshold_sec: Optional[int] = None) -> bool:
    """Return True iff input has been idle for at least *threshold_sec*."""
    threshold = (
        threshold_sec
        if threshold_sec is not None
        else int(LOCAL_SETTINGS.get("idle_threshold_sec", 300) or 300)
    )
    secs = seconds_since_input()
    if secs is None:
        return True  # no signal -> "idle" so feature still works on headless boxes
    return secs >= threshold


def is_node_online_effective() -> bool:
    """Combine the user's ``node_online`` pref with the idle gate.

    Used for *compute dispatch* decisions — should we pull tasks, advertise
    capacity for compute work, etc. NOT used for peer-link lifecycle, which
    has its own gate (:func:`is_peer_link_allowed`).
    """
    if not bool(LOCAL_SETTINGS.get("node_online", True)):
        return False
    if not bool(LOCAL_SETTINGS.get("idle_auto_accept", False)):
        return True
    return is_idle()


def is_peer_link_allowed() -> bool:
    """Should we keep peer websockets open?

    Decoupled from ``idle_auto_accept`` so storage / view-grant / control
    frames flow over the existing WS even while the user is actively using
    the machine. Only an explicit ``node_online=False`` shuts links down —
    matching the user-stated invariant that *any trusted peer can deposit
    whenever both nodes are reachable*.
    """
    return bool(LOCAL_SETTINGS.get("node_online", True))


# ---------------------------------------------------------------------------
# Platform implementations
# ---------------------------------------------------------------------------

def _windows_idle() -> Optional[float]:
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # GetTickCount wraps every ~49 days — close enough for our timescale.
        elapsed_ms = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        return max(0.0, elapsed_ms / 1000.0)
    except Exception as exc:
        _log.debug("Windows idle probe failed: %s", exc)
        return None


def _linux_idle() -> Optional[float]:
    if not shutil.which("xprintidle"):
        return None
    try:
        out = subprocess.run(
            ["xprintidle"], capture_output=True, text=True, timeout=1.0
        )
        if out.returncode != 0:
            return None
        return max(0.0, int(out.stdout.strip()) / 1000.0)
    except Exception as exc:
        _log.debug("Linux idle probe failed: %s", exc)
        return None


def _macos_idle() -> Optional[float]:
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            if "HIDIdleTime" in line:
                # `... "HIDIdleTime" = 12345678901`  (nanoseconds)
                _, _, raw = line.partition("=")
                return max(0.0, int(raw.strip()) / 1_000_000_000.0)
    except Exception as exc:
        _log.debug("macOS idle probe failed: %s", exc)
    return None


__all__ = [
    "is_idle",
    "is_node_online_effective",
    "is_peer_link_allowed",
    "seconds_since_input",
]
