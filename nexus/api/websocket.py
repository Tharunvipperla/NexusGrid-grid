"""Peer + UI WebSocket endpoints.

Ported from Phase-1/node_modified.py:

* ``/peer/ws`` (``websocket_endpoint``) — lines 6680-6762.
  Master-side worker connection: token-authenticated, receives
  ``heartbeat`` and ``bye`` frames, keeps ``STATE.active_workers`` fresh.

* ``/local/ws`` (``ui_websocket_endpoint``) — lines 8459-8478.
  UI live-feed: loopback / private-network only, token via query string,
  sockets registered with :mod:`nexus.ui.broadcaster` for fan-out.

The admission gates + presence updates delegate to already-ported helpers;
this file is just the WebSocket transport shell.
"""

from __future__ import annotations

import hmac
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from nexus.core import STATE
from nexus.core.identity import register_peer_uuid, resolve_ip_to_uuid
from nexus.networking.connection_manager import ws_manager
from nexus.runtime import refresh_worker_task_leases
from nexus.security.auth import _management_client_allowed
from nexus.security.crypto import verify_bye
from nexus.security.tokens import get_local_api_token
from nexus.storage import Peer, get_session
from nexus.telemetry import presence
from nexus.ui.broadcaster import register_ws as ui_register_ws
from nexus.ui.broadcaster import unregister_ws as ui_unregister_ws
from nexus.utils.net import client_host

_log = logging.getLogger("nexus.api.websocket")

router = APIRouter(tags=["WebSockets"])

# Track the last benchmark timestamp we persisted per worker so we only
# touch the DB when a worker re-runs its self-bench (not every heartbeat).
_last_persisted_bench_at: dict[str, str] = {}


# ---------------------------------------------------------------------------
# /peer/ws  — master side of the worker connection
# ---------------------------------------------------------------------------

