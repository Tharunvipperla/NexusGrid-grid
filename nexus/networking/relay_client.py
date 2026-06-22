"""Relay WebSocket connection + HTTP-over-relay bridge.

Ported from Phase-1/node_modified.py:

* ``_get_relay_url`` / ``_get_grid_key`` — lines 4900-4914
* ``relay_send`` / ``relay_send_to_peer`` — lines 4917-4934
* ``relay_client_loop`` — lines 4937-5173
* ``_handle_relayed_peer_message`` — lines 5176-5180
* ``_handle_relayed_http_request`` — lines 5183-5656
* ``relay_http_request`` — lines 5686-5725

The loop registers with the relay server, maintains a heartbeat, and
dispatches three kinds of inbound frames:

1. ``peer_list`` — roster updates (mirrored into ``STATE.discovered_peers``
   so the UI shows relay-only peers alongside LAN beacons).
2. ``relayed`` — a payload from another node. Sub-types:
   ``peer_ws_frame`` (logged for now), ``http_request`` (dispatched to the
   local peer handler and replied via :func:`relay_send_to_peer`),
   ``http_response`` (wakes the ``relay_http_request`` pending entry).
3. ``relayed_broadcast`` — a gossip beacon; updates presence + discovery.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import secrets
import time
import uuid
import zipfile
from urllib.parse import quote

import websockets
from fastapi import HTTPException
from sqlalchemy import delete, select

from nexus.core import LOCAL_SETTINGS, STATE, TERMINAL_STATES
from nexus.core.identity import (
    get_or_create_node_uuid,
    register_peer_uuid,
    resolve_uuid_to_ip,
)
from nexus.networking.peer_protocol import (
    check_join_rate_limit,
    verify_callback_hmac,
    verify_join_hmac,
)
from nexus.scheduler.reliability import record_worker_outcome
from nexus.security.crypto import verify_signature
from nexus.storage import Peer, TaskRecord, get_session
from nexus.telemetry import incr_metric, presence, write_audit_event
from nexus.utils.text import safe_extractall
from nexus.utils.time import timestamp

_log = logging.getLogger("nexus.networking.relay_client")

# CLI-overridable values set by ``set_relay_cli_overrides`` at startup.
_cli_relay_url: str = ""
_cli_grid_key: str = ""


# ---------------------------------------------------------------------------
# CLI overrides + URL / grid-key resolution
# ---------------------------------------------------------------------------

def set_relay_cli_overrides(relay_url: str = "", grid_key: str = "") -> None:
    """Record CLI-provided relay URL + grid key (takes priority over settings)."""
    global _cli_relay_url, _cli_grid_key
    _cli_relay_url = str(relay_url or "").strip()
    _cli_grid_key = str(grid_key or "").strip()


def get_relay_url() -> str:
    """Return the relay WebSocket URL with ``https://``/``http://`` normalized."""
    url = _cli_relay_url or str(LOCAL_SETTINGS.get("relay_server_url", "") or "")
    url = url.strip().rstrip("/") if url else ""
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url


def get_grid_key() -> str:
    """Return the grid key (CLI override wins over ``relay_grid_key`` setting).

    deprecates the global grid_key as a routing primitive — the
    relay buckets by per-context keys derived in :func:`assemble_local_grid_keys`.
    This function survives because some relays still gate connection
    auth by a shared secret (``GRID_KEY`` env on their side), and we want
    custom-relay operators to keep that lever.
    """
    return (
        _cli_grid_key or str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    ).strip()


async def push_grid_key_update(
    *, added: list[str] | None = None, removed: list[str] | None = None,
) -> None:
    """Push ``add_grid_keys`` / ``remove_grid_keys`` to every live
    relay subscription so a group join/leave takes effect without a
    full reconnect cycle.

    No-op when no relay is connected. Caller is responsible for keeping
    the local DB membership state authoritative — this just keeps the
    relay's view in sync. Silent on per-WS send failures: the next
    reconnect re-derives ``grid_keys`` from the DB anyway.
    """
    add_list = sorted({k for k in (added or []) if k})
    rem_list = sorted({k for k in (removed or []) if k})
    if not add_list and not rem_list:
        return
    add_msg = json.dumps({"type": "add_grid_keys", "grid_keys": add_list}) if add_list else ""
    rem_msg = json.dumps({"type": "remove_grid_keys", "grid_keys": rem_list}) if rem_list else ""

    async with STATE.relay_ws_lock:
        primary = STATE.relay_ws
    if primary is not None:
        try:
            if add_msg:
                await primary.send(add_msg)
            if rem_msg:
                await primary.send(rem_msg)
        except Exception:
            _log.debug("push_grid_key_update primary failed", exc_info=True)

    async with STATE.relay_ws_pool_lock:
        pool_copy = list(STATE.relay_ws_pool.values())
    for ws in pool_copy:
        try:
            if add_msg:
                await ws.send(add_msg)
            if rem_msg:
                await ws.send(rem_msg)
        except Exception:
            _log.debug("push_grid_key_update pool failed", exc_info=True)


async def assemble_local_grid_keys() -> list[str]:
    """Build the per-context ``grid_keys`` this node subscribes to.

    Walks every active group this node is a member of and derives a
    stable grid_key per ``group_id``. The relay buckets all broadcast
    traffic by these keys, so a relay operator can no longer correlate
    the groups a single node belongs to (they see N independent buckets,
    not one).

    Pair-only relationships (trusted peers without a shared group) are
    not bucketed here — they route point-to-point via ``target=node_id``,
    which the relay already serves without a broadcast fan-out. If a
    future wave adds broadcast frames to a pair-context, derive its key
    via :func:`nexus.security.grid_keys.derive_pair_grid_key` and add it
    to the set returned here.
    """
    from sqlalchemy import select
    from nexus.security.grid_keys import derive_group_grid_key
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage import get_session
    from nexus.storage.models import Group, GroupMember

    keys: set[str] = set()
    me = get_local_group_pubkey()
    if not me:
        return []
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(GroupMember.group_id).where(
                        GroupMember.pubkey == me
                    )
                )
            ).fetchall()
            for (gid,) in rows:
                g = await session.get(Group, gid)
                if g is None or g.deleted_at:
                    continue
                k = derive_group_grid_key(gid)
                if k:
                    keys.add(k)
    except Exception:
        _log.warning("assemble_local_grid_keys failed", exc_info=True)
    return sorted(keys)


# ---------------------------------------------------------------------------
# Frame senders
# ---------------------------------------------------------------------------

async def relay_send(msg: dict) -> bool:
    """Send *msg* through the legacy primary relay WebSocket if connected.

    Kept for non-targeted frames (heartbeat, registration). Targeted
    peer routing should use :func:`relay_send_to_peer` so it picks the
    lowest-RTT pool relay per.
    """
    async with STATE.relay_ws_lock:
        if STATE.relay_ws:
            try:
                await STATE.relay_ws.send(json.dumps(msg))
                # Telemetry counter — tracks frames-per-relay
                # for the relay:host diagnostics view.
                from nexus.runtime import relay_telemetry
                await relay_telemetry.increment(get_relay_url())
                return True
            except Exception:
                return False
    return False


async def my_relay_pool_urls() -> list[str]:
    """Every relay URL this node is reachable through.

    Used to advertise our relay set to trusted peers at pair-handshake
    time so they can do intersection-based routing.

    Union of:

    * Legacy primary ``relay_server_url`` setting.
    * Every active pool connection (``STATE.relay_ws_pool``).
    * follow-up: the in-process local relay's LAN URL
      (``ws://<lan-ip>:<port>``) AND its Cloudflare tunnel URL
      (``wss://...trycloudflare.com``) when either is up. Without
      this, a node running *only* a local relay reports zero URLs
      and pair-link minting falsely 409s "no relay configured".

    Returns a deduplicated, sorted list.
    """
    urls: set[str] = set()
    primary = (get_relay_url() or "").strip()
    if primary:
        urls.add(primary)
    async with STATE.relay_ws_pool_lock:
        for url in STATE.relay_ws_pool.keys():
            if url:
                urls.add(url)
    # Local-relay LAN + public bindings — both are addresses peers can
    # dial back to us on.
    try:
        from nexus.runtime import local_relay
        st = local_relay.status()
        if st.get("running") and st.get("suggested_url"):
            urls.add(str(st["suggested_url"]).strip())
    except Exception:
        pass
    tunnel = str(LOCAL_SETTINGS.get("relay_self_heal_url", "") or "").strip()
    if tunnel:
        urls.add(tunnel)
    return sorted(u for u in urls if u)


