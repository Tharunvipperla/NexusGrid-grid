"""Periodic UDP beacon broadcaster + stale-peer sweeper.

Extracted from node_modified.py (lines 4853-4897).

This module sends the *outbound* half of discovery: it pushes a beacon
describing this node (UUID, display name, live CPU/RAM/GPU stats) every
few seconds and, while it's at it, prunes ``STATE.discovered_peers``
entries that have not heartbeated in the last 15 seconds.

If the relay connection is up, the same beacon is also wrapped inside a
``type=broadcast`` frame and shipped over the relay so cross-region peers
can see us.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import psutil

from nexus.core import (
    DEFAULT_DISCOVERY_PORT,
    LOCAL_SETTINGS,
    STATE,
)
from nexus.core.identity import get_or_create_node_uuid
from nexus.telemetry.hardware import get_gpu_stats

_log = logging.getLogger("nexus.networking.gossip")


async def gossip_broadcaster_loop(
    node_port: int,
    *,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    interval: float = 3.0,
) -> None:
    """Forever: broadcast our beacon and prune stale discovered peers."""
    # Lazy relay_send import keeps gossip usable even when relay is disabled.
    try:
        from nexus.networking.relay_client import (  # type: ignore
            assemble_local_grid_keys,
            relay_send,
        )
    except Exception:
        relay_send = None  # type: ignore[assignment]
        assemble_local_grid_keys = None  # type: ignore[assignment]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        while True:
            beacon = None
            if LOCAL_SETTINGS.get("node_online", True):
                try:
                    mem = psutil.virtual_memory()
                    stats = {
                        "cpu_pct": psutil.cpu_percent(interval=None),
                        "ram_total_mb": mem.total // (1024 * 1024),
                        "ram_free_mb": mem.available // (1024 * 1024),
                        "cpu_cores": psutil.cpu_count(logical=False) or 1,
                        "gpu": bool(LOCAL_SETTINGS.get("node_gpu", False)),
                    }
                    if stats["gpu"]:
                        gs = get_gpu_stats()
                        stats["gpu_name"] = gs.get("gpu_name", "")
                        stats["vram_free_mb"] = gs.get("gpu_mem_free_mb", 0)
                        stats["vram_total_mb"] = gs.get("gpu_mem_total_mb", 0)
                    beacon = {
                        "action": "nexus_beacon",
                        "identity": get_or_create_node_uuid(),
                        "port": node_port,
                        "display_name": str(
                            LOCAL_SETTINGS.get("user_display_name", "") or ""
                        ),
                        "hide_profile": LOCAL_SETTINGS.get("hide_profile", True),
                        "stats": stats,
                    }
                    sock.sendto(
                        json.dumps(beacon).encode(),
                        ("<broadcast>", discovery_port),
                    )
                except Exception:
                    _log.debug("Beacon broadcast failed", exc_info=True)
                if (
                    STATE.relay_connected
                    and beacon is not None
                    and relay_send
                    and assemble_local_grid_keys
                ):
                    # Emit one bucketed broadcast per local
                    # grid_key. Today that's one beacon per group this
                    # node belongs to — peers in other groups don't see
                    # the beacon at all, even when they share the relay.
                    try:
                        local_grid_keys = await assemble_local_grid_keys()
                        for gk in local_grid_keys:
                            await relay_send({
                                "type": "broadcast",
                                "grid_key": gk,
                                "payload": beacon,
                            })
                    except Exception:
                        _log.debug("Relay beacon broadcast failed", exc_info=True)

            # Prune peers we haven't heard from in 15s
            for peer_id in list(STATE.discovered_peers.keys()):
                entry = STATE.discovered_peers[peer_id]
                ts = entry[0] if isinstance(entry, tuple) else entry
                if time.time() - ts > 15:
                    del STATE.discovered_peers[peer_id]

            await asyncio.sleep(interval)
    finally:
        try:
            sock.close()
        except Exception:
            pass


__all__ = ["gossip_broadcaster_loop"]
