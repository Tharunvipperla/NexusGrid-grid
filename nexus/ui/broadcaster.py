"""Live UI WebSocket broadcaster.

Extracted from node_modified.py (lines 8460-8490).

The broadcaster keeps a list of connected UI-side WebSockets and fans out
state deltas. Non-UI packages publish ``"state_changed"`` etc. on the
:mod:`nexus.core.events` bus; this module subscribes once at startup so
nobody else needs to import us.

The admission gate (local/private-network only + token check) happens in
the WebSocket endpoint itself; this module assumes the sockets it holds
are already authenticated.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.core import events

_log = logging.getLogger("nexus.ui.broadcaster")

_ui_ws_connections: list[Any] = []


def register_ws(ws) -> None:
    """Record an authenticated UI WebSocket."""
    if ws not in _ui_ws_connections:
        _ui_ws_connections.append(ws)


def unregister_ws(ws) -> None:
    """Remove a previously-registered UI WebSocket."""
    if ws in _ui_ws_connections:
        _ui_ws_connections.remove(ws)


async def broadcast_ui_update(payload: dict) -> None:
    """Fan-out *payload* to every connected UI WebSocket.

    Sockets that fail to accept the message are dropped from the list.
    A ``state_changed`` event also invalidates the ``/local/network``
    cache so the next poll rebuilds fresh.
    """
    if payload.get("type") == "state_changed":
        # Local import: avoid the ui → api dependency at module load.
        from nexus.api.network_cache import get_cache

        get_cache()["ts"] = 0.0

    for ws in list(_ui_ws_connections):
        try:
            await ws.send_json(payload)
        except Exception:
            if ws in _ui_ws_connections:
                _ui_ws_connections.remove(ws)


def _on_bus_event(event_name: str):
    async def handler(data: dict) -> None:
        await broadcast_ui_update({"type": event_name, **data})
    return handler


_INSTALLED = False


def install_event_bridge() -> None:
    """Subscribe the broadcaster to a curated set of bus events.

    Idempotent: safe to call multiple times.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    events.subscribe("task.status_changed", _on_bus_event("task_status_changed"))
    events.subscribe("scheduler.dag_released", _on_bus_event("state_changed"))
    events.subscribe("scheduler.requeued", _on_bus_event("state_changed"))
    # Foreign-storage events surface to the bell + Foreign Storage tab.
    events.subscribe("storage.offer_incoming", _on_bus_event("storage_offer_incoming"))
    events.subscribe("storage.deposit_accepted", _on_bus_event("storage_deposit_accepted"))
    events.subscribe("storage.deposit_completed", _on_bus_event("storage_deposit_completed"))
    events.subscribe("storage.eviction_requested", _on_bus_event("storage_eviction_requested"))
    events.subscribe("storage.eviction_cancelled", _on_bus_event("storage_eviction_cancelled"))
    events.subscribe("storage.deposit_purged", _on_bus_event("storage_deposit_purged"))
    # Cloud-eviction tier progress events.
    events.subscribe(
        "storage.cloud_upload_progress",
        _on_bus_event("storage_cloud_upload_progress"),
    )
    events.subscribe(
        "storage.cloud_upload_complete",
        _on_bus_event("storage_cloud_upload_complete"),
    )
    events.subscribe(
        "storage.cloud_upload_failed",
        _on_bus_event("storage_cloud_upload_failed"),
    )
    # Live transfer progress (depositor + host roles).
    events.subscribe(
        "storage.transfer_progress",
        _on_bus_event("storage_transfer_progress"),
    )
    # Batch C: unauthorized-access tripwire (depositor toast + audit).
    events.subscribe(
        "storage.unauthorized_access_detected",
        _on_bus_event("storage_unauthorized_access_detected"),
    )
    # P2: auto-mode fan-out timeout / all-declined / lost-state — depositor
    # toast asking the user to redo (never auto-retry, by design).
    events.subscribe(
        "storage.auto_offer_failed",
        _on_bus_event("storage_auto_offer_failed"),
    )
    # P2: candidate-side bell when a depositor cancels their offer
    # (because another peer accepted first or because the offer timed out).
    events.subscribe(
        "storage.offer_cancelled",
        _on_bus_event("storage_offer_cancelled"),
    )
    # Depositor-side: host bounced our view-grant frame so the UI can
    # roll the row back and surface the reason. Otherwise the depositor
    # silently thinks the share went through.
    events.subscribe(
        "storage.view_grant_rejected",
        _on_bus_event("storage_view_grant_rejected"),
    )
    # Depositor-side: host acked our view-grant frame after caching the
    # key. UI flips "Share pending" → "Shared".
    events.subscribe(
        "storage.view_grant_accepted",
        _on_bus_event("storage_view_grant_accepted"),
    )
    _INSTALLED = True


__all__ = [
    "register_ws",
    "unregister_ws",
    "broadcast_ui_update",
    "install_event_bridge",
]