async def relay_fingerprint_ok_for_group(
    relay_url: str, group_id: str
) -> tuple[bool, str]:
    """Validate the cached fingerprint for ``relay_url`` against
    the frozen ``Group.relay_code_fingerprint``.

    Returns ``(ok, reason)``.

    * ``ok=True`` when the group has no frozen fingerprint (anything goes)
      OR when the cached fingerprint for this URL matches.
    * ``ok=False`` when the group froze a fingerprint and we either have
      no cached value for this URL (relay hasn't reported one yet) or
      the cached value differs. ``reason`` is human-readable for UI.
    """
    if not relay_url or not group_id:
        return True, ""
    from sqlalchemy import select
    from nexus.storage import get_session
    from nexus.storage.models import Group

    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return True, ""
        expected = str(g.relay_code_fingerprint or "").strip().lower()
    if not expected:
        return True, ""
    got = STATE.relay_code_fingerprints.get(relay_url, "").strip().lower()
    if not got:
        return False, "relay did not advertise a code fingerprint"
    if got != expected:
        return False, f"code mismatch (got {got[:8]}…, expected {expected[:8]}…)"
    return True, ""


async def _group_relay_url_set(group_id: str) -> set[str]:
    """Relay URLs bound to ``group_id`` (active only).

    Used by group-context outbound routing to restrict the candidate
    pool to relays the receiver is guaranteed subscribed to (via
    's per-group pool subscription).
    """
    if not group_id:
        return set()
    from sqlalchemy import select
    from nexus.storage import get_session
    from nexus.storage.models import GroupRelayBinding

    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(GroupRelayBinding.relay_url).where(
                        (GroupRelayBinding.group_id == group_id)
                        & (GroupRelayBinding.status == "active")
                    )
                )
            ).fetchall()
        return {(u or "").strip() for (u,) in rows if u}
    except Exception:
        _log.debug(
            "[RELAY] _group_relay_url_set(%s) failed", group_id, exc_info=True
        )
        return set()


async def _group_relay_priorities(group_id: str) -> dict[str, int]:
    """``{relay_url: priority}`` for a group's active bindings.

    Used to order the send-candidate relays: higher priority first.
    Empty on any error — callers fall back to latency-only ordering.
    """
    from sqlalchemy import select
    from nexus.storage import get_session
    from nexus.storage.models import GroupRelayBinding

    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(
                        GroupRelayBinding.relay_url, GroupRelayBinding.priority
                    ).where(
                        (GroupRelayBinding.group_id == group_id)
                        & (GroupRelayBinding.status == "active")
                    )
                )
            ).fetchall()
        return {(u or "").strip(): int(p or 0) for (u, p) in rows if u}
    except Exception:
        return {}


async def _ordered_relay_ws_candidates(
    allowed_urls: set[str] | None = None,
    priorities: dict[str, int] | None = None,
) -> list[tuple[str, "Any"]]:  # noqa: F821
    """Return ``[(url, ws)]`` ordered by priority then latency.

    . Includes the legacy primary plus every secondary pool
    connection. Latency comes from the in-memory cache populated by
    ``nexus.runtime.relay_latency``; URLs with no probe hit sort last.

    if ``allowed_urls`` is provided, candidates are
    restricted to that set (typically a group's bound relays). When
    no in-pool connection matches, returns ``[]`` — caller will
    return False rather than blindly send through a relay the
    target isn't on.

    ``priorities`` (a ``{url: priority}`` map) takes precedence
    over latency — a higher priority sorts first — so operators can pin
    a preferred relay for fan-out. Ties fall back to latency.
    """
    candidates: list[tuple[str, "Any"]] = []  # noqa: F821
    primary_url = (get_relay_url() or "").strip()
    async with STATE.relay_ws_lock:
        primary_ws = STATE.relay_ws
    if primary_ws is not None and primary_url:
        if allowed_urls is None or primary_url in allowed_urls:
            candidates.append((primary_url, primary_ws))
    async with STATE.relay_ws_pool_lock:
        for url, ws in STATE.relay_ws_pool.items():
            if allowed_urls is None or url in allowed_urls:
                candidates.append((url, ws))
    from nexus.runtime import relay_latency

    prio = priorities or {}

    def _key(item: tuple[str, object]) -> tuple[int, int, str]:
        rtt = relay_latency.get(item[0])
        return (-prio.get(item[0], 0), rtt if rtt is not None else 10**9, item[0])

    candidates.sort(key=_key)
    return candidates


async def relay_send_to_peer(
    target_node_id: str,
    payload: dict,
    *,
    group_id: str | None = None,
    allowed_relay_urls: set[str] | None = None,
) -> bool:
    """Send a relay-routed message to a specific peer.

    tries the lowest-RTT relay connection first and falls
    back through the rest on send error. The receiver is subscribed
    to every relay any of their groups is bound to (via 's
    pool), so whichever relay we pick that the target is also on,
    they receive it. ``http_response`` round-trips work identically
    because the pending-request dict is keyed by ``request_id`` and
    any of our pool's connections can wake it.

    callers that know the group context pass ``group_id``
    so candidates are restricted to that group's bound relays. The
    receiver, as a group member, is guaranteed subscribed to those
    relays — eliminates the "lowest-RTT relay isn't on the target's
    pool" silent-drop case described under W36.C's known limitation.

    cross-group P2P callers (e.g. ``peer_http_post``)
    pre-compute the allow-set themselves from the peer's advertised
    ``peer_relay_urls`` and pass it as ``allowed_relay_urls``. When
    both ``group_id`` and ``allowed_relay_urls`` are provided, the
    effective allow-set is their intersection.
    """
    allowed: set[str] | None = None
    priorities: dict[str, int] | None = None
    if group_id:
        allowed = await _group_relay_url_set(group_id)
        priorities = await _group_relay_priorities(group_id)
    if allowed_relay_urls is not None:
        allowed = (allowed & allowed_relay_urls) if allowed is not None else set(allowed_relay_urls)
    msg = {"type": "relay", "target": target_node_id, "payload": payload}
    body = json.dumps(msg)
    for url, ws in await _ordered_relay_ws_candidates(allowed, priorities):
        try:
            await ws.send(body)
            # Mark this pool relay as active so the
            # idle-disconnect sweep doesn't reclaim it under us.
            _touch_relay_activity(url)
            # Per-URL telemetry counter for relay:host diagnostics.
            from nexus.runtime import relay_telemetry
            await relay_telemetry.increment(url)
            return True
        except Exception:
            _log.debug(
                "[RELAY] send via %s failed; trying next pool member", url
            )
            continue
    return False


async def relay_http_request(
    target_node_id: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 10.0,
    *,
    group_id: str | None = None,
    allowed_relay_urls: set[str] | None = None,
) -> dict:
    """Send an HTTP request to a peer through the relay and wait for the response.

    callers with a known group context pass ``group_id``
    so :func:`relay_send_to_peer` restricts the candidate relays to
    that group's bindings (guaranteeing the receiver is subscribed).

    cross-group P2P callers pass ``allowed_relay_urls``
    (the peer's advertised relay-pool set) so candidates are
    restricted to relays both ends share.
    """
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    pending_entry: dict[str, object] = {"event": event, "response": None}

    async with STATE.relay_http_pending_lock:
        STATE.relay_http_pending[request_id] = pending_entry

    payload = {
        "type": "http_request",
        "method": method,
        "path": path,
        "body": body or {},
        "request_id": request_id,
    }
    success = await relay_send_to_peer(
        target_node_id, payload,
        group_id=group_id, allowed_relay_urls=allowed_relay_urls,
    )
    if not success:
        async with STATE.relay_http_pending_lock:
            STATE.relay_http_pending.pop(request_id, None)
        return {"status": 503, "body": {"error": "Relay not connected"}}

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        async with STATE.relay_http_pending_lock:
            STATE.relay_http_pending.pop(request_id, None)
        return {"status": 504, "body": {"error": "Relay request timed out"}}

    response = pending_entry["response"]
    async with STATE.relay_http_pending_lock:
        STATE.relay_http_pending.pop(request_id, None)
    return response or {"status": 500, "body": {"error": "No response"}}


