"""Peer discovery, peer protocol, worker-client, relay client.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.networking.connection_manager import ConnectionManager, ws_manager
from nexus.networking.discovery import UDPDiscoveryProtocol, start_discovery
from nexus.networking.gossip import gossip_broadcaster_loop
from nexus.networking.peer_protocol import (
    check_join_rate_limit,
    clear_rate_limits,
    set_grid_key_provider,
    sign_callback_hmac,
    sign_join_request,
    verify_callback_hmac,
    verify_join_hmac,
)
from nexus.networking.peer_http import peer_http_post
from nexus.networking.relay_client import (
    get_grid_key,
    get_relay_url,
    relay_client_loop,
    relay_http_request,
    relay_send,
    relay_send_to_peer,
    set_relay_cli_overrides,
)
from nexus.networking.websocket_client import open_worker_websocket
from nexus.networking.worker_client import (
    master_manager_loop,
    start_worker_client,
)


async def get_connected_master_peers() -> list[str]:
    """Return the sorted list of trusted masters (+ dual-role peers).

    Ported from node_modified.py (lines 1900-1913). Queries the
    ``peers`` table for every peer with ``status='trusted'`` whose role
    is ``master`` or ``dual``.
    """
    from sqlalchemy import select

    from nexus.storage import Peer, get_session

    async with get_session() as db:
        masters = (
            (
                await db.execute(
                    select(Peer).filter(
                        Peer.status == "trusted",
                        Peer.role.in_(["master", "dual"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(peer.ip for peer in masters)


__all__ = [
    # discovery + gossip
    "UDPDiscoveryProtocol",
    "start_discovery",
    "gossip_broadcaster_loop",
    # connection manager
    "ConnectionManager",
    "ws_manager",
    # peer protocol
    "set_grid_key_provider",
    "check_join_rate_limit",
    "sign_join_request",
    "verify_join_hmac",
    "sign_callback_hmac",
    "verify_callback_hmac",
    "clear_rate_limits",
    # worker client
    "start_worker_client",
    "master_manager_loop",
    # relay client
    "relay_send",
    "relay_send_to_peer",
    "relay_client_loop",
    "relay_http_request",
    "get_relay_url",
    "get_grid_key",
    "set_relay_cli_overrides",
    # outbound websocket helper
    "open_worker_websocket",
    # peer http helper
    "peer_http_post",
    # helpers
    "get_connected_master_peers",
]
