"""UDP-broadcast discovery on port 34567.

Extracted from node_modified.py:

* ``UDPDiscoveryProtocol`` — lines 2730-2762
* ``create_datagram_endpoint`` wiring — lines 6069-6075

Every node broadcasts a beacon over UDP containing its UUID, display
name, and a light stats summary. This module owns the receive side — a
listener that registers remote UUID→IP mappings and flips peer presence to
online when a beacon arrives.

The broadcast side lives in :mod:`nexus.networking.gossip`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from nexus.core import DEFAULT_DISCOVERY_PORT, STATE
from nexus.core.identity import get_or_create_node_uuid, register_peer_uuid

_log = logging.getLogger("nexus.networking.discovery")


class UDPDiscoveryProtocol(asyncio.DatagramProtocol):
    """Passive listener: registers every ``nexus_beacon`` datagram seen."""

    def __init__(self, default_port: int = DEFAULT_DISCOVERY_PORT):
        self._default_port = default_port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        from nexus.telemetry import presence  # local: avoid circular at import time

        try:
            msg = json.loads(data.decode())
            beacon_uuid = msg.get("identity", "")
            if (
                msg.get("action") != "nexus_beacon"
                or not beacon_uuid
                or beacon_uuid == get_or_create_node_uuid()
            ):
                return
            beacon_port = msg.get("port", self._default_port)
            real_ip_port = f"{addr[0]}:{beacon_port}"
            register_peer_uuid(beacon_uuid, real_ip_port)
            STATE.discovered_peers[beacon_uuid] = (
                time.time(),
                str(msg.get("display_name", "") or ""),
                "lan",
                msg.get("stats", {}),
                bool(msg.get("hide_profile", False)),
                real_ip_port,
            )
            try:
                presence.touch(real_ip_port, source="udp")
                presence.touch(beacon_uuid, source="udp")
            except Exception:
                pass
        except Exception:
            _log.debug("discovery: malformed datagram", exc_info=True)


async def start_discovery(port: int = DEFAULT_DISCOVERY_PORT):
    """Bind the UDP listener on ``0.0.0.0:port``. Returns the transport."""
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: UDPDiscoveryProtocol(port),
            local_addr=("0.0.0.0", port),
            allow_broadcast=True,
        )
        return transport
    except Exception:
        _log.debug("UDP discovery endpoint failed to bind", exc_info=True)
        return None


def lookup_discovered_peer(ip_port: str) -> tuple[str | None, tuple | None]:
    """Resolve *ip_port* to a ``(uuid, entry)`` in ``STATE.discovered_peers``.

    Ported from node_modified.py (``_lookup_discovered_peer`` at
    lines 880-925). Walks several matching strategies so a peer discovered
    by UUID via beacon is still reachable when the caller only has the raw
    LAN IP (and vice versa).
    """
    discovered = STATE.discovered_peers
    if ip_port in discovered:
        entry = discovered[ip_port]
        if isinstance(entry, tuple):
            return ip_port, entry

    # Strategy 1: direct ``IP → UUID`` lookup from identity mappings.
    from nexus.core.identity import _IP_TO_UUID  # module-private, intentional

    uuid_key = _IP_TO_UUID.get(ip_port, "")
    if uuid_key and uuid_key in discovered:
        entry = discovered[uuid_key]
        if isinstance(entry, tuple):
            return uuid_key, entry

    # Strategy 2: same port, different interface (loopback vs LAN on one host).
    peer_host = ip_port.split(":")[0] if ":" in ip_port else ip_port
    peer_port = ip_port.split(":")[1] if ":" in ip_port else ""
    for alt_ip, alt_uuid in _IP_TO_UUID.items():
        if alt_uuid in discovered:
            alt_host = alt_ip.split(":")[0] if ":" in alt_ip else alt_ip
            alt_port = alt_ip.split(":")[1] if ":" in alt_ip else ""
            if peer_port and alt_port == peer_port and alt_host != peer_host:
                entry = discovered[alt_uuid]
                if isinstance(entry, tuple):
                    return alt_uuid, entry

    # Strategy 3: iterate discovered peers and match on the stored real_ip (index 5).
    for disc_id, disc_entry in discovered.items():
        if not isinstance(disc_entry, tuple) or len(disc_entry) <= 5:
            continue
        disc_real_ip = disc_entry[5]
        if not disc_real_ip:
            continue
        if disc_real_ip == ip_port:
            return disc_id, disc_entry
        disc_port = disc_real_ip.split(":")[1] if ":" in disc_real_ip else ""
        if peer_port and disc_port == peer_port:
            return disc_id, disc_entry

    return None, None


__all__ = ["UDPDiscoveryProtocol", "start_discovery", "lookup_discovered_peer"]
