"""Pause/resume the local relay with a delayed-kill grace window.

A user who wants to take a break (testing, going AFK, momentarily blocking
inbound traffic) can pause the local relay without losing the tunnel URL —
provided they resume within ``PAUSE_GRACE_SEC``. The local relay's uvicorn
thread stops immediately (so inbound connections fail at the relay), but the
cloudflared subprocess stays alive in case of a quick resume. If the grace
window expires, cloudflared is killed too — on next resume the cached
binary is reused (no re-download) and a fresh tunnel URL is acquired +
broadcast via 's self-heal.

State is in-memory only; an app restart while paused effectively cancels
the pause (next boot acts on the persisted ``local_relay_enabled`` /
``relay_tunnel_enabled`` settings).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import local_relay, relay_selfheal, relay_tunnel

_log = logging.getLogger("nexus.runtime.relay_pause")


PAUSE_GRACE_SEC = 120

_state: dict = {
    "is_paused": False,
    "paused_at": 0.0,
    "kill_task": None,  # Optional[asyncio.Task]
    "cloudflared_killed": False,  # True after grace expires
}
_lock = asyncio.Lock()


def status() -> dict:
    """Snapshot of pause state."""
    now = time.time()
    paused_at = float(_state.get("paused_at") or 0.0)
    grace_remaining = 0
    if _state.get("is_paused") and paused_at:
        grace_remaining = max(0, int(PAUSE_GRACE_SEC - (now - paused_at)))
    return {
        "is_paused": bool(_state.get("is_paused")),
        "paused_at": paused_at,
        "grace_total_sec": PAUSE_GRACE_SEC,
        "grace_remaining_sec": grace_remaining,
        "cloudflared_killed": bool(_state.get("cloudflared_killed")),
    }


async def _delayed_cloudflared_kill() -> None:
    """Sleep the grace window; if still paused, terminate cloudflared.

    On wake, only acts when ``is_paused`` is still True — a resume call
    cancels this task so the kill never runs.
    """
    try:
        await asyncio.sleep(PAUSE_GRACE_SEC)
    except asyncio.CancelledError:
        return
    async with _lock:
        if not _state.get("is_paused"):
            return
        if relay_tunnel.is_running():
            try:
                await asyncio.to_thread(relay_tunnel.stop)
            except Exception:
                _log.warning(
                    "delayed cloudflared kill failed", exc_info=True
                )
            _state["cloudflared_killed"] = True
            _log.info(
                "[RELAY-PAUSE] grace window expired; cloudflared killed"
            )
        _state["kill_task"] = None


async def pause() -> dict:
    """Pause the local relay. Cloudflared stays alive for the grace window."""
    async with _lock:
        if _state.get("is_paused"):
            return {"status": "already_paused", **status()}
        if not local_relay.is_running():
            return {"status": "not_running", **status()}

        # Stop the relay's uvicorn thread — inbound traffic fails fast.
        try:
            local_relay.stop()
        except Exception:
            _log.warning("local_relay.stop during pause failed", exc_info=True)
        # Note: we deliberately do NOT clear local_relay_enabled here — the
        # intent to run the relay survives the pause. relay_admin's
        # /stop endpoint is the only thing that flips that off.

        _state["is_paused"] = True
        _state["paused_at"] = time.time()
        _state["cloudflared_killed"] = False
        prev = _state.get("kill_task")
        if prev is not None and not prev.done():
            prev.cancel()
        _state["kill_task"] = asyncio.create_task(
            _delayed_cloudflared_kill(),
            name="nexus.runtime.relay_pause.delayed_kill",
        )
    _log.info(
        "[RELAY-PAUSE] paused; cloudflared keep-alive for %ds", PAUSE_GRACE_SEC
    )
    return {"status": "paused", **status()}


async def resume() -> dict:
    """Resume the local relay. Fast path if cloudflared is still alive."""
    async with _lock:
        if not _state.get("is_paused"):
            return {"status": "not_paused", **status()}

        # Cancel the pending kill task first — no race with the grace
        # timer firing mid-resume.
        kt = _state.get("kill_task")
        if kt is not None and not kt.done():
            kt.cancel()
        _state["kill_task"] = None

        cloudflared_killed = bool(_state.get("cloudflared_killed"))
        _state["is_paused"] = False
        _state["paused_at"] = 0.0
        _state["cloudflared_killed"] = False

    # Restart local relay (always — the relay thread was stopped on pause).
    grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    port = int(
        LOCAL_SETTINGS.get("local_relay_port") or local_relay.DEFAULT_RELAY_PORT
    )
    try:
        local_relay.start(port, grid_key)
    except OSError as exc:
        return {
            "status": "resume_failed",
            "reason": f"port {port} unavailable: {exc}",
            **status(),
        }

    # Cloudflared: if it survived the grace, the tunnel URL is unchanged
    # and traffic flows immediately. If it was killed, restart it (cached
    # Binary so no re-download; new URL → self-heal broadcasts
    # the new URL to every group binding).
    url_changed = False
    if cloudflared_killed and LOCAL_SETTINGS.get("relay_tunnel_enabled"):
        try:
            await relay_selfheal.start_tunnel_and_reconcile()
            url_changed = True
        except Exception:
            _log.warning(
                "tunnel restart after pause-grace expiry failed", exc_info=True
            )

    _log.info(
        "[RELAY-PAUSE] resumed (url_changed=%s, cloudflared_was_killed=%s)",
        url_changed, cloudflared_killed,
    )
    return {
        "status": "resumed",
        "url_changed": url_changed,
        "cloudflared_was_killed": cloudflared_killed,
        **status(),
    }


__all__ = ["PAUSE_GRACE_SEC", "pause", "resume", "status"]