@router.websocket("/peer/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Worker WebSocket connection for heartbeats and task coordination."""
    from sqlalchemy import select

    req_token = websocket.headers.get("X-Cluster-Key")
    announced_address = websocket.headers.get("X-Node-Address")

    # Always require token auth — no loopback bypass (Phase-1 line 6685).
    # Reject missing/empty token up-front: `Peer.my_auth_token == None`
    # compiles to `IS NULL` and would otherwise match rows seeded without an
    # explicit token.
    if not req_token:
        await websocket.close(code=1008)
        return
    peer_skey = ""
    async with get_session() as db:
        peer = (
            await db.execute(
                select(Peer).filter(
                    Peer.my_auth_token == req_token,
                    Peer.status == "trusted",
                )
            )
        ).scalar_one_or_none()
    worker_id, valid = (peer.ip, True) if peer else (None, False)
    peer_skey = (peer.signing_key or "") if peer else ""

    if not valid:
        await websocket.close(code=1008)
        return

    await ws_manager.connect(websocket, worker_id)
    async with STATE.inbound_peer_ws_lock:
        STATE.inbound_peer_ws[worker_id] = websocket
    presence.mark_peer_online(worker_id, source="ws")

    # Populate UUID↔IP mapping from the incoming connection so the reverse
    # direction (us dialing this peer back) can resolve UUID → real IP. This
    # matters when both nodes run on the same host and the UDP beacons can't
    # share port 34567.
    if announced_address:
        client_h = websocket.client.host if websocket.client else ""
        incoming_real = (
            f"{client_h}:{announced_address.split(':')[-1]}"
            if ":" in str(announced_address)
            else ""
        )
        if incoming_real:
            incoming_uuid = resolve_ip_to_uuid(worker_id)
            if incoming_uuid and incoming_uuid != worker_id:
                register_peer_uuid(incoming_uuid, incoming_real)
            elif str(worker_id).startswith("nexus_"):
                register_peer_uuid(worker_id, incoming_real)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "heartbeat":
                stats = data.get("stats", {})
                stats["connection_type"] = "lan"
                # Keep UUID→IP mapping fresh from the heartbeat's node_identity
                hb_identity = stats.get("node_identity", "")
                if hb_identity and ":" in hb_identity:
                    hb_uuid = resolve_ip_to_uuid(worker_id)
                    if hb_uuid and hb_uuid != worker_id:
                        register_peer_uuid(hb_uuid, hb_identity)
                    if announced_address and ":" in str(announced_address):
                        register_peer_uuid(
                            resolve_ip_to_uuid(worker_id) or worker_id,
                            str(announced_address),
                        )
                async with STATE.worker_state_lock:
                    STATE.active_workers[worker_id] = {
                        "stats": stats,
                        "last_seen": time.time(),
                    }
                bench_at = str(stats.get("bench_at", "") or "")
                bench_score = stats.get("bench")
                if (
                    bench_at
                    and bench_score
                    and _last_persisted_bench_at.get(worker_id) != bench_at
                ):
                    try:
                        async with get_session() as db:
                            row = (
                                await db.execute(
                                    select(Peer).filter(Peer.ip == worker_id)
                                )
                            ).scalar_one_or_none()
                            if row:
                                row.benchmark_score = float(bench_score)
                                row.benchmark_at = bench_at
                                await db.commit()
                        _last_persisted_bench_at[worker_id] = bench_at
                    except Exception as exc:
                        _log.debug(
                            "failed to persist benchmark for %s: %s", worker_id, exc
                        )
                presence.mark_peer_online(worker_id, source="ws")
                await refresh_worker_task_leases(worker_id)
            elif msg_type in ("tunnel_data", "tunnel_close"):
                from nexus.networking.tunnel import (
                    handle_master_tunnel_close,
                    handle_master_tunnel_data,
                )

                if msg_type == "tunnel_data":
                    await handle_master_tunnel_data(data)
                else:
                    await handle_master_tunnel_close(data)
            elif msg_type == "tunnel_udp_recv":
                from nexus.networking.tunnel import handle_master_tunnel_udp_recv

                await handle_master_tunnel_udp_recv(data)
            elif msg_type in ("svc_open", "svc_data", "svc_close"):
                from nexus.runtime.service_tunnel import dispatch_service_frame

                await dispatch_service_frame(worker_id, data)
            elif isinstance(msg_type, str) and msg_type.startswith("storage_"):
                from nexus.networking.storage_pump import dispatch_storage_frame

                await dispatch_storage_frame(worker_id, data)
            elif msg_type == "bye":
                bye_node = data.get("node_id", "")
                bye_ts = data.get("ts", 0)
                bye_sig = data.get("sig", "")
                if not verify_bye(bye_node, bye_ts, bye_sig, key=peer_skey):
                    _log.warning(
                        "Ignoring bye with invalid signature from %s", worker_id
                    )
                    continue
                presence.mark_peer_offline(worker_id, source="ws")
                async with STATE.worker_state_lock:
                    STATE.active_workers.pop(worker_id, None)
                break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(worker_id)
        async with STATE.worker_state_lock:
            STATE.active_workers.pop(worker_id, None)
        async with STATE.inbound_peer_ws_lock:
            STATE.inbound_peer_ws.pop(worker_id, None)


# ---------------------------------------------------------------------------
# /local/ws  — UI live feed
# ---------------------------------------------------------------------------

@router.websocket("/local/ws")
async def ui_websocket_endpoint(websocket: WebSocket) -> None:
    """Browser-facing live-update WebSocket.

    Gates (Phase-1 parity):
    * caller host must be loopback/private unless ``NEXUS_ALLOW_REMOTE_UI``;
    * ``?token=<LOCAL_API_TOKEN>`` must match constant-time.
    """
    if not _management_client_allowed(client_host(websocket)):
        await websocket.close(code=1008)
        return
    token = websocket.query_params.get("token", "")
    if not hmac.compare_digest(token, get_local_api_token()):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    ui_register_ws(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ui_unregister_ws(websocket)


__all__ = ["router"]