async def relay_http_request_one_shot(
    relay_url: str,
    grid_key: str,
    target_node_id: str,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = 10.0,
) -> dict:
    """Single-shot HTTP-over-relay via a transient WebSocket.

    Opens a fresh WS to ``relay_url`` authenticated with ``grid_key``,
    sends one ``http_request`` to ``target_node_id``, waits for the
    matching ``http_response``, then closes. Lets a joiner reach a
    NAT'd admin's relay using the credentials embedded in the join link
    even when the joiner isn't otherwise connected to that relay.
    """
    from nexus.core.identity import get_node_port

    node_id = get_or_create_node_uuid()
    ws_url = f"{relay_url}/relay/{quote(node_id, safe='')}"
    request_id = str(uuid.uuid4())
    try:
        async with websockets.connect(ws_url, open_timeout=timeout) as ws:
            await ws.send(json.dumps({
                "type": "register",
                "grid_key": grid_key,
                "display_name": "",
                "port": get_node_port(),
                "capabilities": {},
                "hide_profile": True,
            }))
            ack_raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            ack = json.loads(ack_raw)
            if ack.get("type") == "error":
                return {"status": 401, "body": {
                    "error": f"relay rejected grid_key: {ack.get('message', '')}",
                }}
            await ws.send(json.dumps({
                "type": "relay",
                "target": target_node_id,
                "payload": {
                    "type": "http_request",
                    "method": method,
                    "path": path,
                    "body": body or {},
                    "request_id": request_id,
                },
            }))
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return {"status": 504, "body": {"error": "relay timeout"}}
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                data = json.loads(raw)
                if data.get("type") != "relayed":
                    continue
                payload = data.get("payload", {})
                if (payload.get("type") == "http_response"
                        and payload.get("request_id") == request_id):
                    return {
                        "status": int(payload.get("status", 500)),
                        "body": payload.get("body", {}) or {},
                    }
    except Exception as exc:
        return {"status": 503, "body": {
            "error": f"one-shot relay request failed: {exc}",
        }}


# ---------------------------------------------------------------------------
# Recv-message dispatch (shared by legacy primary loop + pool loops)
# ---------------------------------------------------------------------------


