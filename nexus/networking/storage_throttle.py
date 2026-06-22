"""Foreign-storage transfer throttle.

The host must serve foreign-storage chunks WITHOUT degrading running
service / batch tasks. This module is the central knob:

* Two profiles — **busy** (worker has at least one active task lease)
  and **idle** (no active leases). The bucket auto-selects on each
  ``acquire`` call.
* Configurable via ``LOCAL_SETTINGS`` (sliders in the dashboard
  Settings panel exposed in 5b.4): ``storage_bw_busy_mbps``,
  ``storage_bw_idle_mbps``, ``storage_max_total_gb``,
  ``storage_max_per_depositor_gb``.
* Pause-when-overloaded: before each chunk the throttle checks RAM
  pressure and active-task count and yields up to a second when the
  worker is under load.

Disk I/O is kept off the event loop in :mod:`storage_pump`; this
module just throttles bandwidth and pause-checks.
"""

from __future__ import annotations

import asyncio
import logging
import time

import psutil

from nexus.core import LOCAL_SETTINGS, STATE

_log = logging.getLogger("nexus.networking.storage_throttle")

_DEFAULT_BUSY_MBPS = 10
_DEFAULT_IDLE_MBPS = 100
_DEFAULT_MAX_TOTAL_GB = 100
_DEFAULT_MAX_PER_DEPOSITOR_GB = 10

_PAUSE_RAM_PCT = 95.0  # pause when used RAM > 95% (was 80%; on a busy desktop 80% triggers per-chunk and tanks throughput)
_PAUSE_SLEEP_SEC = 0.2


class StorageThrottle:
    """Token-bucket throttle with busy / idle profiles.

    Only one instance per process; obtain via :func:`get_storage_throttle`.
    """

    __slots__ = ("_busy_tokens", "_idle_tokens", "_busy_burst", "_idle_burst", "_last")

    def __init__(self) -> None:
        self._busy_tokens = 0.0
        self._idle_tokens = 0.0
        self._busy_burst = 0.0
        self._idle_burst = 0.0
        self._last = time.monotonic()

    @staticmethod
    def busy_mbps() -> int:
        return max(
            1, int(LOCAL_SETTINGS.get("storage_bw_busy_mbps", _DEFAULT_BUSY_MBPS) or 0)
        )

    @staticmethod
    def idle_mbps() -> int:
        return max(
            1, int(LOCAL_SETTINGS.get("storage_bw_idle_mbps", _DEFAULT_IDLE_MBPS) or 0)
        )

    @staticmethod
    def is_busy() -> bool:
        # Worker has an active task lease => busy. Master-only nodes
        # always look idle (they don't pull tasks themselves).
        return bool(getattr(STATE, "running_task_containers", None)) and len(
            STATE.running_task_containers
        ) > 0 or bool(getattr(STATE, "running_task_procs", None)) and len(
            STATE.running_task_procs
        ) > 0

    async def _maybe_pause_for_overload(self) -> None:
        """Yield up to ``_PAUSE_SLEEP_SEC`` if RAM is tight."""
        try:
            used_pct = psutil.virtual_memory().percent
        except Exception:
            used_pct = 0.0
        if used_pct < _PAUSE_RAM_PCT:
            return
        _log.debug(
            "storage throttle paused: RAM at %.1f%%", used_pct
        )
        await asyncio.sleep(_PAUSE_SLEEP_SEC)

    async def acquire(self, n: int) -> None:
        """Block until *n* bytes are available under the active profile."""
        if n <= 0:
            return
        await self._maybe_pause_for_overload()

        rate_bps = (
            self.busy_mbps() if self.is_busy() else self.idle_mbps()
        ) * 1024 * 1024
        burst = max(64 * 1024, rate_bps)

        while True:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            # Only one bucket is active per call but we top up both so a
            # busy→idle flip doesn't suddenly stall.
            self._busy_tokens = min(
                burst, self._busy_tokens + elapsed * self.busy_mbps() * 1024 * 1024
            )
            self._idle_tokens = min(
                burst, self._idle_tokens + elapsed * self.idle_mbps() * 1024 * 1024
            )
            tokens = self._busy_tokens if self.is_busy() else self._idle_tokens
            if tokens >= n:
                if self.is_busy():
                    self._busy_tokens -= n
                else:
                    self._idle_tokens -= n
                return
            deficit = n - tokens
            await asyncio.sleep(max(0.005, deficit / max(1, rate_bps)))


_singleton: StorageThrottle | None = None


def get_storage_throttle() -> StorageThrottle:
    """Return the process-wide throttle, creating it on first call."""
    global _singleton
    if _singleton is None:
        _singleton = StorageThrottle()
    return _singleton


def install_storage_throttle() -> StorageThrottle:
    """Hook the throttle onto STATE so :mod:`storage_pump` finds it."""
    throttle = get_storage_throttle()
    setattr(STATE, "foreign_storage_throttle", throttle)
    return throttle


__all__ = [
    "StorageThrottle",
    "get_storage_throttle",
    "install_storage_throttle",
]
