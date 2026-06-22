"""Shared WebSocket connection registry (master-side).

Extracted from node_modified.py (lines 610-647).

The master keeps a dict of active worker WebSockets keyed by worker IP /
UUID. This manager centralizes the accept, disconnect, and broadcast
flows so API routers (``/ws``) and scheduler (``broadcast_ping`` when new
work arrives) share one view of who is connected.

The UI broadcaster lives elsewhere (:mod:`nexus.ui`) and subscribes to the
``scheduler.dag_released`` / ``scheduler.requeued`` events to do its own
notifications — no cross-import needed.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("nexus.networking.connection_manager")


class ConnectionManager:
    """Registry of active worker WebSockets."""

    def __init__(self) -> None:
        self.active_connections: dict[str, Any] = {}

    async def connect(self, ws, ip: str) -> None:
        await ws.accept()
        self.active_connections[ip] = ws

    def disconnect(self, ip: str) -> None:
        self.active_connections.pop(ip, None)

    async def broadcast_ping(self) -> None:
        """Tell every connected worker there's new work to pull."""
        for ws in list(self.active_connections.values()):
            try:
                await ws.send_json({"type": "task_available"})
            except Exception:
                _log.debug("broadcast_ping failed", exc_info=True)

    async def broadcast_json(self, data: dict) -> None:
        for ws in list(self.active_connections.values()):
            try:
                await ws.send_json(data)
            except Exception:
                _log.debug("broadcast_json failed", exc_info=True)


ws_manager = ConnectionManager()
"""Process-wide connection manager singleton."""


__all__ = ["ConnectionManager", "ws_manager"]