async def _handle_relay_message(data: dict, node_id: str) -> None:
    """Dispatch one relay-server frame to the right handler.

    Pulled out of ``relay_client_loop`` in so the secondary
    pool connections (``_pool_connection_loop``) reuse the exact same
    routing — inbound from any of a group's bound relays is handled
    identically regardless of which connection received it.
    """
    msg_type = data.get("type", "")

    if msg_type == "peer_list":
        await _apply_peer_list(data, node_id)
    elif msg_type == "relayed":
        from_id = data.get("from", "")
        payload = data.get("payload", {})
        payload_type = payload.get("type", "")
        if payload_type == "peer_ws_frame":
            await _handle_relayed_peer_message(
                from_id, payload.get("data", {})
            )
        elif payload_type == "http_request":
            await _handle_relayed_http_request(from_id, payload)
        elif payload_type == "http_response":
            rid = payload.get("request_id", "")
            async with STATE.relay_http_pending_lock:
                entry = STATE.relay_http_pending.get(rid)
            if entry:
                entry["response"] = {
                    "status": payload.get("status", 500),
                    "body": payload.get("body", {}),
                    "headers": payload.get("headers", {}),
                }
                entry["event"].set()
        elif payload_type == "tunnel_open":
            from nexus.networking.tunnel import handle_worker_tunnel_open

            await handle_worker_tunnel_open(from_id, payload)
        elif payload_type == "tunnel_data":
            from nexus.networking.tunnel import (
                handle_master_tunnel_data,
                handle_worker_tunnel_data,
            )

            # Direction discriminates which side handles it.
            if payload.get("dir") == "to_worker":
                await handle_worker_tunnel_data(payload)
            else:
                await handle_master_tunnel_data(payload)
        elif payload_type == "tunnel_close":
            from nexus.networking.tunnel import (
                handle_master_tunnel_close,
                handle_worker_tunnel_close,
            )

            # Try both sides — only one will find the stream.
            await handle_master_tunnel_close(payload)
            await handle_worker_tunnel_close(payload)
        elif payload_type == "tunnel_udp_send":
            from nexus.networking.tunnel import handle_worker_tunnel_udp_send

            await handle_worker_tunnel_udp_send(from_id, payload)
        elif payload_type == "tunnel_udp_recv":
            from nexus.networking.tunnel import handle_master_tunnel_udp_recv

            await handle_master_tunnel_udp_recv(payload)
        elif payload_type in ("svc_open", "svc_data", "svc_close"):
            from nexus.runtime.service_tunnel import dispatch_service_frame

            await dispatch_service_frame(from_id, payload)
        elif isinstance(payload_type, str) and payload_type.startswith(
            "storage_"
        ):
            from nexus.networking.storage_pump import dispatch_storage_frame

            await dispatch_storage_frame(from_id, payload)
        elif payload_type == "service_stop":
            from nexus.runtime.service_runner import stop_service

            tid = str(payload.get("task_id", ""))
            reason = str(payload.get("reason", "manual"))
            if tid:
                await stop_service(tid, reason=reason)
        elif payload_type == "service_prepare_standby":
            from nexus.runtime.service_replication import prepare_standby

            tid = str(payload.get("task_id", ""))
            mf = payload.get("manifest") or {}
            if tid and isinstance(mf, dict):
                asyncio.create_task(prepare_standby(tid, mf, from_id))
        elif payload_type == "service_promote_with_snapshot":
            from nexus.runtime.service_replication import promote_standby

            tid = str(payload.get("task_id", ""))
            if tid:
                asyncio.create_task(promote_standby(tid))
        elif payload_type == "service_image_refresh":
            from nexus.runtime.service_replication import refresh_standby_image

            tid = str(payload.get("task_id", ""))
            image = str(payload.get("image", ""))
            if tid and image:
                asyncio.create_task(refresh_standby_image(tid, image))
        elif payload_type == "service_dep_grant":
            tid = str(payload.get("task_id", ""))
            peers = payload.get("peers") or []
            if tid and isinstance(peers, list):
                grants = STATE.service_dep_grants.setdefault(tid, set())
                for p in peers:
                    if isinstance(p, str) and p:
                        grants.add(p)
        elif payload_type == "service_dep_changed":
            from nexus.networking.tunnel import ensure_dependency_tunnel

            tid = str(payload.get("task_id", ""))
            primary = str(payload.get("primary", ""))
            try:
                port = int(payload.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if tid and primary and port:
                asyncio.create_task(
                    ensure_dependency_tunnel(tid, primary, port)
                )
    elif msg_type == "relayed_broadcast":
        await _apply_relayed_broadcast(data, node_id)
    elif msg_type == "pair_invite_probe":
        # Incoming pair request forwarded by the relay.
        # The relay already verified the signature + redemption gate;
        # we re-verify defensively + check our local PairInvite row
        # before notifying the user.
        await _handle_incoming_pair_probe(data)
    elif msg_type == "pair_reply_ack":
        # Relay-side confirmation that our accept/reject
        # reply was forwarded to the probe WS (or not, if the prober
        # had already disconnected). Informational only.
        if not data.get("delivered", True):
            _log.info(
                "[PAIR] pair_reply for %s could not be delivered "
                "(prober disconnected)", data.get("transient_id", ""),
            )
    elif msg_type == "heartbeat_ack":
        pass
    elif msg_type == "registered":
        _log.info(
            "[RELAY] Registration confirmed: %s", data.get("node_id")
        )
    elif msg_type == "relay_failed":
        _log.warning(
            "[RELAY] Relay failed to %s: %s",
            data.get("target"), data.get("reason"),
        )


async def _handle_incoming_pair_probe(data: dict) -> None:
    """Process an inbound ``pair_invite_probe`` frame.

    The relay forwards these to us when a stranger redeems one of our
    issued pair-invite links. We re-verify the signed payload (defense
    in depth — the relay already checked it), consult our local
    PairInvite row for revocation / expiry, then either:

    * **auto-reject** (signature bad, invite revoked / expired / unknown)
      by immediately sending ``pair_reply{decision: "reject"}`` back via
      the main relay WS, no user prompt;
    * **park for user decision** by storing in
      :mod:`nexus.runtime.pair_handshake`'s pending list and publishing
      a ``pair.request_incoming`` event for the UI.
    """
    from nexus.runtime import pair_handshake
    from nexus.security.pair_invite import verify_pair_invite
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage import get_session
    from nexus.storage.models import PairAttempt as PairAttemptRow
    from nexus.storage.models import PairInvite as PairInviteRow
    from nexus.utils.time import iso_now

    transient_id = str(data.get("transient_id", "")).strip()
    if not transient_id:
        return

    inv_payload = data.get("inv_payload") or {}
    invite_id = str(inv_payload.get("invite_id", "")).strip()
    bob_pubkey = str(data.get("bob_pubkey", "")).strip()
    bob_relay_urls = data.get("bob_relay_urls") or []
    if not isinstance(bob_relay_urls, list):
        bob_relay_urls = []
    bob_relay_urls = [str(u) for u in bob_relay_urls if isinstance(u, str)]
    bob_display_name = str(data.get("bob_display_name", "")).strip()[:80]

    async def _auto_reject(reason: str) -> None:
        await relay_send(
            {
                "type": "pair_reply",
                "transient_id": transient_id,
                "decision": "reject",
                "payload": {"reason": reason},
            }
        )

    if not invite_id or not bob_pubkey:
        await _auto_reject("malformed probe")
        return

    # Refuse self-pair. The relay can't always catch this
    # (relays don't know who issued vs redeemed); we authoritatively
    # know here because we are the issuer.
    my_pubkey = get_local_group_pubkey() or ""
    if my_pubkey and bob_pubkey == my_pubkey:
        await _auto_reject("cannot pair with self")
        return

    # Local PairInvite row check — defense-in-depth + revocation respect.
    async with get_session() as session:
        row = await session.get(PairInviteRow, invite_id)
        if row is None:
            # Should not happen — relay only forwards invites we issued.
            await _auto_reject("unknown invite")
            return
        if row.status in ("rejected", "revoked", "expired"):
            await _auto_reject(f"invite {row.status}")
            return
        # Permanent links bypass the per-link used_count gate — they
        # rely on PairAttempt for per-redeemer rate-limiting below.
        if not bool(row.is_permanent):
            if row.status == "redeemed" and (row.used_count or 0) >= (row.max_uses or 1):
                await _auto_reject("invite already redeemed")
                return

        # Per-(invite_id, bob_pubkey) rate-limit. Once Alice
        # has handled Bob's request (accept or reject), further requests
        # from the same Bob via the same link auto-reject without a UI
        # toast. Mirrors Twitter-follow semantics: one request per pair.
        attempt = await session.get(
            PairAttemptRow, (invite_id, bob_pubkey)
        )
        if attempt is not None:
            if attempt.decision in ("accepted", "rejected"):
                await _auto_reject(
                    f"already {attempt.decision} previously"
                )
                return
            # decision == "pending" — duplicate probe before user clicked.
            # Silently drop; the user already has the request parked.
            return
        # Record this attempt; decision flips on accept/reject.
        session.add(
            PairAttemptRow(
                invite_id=invite_id,
                bob_pubkey=bob_pubkey,
                first_seen_at=iso_now(),
                decided_at="",
                decision="pending",
            )
        )
        await session.commit()

    # Re-verify the signed envelope from row.signed_blob against the
    # invite_id; cheap insurance the relay didn't substitute a
    # different payload.
    inv = verify_pair_invite(
        (row.signed_blob or "").strip(),
        expected_issuer_pubkey=row.issuer_pubkey or None,
    )
    if inv is None or inv.invite_id != invite_id:
        await _auto_reject("local invite signature mismatch")
        return

    req = pair_handshake.IncomingPairRequest(
        transient_id=transient_id,
        invite_id=invite_id,
        bob_pubkey=bob_pubkey,
        bob_relay_urls=bob_relay_urls,
        bob_display_name=bob_display_name,
    )
    await pair_handshake.add(req)
    from nexus.runtime import event_bus

    await event_bus.publish({
        "type": "pair.request_incoming",
        "transient_id": transient_id,
        "invite_id": invite_id,
        "bob_pubkey": bob_pubkey,
        "bob_display_name": bob_display_name,
    })
    _log.info(
        "[PAIR] incoming request transient=%s invite=%s bob=%s",
        transient_id[:16], invite_id[:8], bob_pubkey[:8],
    )


# ---------------------------------------------------------------------------
# Main reconnect loop
# ---------------------------------------------------------------------------

async def relay_client_loop() -> None:
    """Maintain a persistent WebSocket connection to the relay server."""
    from nexus.core.identity import get_node_port

    backoff = 5
    while True:
        relay_url = get_relay_url()
        grid_key = get_grid_key()

        if (
            not relay_url
            or not grid_key
            or not LOCAL_SETTINGS.get("relay_enabled", True)
        ):
            STATE.relay_connected = False
            if relay_url and not grid_key:
                STATE.relay_last_error = "Grid key is required"
            else:
                STATE.relay_last_error = ""
            try:
                await asyncio.wait_for(
                    STATE.relay_settings_changed.wait(), timeout=5
                )
                STATE.relay_settings_changed.clear()
                backoff = 5
            except asyncio.TimeoutError:
                pass
            continue

        node_id = get_or_create_node_uuid()
        ws_url = f"{relay_url}/relay/{quote(node_id, safe='')}"

        try:
            async with websockets.connect(ws_url) as ws:
                # Per-context grid_keys go in the subscribe envelope.
                local_grid_keys = await assemble_local_grid_keys()
                reg_msg = {
                    "type": "register",
                    "grid_key": grid_key,
                    "grid_keys": local_grid_keys,
                    "display_name": str(
                        LOCAL_SETTINGS.get("user_display_name", "") or ""
                    ),
                    "port": get_node_port(),
                    "capabilities": {},
                    "hide_profile": LOCAL_SETTINGS.get("hide_profile", True),
                }
                await ws.send(json.dumps(reg_msg))

                try:
                    ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    ack = json.loads(ack_raw)
                    ack_type = ack.get("type", "unknown")
                    _log.info("[RELAY] Server response: %s", ack_type)
                    if ack_type == "error":
                        STATE.relay_last_error = ack.get(
                            "message",
                            "Authentication failed — check grid key",
                        )
                        _log.warning(
                            "[RELAY] Server rejected registration: %s",
                            STATE.relay_last_error,
                        )
                        STATE.relay_connected = False
                        try:
                            await asyncio.wait_for(
                                STATE.relay_settings_changed.wait(), timeout=backoff
                            )
                            STATE.relay_settings_changed.clear()
                            backoff = 5
                        except asyncio.TimeoutError:
                            backoff = min(backoff * 2, 120)
                        continue
                    # Cache the relay's reported code fingerprint
                    # so per-group bind validation can compare against
                    # the group's frozen value.
                    fp = str(ack.get("code_fingerprint", "") or "").strip().lower()
                    if fp:
                        STATE.relay_code_fingerprints[relay_url] = fp
                except asyncio.TimeoutError:
                    STATE.relay_last_error = (
                        "Registration timed out — server not responding"
                    )
                    _log.warning("[RELAY] Registration timed out")
                    continue

                async with STATE.relay_ws_lock:
                    STATE.relay_ws = ws
                STATE.relay_connected = True
                STATE.relay_last_error = ""
                backoff = 5
                _log.info("[RELAY] Connected to %s", relay_url)

                async def relay_heartbeat():
                    while True:
                        try:
                            # Cached encoded heartbeat constant.
                            await ws.send(_HEARTBEAT_BYTES)
                            await asyncio.sleep(10)
                        except Exception:
                            break

                hb = asyncio.create_task(relay_heartbeat())

                try:
                    while True:
                        raw = await ws.recv()
                        data = json.loads(raw)
                        await _handle_relay_message(data, node_id)
                finally:
                    hb.cancel()

        except websockets.exceptions.ConnectionClosedError as e:
            err_reason = e.reason or f"Connection closed (code {e.code})"
            if e.code == 4001 or "auth" in str(e.reason or "").lower():
                STATE.relay_last_error = "Invalid grid key — authentication failed"
            else:
                STATE.relay_last_error = err_reason
            _log.warning(
                "[RELAY] %s. Retrying in %ds...", STATE.relay_last_error, backoff
            )
        except ConnectionRefusedError:
            STATE.relay_last_error = "Cannot reach relay server — connection refused"
            _log.warning(
                "[RELAY] %s. Retrying in %ds...", STATE.relay_last_error, backoff
            )
        except Exception as e:
            err_msg = str(e)
            if (
                "getaddrinfo" in err_msg
                or "nodename" in err_msg
                or "Name or service not known" in err_msg
            ):
                STATE.relay_last_error = "Cannot reach relay server — invalid URL"
            elif "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
                STATE.relay_last_error = (
                    "Cannot reach relay server — connection timed out"
                )
            else:
                STATE.relay_last_error = f"Connection failed: {type(e).__name__}"
            _log.warning(
                "[RELAY] %s. Retrying in %ds...", STATE.relay_last_error, backoff
            )

        STATE.relay_connected = False
        async with STATE.relay_ws_lock:
            STATE.relay_ws = None
        try:
            await asyncio.wait_for(
                STATE.relay_settings_changed.wait(), timeout=backoff
            )
            STATE.relay_settings_changed.clear()
            backoff = 5
        except asyncio.TimeoutError:
            backoff = min(backoff * 2, 120)


# ---------------------------------------------------------------------------
# peer_list + broadcast handlers
# ---------------------------------------------------------------------------

async def _apply_peer_list(data: dict, node_id: str) -> None:
    """Replace ``STATE.relay_peers`` from a ``peer_list`` frame."""
    old_peer_ids = set(STATE.relay_peers.keys())
    new_peers: dict[str, dict] = {}
    async with STATE.discovered_peers_lock:
        for p in data.get("peers", []):
            pid = p.get("node_id", "")
            if not pid or pid == node_id:
                continue
            new_peers[pid] = p
            dname = p.get("display_name", "")
            STATE.discovered_peers[pid] = (
                time.time(),
                dname,
                "relay",
                p.get("capabilities", {}),
                bool(p.get("hide_profile", False)),
                "",
            )
            if p.get("status", "online") == "online":
                presence.mark_peer_online(pid, source="relay")
            else:
                presence.mark_peer_offline(pid, source="relay")
    STATE.relay_peers = new_peers
    new_peer_ids = set(new_peers.keys())
    for gone in old_peer_ids - new_peer_ids:
        presence.mark_peer_offline(gone, source="relay")
    if old_peer_ids != new_peer_ids:
        STATE.relay_peer_changed.set()


async def _apply_relayed_broadcast(data: dict, node_id: str) -> None:
    """Handle a gossip beacon that arrived via the relay server."""
    from_id = data.get("from", "")
    payload = data.get("payload", {})
    if payload.get("action") != "nexus_beacon" or from_id == node_id:
        return
    async with STATE.discovered_peers_lock:
        dname = payload.get("display_name", "")
        STATE.discovered_peers[from_id] = (
            time.time(),
            dname,
            "relay",
            payload.get("stats", {}),
            bool(payload.get("hide_profile", False)),
            "",
        )
    presence.mark_peer_online(from_id, source="relay-beacon")


# ---------------------------------------------------------------------------
# Inbound-message dispatchers
# ---------------------------------------------------------------------------

async def _handle_relayed_peer_message(from_id: str, data: dict) -> None:
    """Handle a peer WebSocket message delivered via the relay (stub)."""
    _log.debug(
        "[RELAY] Peer message from %s: %s",
        from_id, data.get("type", "unknown"),
    )


async def _handle_relayed_http_request(from_id: str, payload: dict) -> None:
    """Dispatch a tunneled HTTP request and send the response back."""
    from_ip = resolve_uuid_to_ip(from_id)
    method = payload.get("method", "GET").upper()
    path = payload.get("path", "")
    body = payload.get("body", {})
    request_id = payload.get("request_id", "")

    response_payload: dict = {
        "type": "http_response",
        "request_id": request_id,
        "status": 500,
        "body": {},
        "headers": {},
    }

    try:
        if path == "/peer/request_join" and method == "POST":
            if not check_join_rate_limit(from_id or from_ip):
                response_payload["status"] = 429
                response_payload["body"] = {"status": "rate_limited"}
                await relay_send_to_peer(from_id, response_payload)
                return
            if not verify_join_hmac(body):
                response_payload["status"] = 403
                response_payload["body"] = {"status": "invalid_join_hmac"}
                await relay_send_to_peer(from_id, response_payload)
                return

            addr = body.get("requester_address", from_ip)
            remote_name = str(body.get("display_name", "") or "").strip()[:50]
            remote_uuid = body.get("node_uuid", "")
            if remote_uuid and addr:
                register_peer_uuid(remote_uuid, addr)

            async with get_session() as db:
                peer = (
                    await db.execute(select(Peer).filter(Peer.ip == addr))
                ).scalar_one_or_none()
                if not peer and remote_uuid:
                    peer = (
                        await db.execute(
                            select(Peer).filter(Peer.ip == remote_uuid)
                        )
                    ).scalar_one_or_none()
                if not peer and from_id:
                    peer = (
                        await db.execute(select(Peer).filter(Peer.ip == from_id))
                    ).scalar_one_or_none()
                if not peer:
                    primary_id = remote_uuid or from_id or addr
                    db.add(
                        Peer(
                            ip=primary_id,
                            status="pending_in",
                            role="master",
                            display_name=remote_name,
                        )
                    )
                elif peer.status == "trusted" and peer.role in ("worker", "master"):
                    peer.status = "trusted_pending_in"
                    if remote_name:
                        peer.display_name = remote_name
                else:
                    if remote_name:
                        peer.display_name = remote_name
                await db.commit()
            response_payload["status"] = 200
            response_payload["body"] = {"status": "received"}

        elif path == "/peer/callback_remove" and method == "POST":
            if not verify_callback_hmac(body):
                response_payload["status"] = 403
                response_payload["body"] = {"status": "invalid_callback_hmac"}
                await relay_send_to_peer(from_id, response_payload)
                return
            addr = body.get("responder_address", from_ip)
            remote_uuid = body.get("node_uuid", "")
            if remote_uuid and addr:
                register_peer_uuid(remote_uuid, addr)
            async with get_session() as db:
                peer = (
                    await db.execute(select(Peer).filter(Peer.ip == addr))
                ).scalar_one_or_none()
                if not peer and remote_uuid:
                    peer = (
                        await db.execute(
                            select(Peer).filter(Peer.ip == remote_uuid)
                        )
                    ).scalar_one_or_none()
                if not peer and from_id:
                    peer = (
                        await db.execute(select(Peer).filter(Peer.ip == from_id))
                    ).scalar_one_or_none()
                if peer:
                    await db.execute(delete(Peer).where(Peer.ip == peer.ip))
                    await db.commit()
            response_payload["status"] = 200
            response_payload["body"] = {"status": "ok"}

        elif path == "/peer/callback_accept" and method == "POST":
            if not verify_callback_hmac(body):
                response_payload["status"] = 403
                response_payload["body"] = {"status": "invalid_callback_hmac"}
                await relay_send_to_peer(from_id, response_payload)
                return
            addr = body.get("responder_address", from_ip)
            their_token = body.get("auth_token")
            their_signing_key = body.get("signing_key", "")
            remote_uuid = body.get("node_uuid", "")
            if remote_uuid and addr:
                register_peer_uuid(remote_uuid, addr)
            async with get_session() as db:
                peer = (
                    await db.execute(select(Peer).filter(Peer.ip == addr))
                ).scalar_one_or_none()
                if not peer and remote_uuid:
                    peer = (
                        await db.execute(
                            select(Peer).filter(Peer.ip == remote_uuid)
                        )
                    ).scalar_one_or_none()
                if not peer and from_id:
                    peer = (
                        await db.execute(select(Peer).filter(Peer.ip == from_id))
                    ).scalar_one_or_none()
                if peer:
                    peer.their_auth_token = their_token
                    peer.my_auth_token = peer.my_auth_token or str(uuid.uuid4())
                    if their_signing_key:
                        peer.signing_key = their_signing_key
                    elif not peer.signing_key:
                        peer.signing_key = secrets.token_hex(32)
                    if peer.status in (
                        "trusted",
                        "trusted_pending_out",
                    ) and peer.role in ("master", "worker", "dual"):
                        peer.role = "dual"
                        peer.status = "trusted"
                    else:
                        peer.status = "trusted"
                        peer.role = "worker"
                    await db.commit()
                    response_payload["status"] = 200
                    response_payload["body"] = {
                        "status": "ok",
                        "auth_token": peer.my_auth_token,
                        "signing_key": peer.signing_key,
                    }
                else:
                    response_payload["status"] = 404
                    response_payload["body"] = {"error": "Peer not found"}

        elif path == "/peer/callback_reject_dual" and method == "POST":
            if not verify_callback_hmac(body):
                response_payload["status"] = 403
                response_payload["body"] = {"status": "invalid_callback_hmac"}
                await relay_send_to_peer(from_id, response_payload)
                return
            addr = body.get("responder_address", from_ip)
            remote_uuid = body.get("node_uuid", "")
            if remote_uuid and addr:
                register_peer_uuid(remote_uuid, addr)
            async with get_session() as db:
                peer = (
                    await db.execute(select(Peer).filter(Peer.ip == addr))
                ).scalar_one_or_none()
                if not peer and remote_uuid:
                    peer = (
                        await db.execute(
                            select(Peer).filter(Peer.ip == remote_uuid)
                        )
                    ).scalar_one_or_none()
                if not peer and from_id:
                    peer = (
                        await db.execute(select(Peer).filter(Peer.ip == from_id))
                    ).scalar_one_or_none()
                if peer and peer.status == "trusted_pending_out":
                    peer.status = "trusted"
                    await db.commit()
            response_payload["status"] = 200
            response_payload["body"] = {"status": "ok"}

        elif path == "/peer/relay_heartbeat" and method == "POST":
            # Tunnel heartbeat from a cross-region worker
            from nexus.runtime import refresh_worker_task_leases

            stats = body.get("stats", {})
            stats["connection_type"] = "relay"
            hb_identity = stats.get("node_identity", "")
            if hb_identity and from_id != from_ip:
                register_peer_uuid(from_id, hb_identity)
            async with get_session() as db:
                peer = (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_([from_id, from_ip, hb_identity])
                        )
                    )
                ).scalar_one_or_none()
                worker_key = peer.ip if peer else from_id
            async with STATE.worker_state_lock:
                STATE.active_workers[worker_key] = {
                    "stats": stats,
                    "last_seen": time.time(),
                }
            await refresh_worker_task_leases(worker_key)
            response_payload["status"] = 200
            response_payload["body"] = {"status": "ok"}

        elif path == "/peer/pop_task" and method == "GET":
            # Reuse the direct /peer/pop_task logic
            from nexus.api.peer import api_pop_task

            async with get_session() as db:
                peer = (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_([from_id, from_ip]),
                            Peer.status == "trusted",
                        )
                    )
                ).scalar_one_or_none()
            if not peer:
                response_payload["status"] = 403
                response_payload["body"] = {"error": "Untrusted relay peer"}
            else:
                worker_key = peer.ip
                pop_res = await api_pop_task(worker_id=worker_key)
                response_payload["status"] = int(pop_res.status_code or 500)
                if pop_res.status_code == 200:
                    outer_zip = bytes(pop_res.body or b"")
                    payload_bytes = b""
                    if outer_zip:
                        try:
                            with zipfile.ZipFile(io.BytesIO(outer_zip), "r") as out_z:
                                payload_bytes = out_z.read("payload.zip")
                        except Exception:
                            payload_bytes = b""
                    response_payload["body"] = {
                        "task_id": pop_res.headers.get("X-Task-ID", ""),
                        "env": pop_res.headers.get("X-Task-Env", "{}"),
                        "task_sig": pop_res.headers.get("X-Task-Sig", ""),
                        "zip_b64": (
                            base64.b64encode(payload_bytes).decode()
                            if payload_bytes
                            else ""
                        ),
                    }
                elif pop_res.status_code == 202:
                    try:
                        response_payload["body"] = json.loads(
                            (pop_res.body or b"{}").decode("utf-8")
                        )
                    except Exception:
                        response_payload["body"] = {}
                    response_payload["headers"] = {
                        "X-Dispatch-Mode": pop_res.headers.get(
                            "X-Dispatch-Mode", ""
                        ),
                        "X-Task-ID": pop_res.headers.get("X-Task-ID", ""),
                    }
                elif pop_res.status_code == 204:
                    response_payload["body"] = {"status": "no_task"}
                else:
                    try:
                        response_payload["body"] = json.loads(
                            (pop_res.body or b"{}").decode("utf-8")
                        )
                    except Exception:
                        response_payload["body"] = {
                            "error": (
                                f"pop_task failed with status "
                                f"{pop_res.status_code}"
                            )
                        }

        elif path.startswith("/peer/accept_offer/") and method == "POST":
            from nexus.api.peer import api_accept_offer

            task_id = path.split("/peer/accept_offer/")[-1]
            async with get_session() as db:
                peer = (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_([from_id, from_ip]),
                            Peer.status == "trusted",
                        )
                    )
                ).scalar_one_or_none()
            if not peer:
                response_payload["status"] = 403
                response_payload["body"] = {"error": "Untrusted relay peer"}
            else:
                worker_key = peer.ip
                try:
                    accept_res = await api_accept_offer(task_id, worker_id=worker_key)
                except HTTPException as exc:
                    response_payload["status"] = int(exc.status_code)
                    response_payload["body"] = {"error": str(exc.detail)}
                else:
                    response_payload["status"] = int(accept_res.status_code or 500)
                    if accept_res.status_code == 200:
                        outer_zip = bytes(accept_res.body or b"")
                        payload_bytes = b""
                        if outer_zip:
                            try:
                                with zipfile.ZipFile(
                                    io.BytesIO(outer_zip), "r"
                                ) as out_z:
                                    payload_bytes = out_z.read("payload.zip")
                            except Exception:
                                payload_bytes = b""
                        response_payload["body"] = {
                            "task_id": accept_res.headers.get(
                                "X-Task-ID", task_id
                            ),
                            "env": accept_res.headers.get("X-Task-Env", "{}"),
                            "task_sig": accept_res.headers.get(
                                "X-Task-Sig", ""
                            ),
                            "zip_b64": (
                                base64.b64encode(payload_bytes).decode()
                                if payload_bytes
                                else ""
                            ),
                        }
                    elif accept_res.status_code == 204:
                        response_payload["body"] = {"status": "state_conflict"}
                    else:
                        try:
                            response_payload["body"] = json.loads(
                                (accept_res.body or b"{}").decode("utf-8")
                            )
                        except Exception:
                            response_payload["body"] = {
                                "error": (
                                    f"accept_offer failed with status "
                                    f"{accept_res.status_code}"
                                )
                            }

        elif path.startswith("/peer/decline_offer/") and method == "POST":
            from nexus.api.peer import api_decline_offer

            task_id = path.split("/peer/decline_offer/")[-1]
            async with get_session() as db:
                peer = (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_([from_id, from_ip]),
                            Peer.status == "trusted",
                        )
                    )
                ).scalar_one_or_none()
            if not peer:
                response_payload["status"] = 403
                response_payload["body"] = {"error": "Untrusted relay peer"}
            else:
                worker_key = peer.ip
                try:
                    decline_res = await api_decline_offer(
                        task_id, worker_id=worker_key
                    )
                except HTTPException as exc:
                    response_payload["status"] = int(exc.status_code)
                    response_payload["body"] = {"error": str(exc.detail)}
                else:
                    response_payload["status"] = 200
                    response_payload["body"] = (
                        decline_res
                        if isinstance(decline_res, dict)
                        else {"status": "ok"}
                    )

        elif path.startswith("/peer/submit_result/") and method == "POST":
            from nexus.networking.connection_manager import ws_manager
            from nexus.tasks import (
                enqueue_task,
                set_task_status,
                try_schedule_retry,
            )

            task_id = path.split("/peer/submit_result/")[-1]
            status_val = str(body.get("status", "failed")).lower().strip()
            logs_val = body.get("logs", "")
            result_zip_b64 = body.get("result_zip_b64", "")
            result_sig = str(body.get("result_sig", ""))
            elapsed_secs = int(body.get("elapsed_secs") or 0)
            worker_pubkey = str(body.get("worker_pubkey") or "")
            worker_proof = str(body.get("worker_proof") or "")
            file_bytes = (
                base64.b64decode(result_zip_b64) if result_zip_b64 else b""
            )
            async with get_session() as db:
                submitter_peer = (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_([from_id, from_ip]),
                            Peer.status == "trusted",
                        )
                    )
                ).scalar_one_or_none()
            if not submitter_peer:
                response_payload["status"] = 403
                response_payload["body"] = {"error": "Untrusted relay peer"}
                await relay_send_to_peer(from_id, response_payload)
                return
            peer_skey = (
                submitter_peer.signing_key
                if submitter_peer and submitter_peer.signing_key
                else ""
            )
            if not verify_signature(
                result_sig, "result", task_id, file_bytes, status_val, key=peer_skey
            ):
                await write_audit_event(
                    "result_signature_rejected",
                    actor=from_id or "relay-worker",
                    task_id=task_id,
                    severity="warning",
                    details="Invalid relay result signature.",
                )
                response_payload["status"] = 403
                response_payload["body"] = {"error": "Result signature invalid"}
            else:
                async with get_session() as db:
                    task = (
                        await db.execute(
                            select(TaskRecord).filter(TaskRecord.id == task_id)
                        )
                    ).scalar_one_or_none()
                    if task:
                        if task.id in STATE.disrupted_master_tasks:
                            task.logs = (task.logs or "") + (
                                f"{logs_val}\n[{timestamp()}] [MASTER] Late relay "
                                "payload ignored (disrupted).\n"
                            )
                            await db.commit()
                            response_payload["status"] = 200
                            response_payload["body"] = {"status": "ignored"}
                        elif str(task.status or "").lower() in TERMINAL_STATES:
                            response_payload["status"] = 200
                            response_payload["body"] = {"status": "ignored"}
                        else:
                            old_worker = task.worker
                            if status_val == "success" and file_bytes:
                                safe_task_dir = os.path.join(
                                    "completed_tasks",
                                    task_id.replace("..", "")
                                    .replace("/", "_")
                                    .replace("\\", "_"),
                                )
                                os.makedirs(safe_task_dir, exist_ok=True)
                                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                                    safe_extractall(z, safe_task_dir)
                                set_task_status(
                                    task,
                                    "completed",
                                    "Relay worker returned successful payload.",
                                )
                                task.logs = (task.logs or "") + (
                                    f"{logs_val}\n[{timestamp()}] [RELAY] Payload "
                                    f"returned from {from_id}. Status: SUCCESS.\n"
                                )
                                incr_metric("tasks_completed")
                                record_worker_outcome(old_worker, ok=True)
                                try:
                                    from nexus.runtime.usage_receipts import (
                                        issue_compute_receipt,
                                    )
                                    await issue_compute_receipt(
                                        task, elapsed_secs, worker_pubkey, worker_proof
                                    )
                                except Exception:
                                    pass
                            elif status_val == "preempted" and file_bytes:
                                task.checkpoint_payload = file_bytes
                                task.worker = None
                                if try_schedule_retry(
                                    task,
                                    "Worker preempted task via relay.",
                                    old_worker,
                                ):
                                    incr_metric("tasks_preempted")
                                    task.logs = (task.logs or "") + (
                                        f"{logs_val}\n[{timestamp()}] [RELAY] "
                                        "Task preempted & checkpointed.\n"
                                    )
                                else:
                                    set_task_status(
                                        task,
                                        "failed",
                                        "Preempted but retry budget exhausted.",
                                    )
                                    task.logs = (task.logs or "") + (
                                        f"{logs_val}\n[{timestamp()}] [MASTER] "
                                        "Preempted, no retries left.\n"
                                    )
                            else:
                                task.payload = (
                                    file_bytes if file_bytes else task.payload
                                )
                                task.worker = None
                                record_worker_outcome(old_worker, ok=False)
                                retry_applied = try_schedule_retry(
                                    task,
                                    f"Relay worker returned {status_val}.",
                                    old_worker,
                                )
                                task.logs = (task.logs or "") + (
                                    f"{logs_val}\n[{timestamp()}] [RELAY] Result "
                                    f"from {from_id}. Status: "
                                    f"{status_val.upper()}.\n"
                                )
                                if not retry_applied:
                                    set_task_status(
                                        task,
                                        "failed",
                                        f"Relay worker returned {status_val}; "
                                        "retry exhausted.",
                                    )
                                    incr_metric("tasks_failed")
                            task.worker = (
                                None
                                if task.status != "processing"
                                else task.worker
                            )
                            await db.commit()
                            STATE.disrupted_master_tasks.discard(task_id)
                            if task.status == "queued":
                                await enqueue_task(task.id)
                                await ws_manager.broadcast_ping()
                            response_payload["status"] = 200
                            response_payload["body"] = {"status": "ok"}
                    else:
                        response_payload["status"] = 404
                        response_payload["body"] = {"error": "Task not found"}

        elif path == "/peer/group/event" and method == "POST":
            # A group-channel frame routed over the WS relay
            # (the cross-region path when direct HTTP to a member fails).
            from nexus.runtime.group_inbox import dispatch_inbound_frame

            result = await dispatch_inbound_frame(body)
            response_payload["status"] = 200
            response_payload["body"] = result

        elif path == "/peer/group/publish" and method == "POST":
            # A relay-host publish routed over the WS relay.
            # Mirror the /peer/group/publish endpoint's relay:host gate.
            from nexus.runtime.group_inbox import relay_inbound_frame
            from nexus.security.group_keys import get_local_group_pubkey
            from nexus.security.group_permissions import (
                PERM_RELAY_HOST,
                has_permission,
            )

            channel = str(body.get("channel", ""))
            async with get_session() as db:
                is_relay_host = await has_permission(
                    db, channel, get_local_group_pubkey(), PERM_RELAY_HOST
                )
            if not is_relay_host:
                response_payload["status"] = 403
                response_payload["body"] = {
                    "detail": "this node is not a relay host for that channel"
                }
            else:
                result = await relay_inbound_frame(body)
                response_payload["status"] = 200
                response_payload["body"] = result

        elif path in (
            "/peer/group/join_request", "/peer/group/info",
            "/peer/group/roster", "/peer/group/join_decision",
        ) and method == "POST":
            # The bootstrap join handshake routed over the WS
            # relay so a NAT'd founder or joiner can complete a join.
            from fastapi import HTTPException as _HTTPExc
            from nexus.api import group_peer as _gp

            _join_handlers = {
                "/peer/group/join_request": (
                    _gp.peer_group_join_request, _gp.JoinRequestBody,
                ),
                "/peer/group/info": (
                    _gp.peer_group_info, _gp.GroupInfoProbeBody,
                ),
                "/peer/group/roster": (
                    _gp.peer_group_roster, _gp.GroupRosterBody,
                ),
                "/peer/group/join_decision": (
                    _gp.peer_group_join_decision, _gp.JoinDecisionBody,
                ),
            }
            _handler, _model = _join_handlers[path]
            try:
                result = await _handler(_model(**body))
                response_payload["status"] = 200
                response_payload["body"] = result
            except _HTTPExc as exc:
                response_payload["status"] = exc.status_code
                response_payload["body"] = {"detail": exc.detail}
            except Exception as exc:
                response_payload["status"] = 400
                response_payload["body"] = {"detail": str(exc)}

        elif path == "/peer/dm" and method == "POST":
            # A 1:1 direct message routed over the WS
            # relay (cross-region / NAT'd peer path).
            from nexus.api.peer import apply_inbound_dm

            result = await apply_inbound_dm(body)
            response_payload["status"] = 200
            response_payload["body"] = result

        elif path == "/peer/enc_pubkey" and method == "POST":
            # Serve our DM encryption pubkey over the relay.
            from nexus.api.peer import _my_enc_pubkey

            response_payload["status"] = 200
            response_payload["body"] = {"enc_pubkey": _my_enc_pubkey()}

        else:
            response_payload["status"] = 404
            response_payload["body"] = {"error": f"Unknown relay path: {path}"}

    except Exception as e:
        response_payload["body"] = {"error": str(e)}

    await relay_send_to_peer(from_id, response_payload)


# ---------------------------------------------------------------------------
# Secondary relay pool
# ---------------------------------------------------------------------------

# Per-URL reconnect tasks keyed by relay_url. Orchestrator spawns one
# per active GroupRelayBinding URL that isn't the legacy primary, and
# cancels entries that drop out of the desired set.
_POOL_TASKS: dict[str, asyncio.Task] = {}

POOL_RECONCILE_INTERVAL_SEC = 30

# Bound the pool to prevent unbounded RAM growth in users
# with many groups + many relays each (15 connections @ ~275 KB ≈ 4 MB
# steady state, climbs from there). 8 is a generous cap for any
# realistic deployment; cross-region routing only needs 2-3 distinct
# regions covered.
MAX_POOL_SIZE = 8

# Drop pool connections that have seen zero traffic for
# IDLE_DISCONNECT_SEC. After cancelling, the URL stays out of the
# pool for IDLE_RECONNECT_COOLDOWN_SEC so the orchestrator doesn't
# immediately re-spawn it. Trades a little reconnect cost on the
# first cross-region send-after-idle for steady-state RAM in cases
# where many pool URLs sit silent.
IDLE_DISCONNECT_SEC = 600
IDLE_RECONNECT_COOLDOWN_SEC = 300

_LAST_ACTIVITY: dict[str, float] = {}
_IDLE_DISCONNECTED_AT: dict[str, float] = {}

# Cache the encoded heartbeat once instead of re-running
# json.dumps every 10s × N pool connections.
_HEARTBEAT_BYTES = json.dumps({"type": "heartbeat"})


def _touch_relay_activity(relay_url: str) -> None:
    """Mark a pool relay as having recently sent or received traffic."""
    if relay_url:
        _LAST_ACTIVITY[relay_url] = time.time()


async def _pool_connection_loop(relay_url: str) -> None:
    """Maintain one secondary relay WS connection.

    Mirrors ``relay_client_loop`` but for a single, caller-specified
    ``relay_url`` and writes into ``STATE.relay_ws_pool[relay_url]``
    instead of ``STATE.relay_ws``. Uses the same ``_handle_relay_message``
    dispatcher so inbound from any pool relay flows through identical
    handling. Outbound still goes through ``STATE.relay_ws`` for
    W36.C will introduce pool-aware selection.
    """
    from nexus.core.identity import get_node_port

    backoff = 5
    while True:
        grid_key = get_grid_key()
        if not grid_key or not LOCAL_SETTINGS.get("relay_enabled", True):
            await asyncio.sleep(backoff)
            continue

        node_id = get_or_create_node_uuid()
        ws_url = f"{relay_url}/relay/{quote(node_id, safe='')}"
        try:
            async with websockets.connect(ws_url) as ws:
                # Per-context grid_keys also sent on every pool sub.
                local_grid_keys = await assemble_local_grid_keys()
                reg_msg = {
                    "type": "register",
                    "grid_key": grid_key,
                    "grid_keys": local_grid_keys,
                    "display_name": str(
                        LOCAL_SETTINGS.get("user_display_name", "") or ""
                    ),
                    "port": get_node_port(),
                    "capabilities": {},
                    "hide_profile": LOCAL_SETTINGS.get("hide_profile", True),
                }
                await ws.send(json.dumps(reg_msg))
                try:
                    ack_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    ack = json.loads(ack_raw)
                    if ack.get("type") == "error":
                        _log.warning(
                            "[RELAY-POOL] %s rejected registration: %s",
                            relay_url, ack.get("message", ""),
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 120)
                        continue
                    fp = str(ack.get("code_fingerprint", "") or "").strip().lower()
                    if fp:
                        STATE.relay_code_fingerprints[relay_url] = fp
                except asyncio.TimeoutError:
                    _log.warning(
                        "[RELAY-POOL] %s registration timed out", relay_url
                    )
                    continue

                async with STATE.relay_ws_pool_lock:
                    STATE.relay_ws_pool[relay_url] = ws
                backoff = 5
                _touch_relay_activity(relay_url)
                _log.info("[RELAY-POOL] Connected to %s", relay_url)

                async def _hb():
                    while True:
                        try:
                            await ws.send(_HEARTBEAT_BYTES)
                            await asyncio.sleep(10)
                        except Exception:
                            break

                hb = asyncio.create_task(_hb())
                try:
                    while True:
                        raw = await ws.recv()
                        _touch_relay_activity(relay_url)
                        data = json.loads(raw)
                        await _handle_relay_message(data, node_id)
                finally:
                    hb.cancel()
        except Exception as exc:
            _log.debug(
                "[RELAY-POOL] %s disconnected (%s); retry in %ds",
                relay_url, type(exc).__name__, backoff,
            )
        finally:
            async with STATE.relay_ws_pool_lock:
                STATE.relay_ws_pool.pop(relay_url, None)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def _desired_pool_urls() -> set[str]:
    """Set of relay URLs the pool should maintain (excluding the legacy
    primary, which ``relay_client_loop`` owns)."""
    from sqlalchemy import select
    from nexus.storage import get_session
    from nexus.storage.models import GroupRelayBinding

    primary = (get_relay_url() or "").strip()
    urls: set[str] = set()
    try:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(GroupRelayBinding.relay_url).where(
                        GroupRelayBinding.status == "active"
                    )
                )
            ).fetchall()
        for (url,) in rows:
            url = (url or "").strip()
            if url and url != primary:
                urls.add(url)
    except Exception:
        _log.debug("[RELAY-POOL] desired-set query failed", exc_info=True)
    return urls


async def _select_capped_pool_urls() -> set[str]:
    """Bound the pool to ``MAX_POOL_SIZE`` lowest-RTT URLs.

    Excludes URLs that were recently idle-disconnected (still inside
    :data:`IDLE_RECONNECT_COOLDOWN_SEC` cooldown) so a chatty
    orchestrator doesn't churn-reconnect a relay we just decided to
    let go. URLs with no probe data sort last but remain eligible.
    """
    candidates = await _desired_pool_urls()
    now = time.time()
    fresh = [
        url for url in candidates
        if now - _IDLE_DISCONNECTED_AT.get(url, 0) >= IDLE_RECONNECT_COOLDOWN_SEC
    ]
    from nexus.runtime import relay_latency

    def _key(url: str) -> tuple[int, str]:
        rtt = relay_latency.get(url)
        return (rtt if rtt is not None else 10**9, url)

    fresh.sort(key=_key)
    return set(fresh[:MAX_POOL_SIZE])


async def relay_pool_orchestrator() -> None:
    """Reconcile :data:`_POOL_TASKS` with the desired set every 30 s.

    Each pass:

    1. **Idle-disconnect sweep** — cancel any task whose connection
       has seen no traffic for ``IDLE_DISCONNECT_SEC``. The URL goes
       into the ``IDLE_RECONNECT_COOLDOWN_SEC`` cooldown.
    2. **Reconcile to capped target** — spawn tasks for URLs in the
       desired-capped set we don't already have, and cancel tasks
       for URLs that fell out (binding removed, RTT lost a slot,
       cooldown active).
    """
    while True:
        try:
            now = time.time()

            # (1) idle-disconnect sweep
            for url in list(_POOL_TASKS.keys()):
                last = _LAST_ACTIVITY.get(url, now)
                if now - last >= IDLE_DISCONNECT_SEC:
                    task = _POOL_TASKS.pop(url, None)
                    if task is not None and not task.done():
                        task.cancel()
                    async with STATE.relay_ws_pool_lock:
                        STATE.relay_ws_pool.pop(url, None)
                    _IDLE_DISCONNECTED_AT[url] = now
                    _log.info(
                        "[RELAY-POOL] %s idle %ds; disconnected (cooldown %ds)",
                        url, IDLE_DISCONNECT_SEC, IDLE_RECONNECT_COOLDOWN_SEC,
                    )

            # (2) reconcile to capped desired set
            desired = await _select_capped_pool_urls()
            for url in desired - _POOL_TASKS.keys():
                task = asyncio.create_task(
                    _pool_connection_loop(url),
                    name=f"nexus.networking.relay_pool[{url}]",
                )
                _POOL_TASKS[url] = task
                _LAST_ACTIVITY[url] = now
                _log.info("[RELAY-POOL] tracking %s", url)
            for url in list(_POOL_TASKS.keys() - desired):
                task = _POOL_TASKS.pop(url, None)
                if task is not None and not task.done():
                    task.cancel()
                async with STATE.relay_ws_pool_lock:
                    STATE.relay_ws_pool.pop(url, None)
                _log.info("[RELAY-POOL] dropped %s (over cap or excluded)", url)
        except Exception:
            _log.warning("[RELAY-POOL] reconcile failed", exc_info=True)
        await asyncio.sleep(POOL_RECONCILE_INTERVAL_SEC)


__all__ = [
    "set_relay_cli_overrides",
    "get_relay_url",
    "get_grid_key",
    "relay_send",
    "relay_send_to_peer",
    "relay_http_request",
    "relay_http_request_one_shot",
    "relay_client_loop",
    "relay_pool_orchestrator",
    "my_relay_pool_urls",
]
