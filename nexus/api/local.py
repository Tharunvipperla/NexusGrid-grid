"""``/local/*`` management routes (peer + task + settings).

Ported from Phase-1/node_modified.py:

* ``/local/request_peer`` — lines 6178-6232
* ``/local/manage_peer`` — lines 6290-6421
* ``/local/rotate_tokens`` — lines 6538-6580
* ``/local/peers`` (``get_peers``) — lines 6583-6676
* ``/local/database`` wipe — lines 7327-7333
* ``/local/requeue_task/{task_id}`` — lines 7336-7359
* ``/local/disrupt_task/{task_id}`` — lines 7362-7414
* ``/local/cancel_task/{task_id}`` — lines 7417-7446
* ``/local/task/{task_id}`` (delete) — lines 7449-7467
* ``/local/preempt_local_worker_task/{task_id}`` — lines 7470-7495
* ``/local/settings`` — lines 7498-7609
* ``/local/relay_status`` — lines 7612-7621
* ``/local/pending_offers`` — lines 7624-7642
* ``/local/consent_respond/{task_id}`` — lines 7645-7660
* ``/local/shutdown`` — lines 7532-7546 (``graceful_shutdown``, gated by ``verify_local_auth``)

All routes are thin — validation and delegation to the extracted business
layer. Cache-admin, prewarm, and diagnostics routes live in other modules
(Steps 4-5).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile

import httpx
import psutil
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, select, text

from nexus.caches import (
    PREWARM_JOBS,
    detect_uv,
    node_cache_root,
    pip_wheel_cache_dir,
    prewarm_job_set,
    run_prewarm,
    scan_workspace_dependencies,
    venv_cache_root,
)
from nexus.core import (
    LOCAL_SETTINGS,
    STATE,
    TERMINAL_STATES,
    get_node_identity,
    get_or_create_node_uuid,
    resolve_uuid_to_ip,
)
from nexus.api.network_cache import NETWORK_CACHE_TTL, get_cache as _get_network_cache
from nexus.networking.connection_manager import ws_manager
from nexus.networking.discovery import lookup_discovered_peer
from nexus.networking.peer_http import peer_http_post
from nexus.networking.peer_protocol import sign_callback_hmac, sign_join_request
from nexus.networking.relay_client import get_relay_url
from nexus.runtime import (
    get_dispatch_capacity_mb,
    get_local_worker_snapshot,
    image_allowed,
)
from nexus.runtime.service_runner import (
    ServiceManifestError,
    validate_data_sources,
)
from nexus.security import (
    enforce_actual_size,
    enforce_content_length,
    get_max_result_bytes,
    sign_bytes,
    verify_local_auth,
)
from nexus.storage import (
    AuditEvent,
    Peer,
    PresenceEvent,
    TaskRecord,
    get_session,
    save_local_settings_to_db,
)
from nexus.storage.models import DirectMessage
from nexus.tasks import (
    build_task_metadata,
    enqueue_task,
    extract_task_metadata,
    mark_task_interrupted,
    set_task_status,
)
from nexus.telemetry import (
    analyze_cluster_health,
    compute_cluster_rollup,
    incr_metric,
    record_audit_event,
    write_audit_event,
)
from nexus.telemetry.hardware import detect_gpu, get_gpu_stats, sample_net_bandwidth
from nexus.telemetry.logs import task_log_tail
from nexus.ui.broadcaster import broadcast_ui_update
from nexus.utils import (
    MASKED_IP_PLACEHOLDER,
    dir_size_bytes,
    get_local_ip,
    safe_extractall,
    split_csv,
    timestamp,
)

_log = logging.getLogger("nexus.api.local")

router = APIRouter(prefix="/local", tags=["Local Management"])


# ---------------------------------------------------------------------------
# /local/request_peer
# ---------------------------------------------------------------------------

@router.post(
    "/request_peer",
    dependencies=[Depends(verify_local_auth)],
    summary="Send a join request to a peer",
    tags=["Peer Management"],
)
async def local_request_peer(target_ip: str = Form(...)) -> dict:
    """Initiate the join handshake to *target_ip*."""
    my_address = get_node_identity()
    # Self-ping guard
    from nexus.core.identity import get_node_port

    port = get_node_port()
    if target_ip in (
        my_address,
        f"127.0.0.1:{port}",
        f"localhost:{port}",
    ):
        return {"status": "error", "message": "Cannot pair with yourself."}

    # Prefer UUID as the primary identifier when we already know it.
    from nexus.core.identity import resolve_ip_to_uuid

    target_uuid = resolve_ip_to_uuid(target_ip)
    primary_target = target_uuid if target_uuid != target_ip else target_ip

    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == primary_target))
        ).scalar_one_or_none()

        # Fallback: saved under raw IP in an earlier version.
        if not peer and primary_target != target_ip:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == target_ip))
            ).scalar_one_or_none()
            if peer:
                primary_target = target_ip

        if not peer:
            new_peer = Peer(ip=primary_target, status="pending_out", role="worker")
            if primary_target != target_ip:
                new_peer.resolved_ip = target_ip
            db.add(new_peer)
        elif peer.role == "master" and peer.status == "trusted":
            peer.status = "trusted_pending_out"
            if primary_target != target_ip and not peer.resolved_ip:
                peer.resolved_ip = target_ip
        else:
            return {"status": "error", "message": "Already pending or trusted."}
        await db.commit()

    my_uuid = get_or_create_node_uuid()
    from nexus.security.tls import get_local_fingerprint
    try:
        my_fp = get_local_fingerprint()
    except Exception:
        my_fp = ""
    # Advertise our current relay-pool URL set so the peer can
    # pick a shared relay when routing back to us. Best-effort; failure
    # leaves it empty and routing falls back to pool-wide.
    try:
        from nexus.networking.relay_client import my_relay_pool_urls

        my_relay_urls = await my_relay_pool_urls()
    except Exception:
        my_relay_urls = []
    result = await peer_http_post(
        primary_target,
        "/peer/request_join",
        {
            "requester_address": my_address,
            "display_name": str(LOCAL_SETTINGS.get("user_display_name", "") or ""),
            "node_uuid": my_uuid,
            "cert_fingerprint": my_fp,
            "join_hmac": sign_join_request(my_uuid, my_address),
            "relay_urls": my_relay_urls,
        },
    )

    if result["status"] == 200:
        body_resp = result.get("body") or {}
        their_fp = (
            str(body_resp.get("cert_fingerprint", "") or "")
            .strip()
            .lower()
        )
        # Peer advertised back their relay-pool URL set.
        their_relay_urls = body_resp.get("relay_urls") or []
        if not isinstance(their_relay_urls, list):
            their_relay_urls = []
        their_relay_urls = [str(u) for u in their_relay_urls if isinstance(u, str)]
        import json as _json
        relay_urls_blob = _json.dumps(sorted(set(their_relay_urls)))
        async with get_session() as db:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == primary_target))
            ).scalar_one_or_none()
            if peer:
                if their_fp:
                    peer.cert_fingerprint = their_fp
                peer.peer_relay_urls = relay_urls_blob
                await db.commit()
        await broadcast_ui_update({"type": "state_changed"})
        return {
            "status": "ok",
            "via": "relay" if result.get("body", {}).get("via") else "direct",
        }

    # Roll back local row on failure.
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == primary_target))
        ).scalar_one_or_none()
        if peer:
            if peer.status == "pending_out":
                await db.execute(delete(Peer).where(Peer.ip == primary_target))
            elif peer.status == "trusted_pending_out":
                peer.status = "trusted"
        await db.commit()
    await broadcast_ui_update({"type": "state_changed"})
    return {"status": "error", "message": "Target unreachable or blocked."}


# ---------------------------------------------------------------------------
# /local/manage_peer
# ---------------------------------------------------------------------------

@router.post(
    "/manage_peer",
    dependencies=[Depends(verify_local_auth)],
    summary="Accept, reject, revoke, or modify a peer connection",
    tags=["Peer Management"],
)
async def local_manage_peer(
    ip: str = Form(...), action: str = Form(...)
) -> dict:
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == ip))
        ).scalar_one_or_none()
        if not peer:
            return {"status": "error", "message": "Peer not found"}
        my_address = get_node_identity()
        my_uuid = get_or_create_node_uuid()

        callback_sig = sign_callback_hmac(my_uuid, my_address)

        if action in ("reject", "cancel", "revoke"):
            await db.execute(delete(Peer).where(Peer.ip == ip))
            await db.commit()
            await peer_http_post(
                ip,
                "/peer/callback_remove",
                {
                    "responder_address": my_address,
                    "node_uuid": my_uuid,
                    "callback_hmac": callback_sig,
                },
                timeout=3.0,
            )
            await broadcast_ui_update({"type": "state_changed"})
            return {"status": "deleted"}

        if action == "reject_dual":
            peer.status = "trusted"
            await db.commit()
            await peer_http_post(
                ip,
                "/peer/callback_reject_dual",
                {
                    "responder_address": my_address,
                    "node_uuid": my_uuid,
                    "callback_hmac": callback_sig,
                },
            )
            await broadcast_ui_update({"type": "state_changed"})
            return {"status": "ok"}

        if action == "accept_dual" and peer.status == "trusted_pending_in":
            peer.role = "dual"
            peer.my_auth_token = peer.my_auth_token or str(uuid.uuid4())
            peer.signing_key = peer.signing_key or secrets.token_hex(32)
            from nexus.security.tls import get_local_fingerprint
            try:
                my_fp = get_local_fingerprint()
            except Exception:
                my_fp = ""
            result = await peer_http_post(
                ip,
                "/peer/callback_accept",
                {
                    "responder_address": my_address,
                    "node_uuid": my_uuid,
                    "auth_token": peer.my_auth_token,
                    "type": "initial",
                    "signing_key": peer.signing_key,
                    "cert_fingerprint": my_fp,
                    "callback_hmac": callback_sig,
                },
                skip_pin=True,
            )
            if result["status"] == 200:
                resp = result["body"]
                peer.their_auth_token = resp.get("auth_token") or peer.their_auth_token
                peer.signing_key = resp.get("signing_key") or peer.signing_key
                their_fp = str(resp.get("cert_fingerprint", "") or "").strip().lower()
                if their_fp:
                    peer.cert_fingerprint = their_fp
            peer.status = "trusted"
            await db.commit()
            await broadcast_ui_update({"type": "state_changed"})
            return {"status": "ok"}

        if action in ("accept", "accept_dual"):
            peer.my_auth_token = str(uuid.uuid4())
            peer.signing_key = peer.signing_key or secrets.token_hex(32)
            from nexus.security.tls import get_local_fingerprint
            try:
                my_fp = get_local_fingerprint()
            except Exception:
                my_fp = ""
            result = await peer_http_post(
                ip,
                "/peer/callback_accept",
                {
                    "responder_address": my_address,
                    "node_uuid": my_uuid,
                    "auth_token": peer.my_auth_token,
                    "type": "initial",
                    "signing_key": peer.signing_key,
                    "cert_fingerprint": my_fp,
                    "callback_hmac": callback_sig,
                },
                skip_pin=True,
            )
            if result["status"] == 200:
                resp = result["body"]
                peer.status = "trusted"
                # User-locked behavior: accept_dual must wait for the
                # other side's explicit accept before flipping to ``dual``.
                # Until then we record ``master`` (we accepted them as
                # someone who can dispatch to us) and let the reciprocal
                # ``/peer/callback_accept`` promote the row to ``dual``
                # via the upgrade branch in ``peer_callback_accept``.
                peer.role = "master"
                peer.their_auth_token = resp.get("auth_token")
                peer.signing_key = resp.get("signing_key") or peer.signing_key
                their_fp = str(resp.get("cert_fingerprint", "") or "").strip().lower()
                if their_fp:
                    peer.cert_fingerprint = their_fp
                await db.commit()
                if action == "accept_dual":
                    # Send the dual-reverse request: B sees a pending row
                    # for us in their UI and must explicitly accept before
                    # the relationship promotes to dual on either side.
                    await peer_http_post(
                        ip,
                        "/peer/request_join",
                        {
                            "requester_address": my_address,
                            "node_uuid": my_uuid,
                            "cert_fingerprint": my_fp,
                            "join_hmac": sign_join_request(my_uuid, my_address),
                        },
                    )
                await broadcast_ui_update({"type": "state_changed"})
                return {"status": "ok"}
            return {
                "status": "error",
                "message": f"Callback failed (status {result['status']})",
            }

        if (
            action == "request_dual"
            and peer.status == "trusted"
            and peer.role != "dual"
        ):
            from nexus.security.tls import get_local_fingerprint
            try:
                my_fp = get_local_fingerprint()
            except Exception:
                my_fp = ""
            result = await peer_http_post(
                ip,
                "/peer/request_join",
                {
                    "requester_address": my_address,
                    "node_uuid": my_uuid,
                    "cert_fingerprint": my_fp,
                    "join_hmac": sign_join_request(my_uuid, my_address),
                },
            )
            if result["status"] == 200:
                peer.status = "trusted_pending_out"
                await db.commit()
                await broadcast_ui_update({"type": "state_changed"})
                return {"status": "ok"}
            return {
                "status": "error",
                "message": f"Dual request failed (status {result['status']})",
            }

        if action == "pause":
            # Stop outbound heartbeats / RPC to this peer.
            # peer_http_post short-circuits to 503 once `paused` is set,
            # so the peer's roster eventually times out and they see us
            # as offline. Local-only flag; not pushed to the peer.
            peer.paused = 1
            await db.commit()
            await broadcast_ui_update({"type": "state_changed"})
            return {"status": "paused"}

        if action == "resume":
            peer.paused = 0
            await db.commit()
            await broadcast_ui_update({"type": "state_changed"})
            return {"status": "resumed"}

    return {"status": "error", "message": "Unknown action."}


# ---------------------------------------------------------------------------
# /local/rotate_tokens
# ---------------------------------------------------------------------------

@router.post(
    "/rotate_tokens",
    dependencies=[Depends(verify_local_auth)],
    summary="Rotate all trusted peer auth tokens",
    tags=["Peer Management"],
)
async def local_rotate_tokens() -> dict:
    """Mint a fresh ``my_auth_token`` for every trusted peer and push it."""
    my_address = get_node_identity()
    rotated, failed = 0, []
    async with get_session() as db:
        peers = (
            (await db.execute(select(Peer).filter(Peer.status == "trusted")))
            .scalars()
            .all()
        )
        for peer in peers:
            if not peer.their_auth_token:
                continue
            new_token = str(uuid.uuid4())
            headers = {
                "X-Cluster-Key": str(peer.their_auth_token),
                "X-Node-Address": my_address,
            }
            try:
                async with httpx.AsyncClient(headers=headers) as client_http:
                    res = await client_http.post(
                        f"http://{peer.ip}/peer/callback_rotate_token",
                        json={
                            "responder_address": my_address,
                            "new_auth_token": new_token,
                        },
                        timeout=5.0,
                    )
                if res.status_code == 200:
                    peer.my_auth_token = new_token
                    rotated += 1
                else:
                    failed.append(peer.ip)
            except Exception:
                failed.append(peer.ip)
        await db.commit()
    await write_audit_event(
        "token_rotate_batch",
        actor=my_address,
        details=f"rotated={rotated}, failed={failed}",
    )
    return {"status": "ok", "rotated": rotated, "failed": failed}


# ---------------------------------------------------------------------------
# /local/peers
# ---------------------------------------------------------------------------

@router.get(
    "/peers",
    dependencies=[Depends(verify_local_auth)],
    summary="List all peers with discovery data and fitness scores",
    tags=["Peer Management"],
)
async def get_peers() -> dict:
    """Return DB-registered peers + LAN-discovered beacons with fitness scores."""
    from nexus.core.identity import resolve_ip_to_uuid as _resolve_ip_to_uuid

    blocked = set(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])

    async with get_session() as db:
        peers = (await db.execute(select(Peer))).scalars().all()

    discovered = []
    for peer_uuid, entry in STATE.discovered_peers.items():
        if peer_uuid in blocked:
            continue
        if isinstance(entry, tuple):
            ts = entry[0]
            dname = entry[1] if len(entry) > 1 else ""
            source = entry[2] if len(entry) > 2 else "lan"
            stats = entry[3] if len(entry) > 3 else {}
            real_ip = entry[5] if len(entry) > 5 else ""
        else:
            ts, dname, source, stats, real_ip = entry, "", "lan", {}, ""
        ip = real_ip or peer_uuid
        score = 0
        score_parts: dict = {}
        if stats:
            ram_free = stats.get("ram_free_mb", 0)
            ram_total = stats.get("ram_total_mb", 1)
            cpu_pct = stats.get("cpu_pct", 100)
            cpu_cores = stats.get("cpu_cores", 1)
            ram_score = min(40, int((ram_free / max(ram_total, 1)) * 40))
            cpu_score = max(0, int((1 - cpu_pct / 100) * 30))
            core_score = min(15, cpu_cores * 2)
            gpu_score = 15 if stats.get("gpu") else 0
            score = ram_score + cpu_score + core_score + gpu_score
            score_parts = {
                "ram_free_mb": ram_free,
                "ram_total_mb": ram_total,
                "cpu_pct": round(cpu_pct, 1),
                "cpu_cores": cpu_cores,
                "gpu": bool(stats.get("gpu")),
                "gpu_name": stats.get("gpu_name", ""),
                "vram_free_mb": stats.get("vram_free_mb", 0),
                "vram_total_mb": stats.get("vram_total_mb", 0),
            }
        hide_flag = entry[4] if isinstance(entry, tuple) and len(entry) > 4 else False
        discovered.append(
            {
                "ip": MASKED_IP_PLACEHOLDER if hide_flag else ip,
                "internal_ip": ip,
                "peer_uuid": peer_uuid,
                "last_seen": int(time.time() - ts),
                "display_name": dname,
                "source": source,
                "score": score,
                "stats": score_parts or {},
                "hide_profile": hide_flag,
            }
        )

    peer_list = []
    for p in peers:
        if p.ip in blocked:
            continue
        # Priority: beacon > active WS heartbeat > DB display_name
        dname = str(p.display_name or "")
        w_data = STATE.active_workers.get(p.ip, {})
        if w_data:
            live_name = str(w_data.get("stats", {}).get("user_display_name", "") or "")
            if live_name:
                dname = live_name
        peer_hidden = False
        _disc_uuid, _disc_entry = lookup_discovered_peer(p.ip)
        if _disc_entry:
            disc_dname = _disc_entry[1] if len(_disc_entry) > 1 else ""
            if disc_dname:
                dname = str(disc_dname)
            if len(_disc_entry) > 4:
                peer_hidden = _disc_entry[4]
        _resolved = resolve_uuid_to_ip(p.ip)
        # Discovery beacons can carry either the LAN IP or the peer's UUID
        # depending on whether the beacon arrived via LAN or relay; without
        # a stable peer_uuid the UI's "is this row already connected?" check
        # flaps as the beacon source rotates. Resolve to the UUID once here
        # so the UI can match by identity, not by transport-dependent IP.
        _peer_uuid = _resolve_ip_to_uuid(p.ip)
        live_bench = (
            float(w_data.get("stats", {}).get("bench", 0.0) or 0.0)
            if w_data
            else 0.0
        )
        peer_list.append(
            {
                "ip": MASKED_IP_PLACEHOLDER if peer_hidden else p.ip,
                "internal_ip": p.ip,
                "resolved_ip": _resolved if _resolved != p.ip else "",
                "peer_uuid": _peer_uuid,
                "status": p.status,
                "role": p.role,
                "display_name": dname,
                "hide_profile": peer_hidden,
                "benchmark_score": live_bench or float(p.benchmark_score or 0.0),
                "benchmark_at": str(p.benchmark_at or ""),
                # Local pause flag.
                "paused": bool(getattr(p, "paused", 0) or 0),
            }
        )

    from nexus.core.identity import get_node_port

    return {
        "my_identity": {
            "ip": get_local_ip(),
            "port": get_node_port(),
            "benchmark_score": float(LOCAL_SETTINGS.get("benchmark_score", 0.0) or 0.0),
            "benchmark_at": str(LOCAL_SETTINGS.get("benchmark_at", "") or ""),
        },
        "peers": peer_list,
        "discovered_lan": discovered,
    }


# ---------------------------------------------------------------------------
# Batch C: peer block / unblock
# ---------------------------------------------------------------------------

@router.get(
    "/peers/blocked",
    dependencies=[Depends(verify_local_auth)],
    summary="List peer UUIDs the user has blocked",
    tags=["Peer Management"],
)
async def get_blocked_peers() -> dict:
    blocked = list(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])
    return {"blocked": blocked, "count": len(blocked)}


@router.post(
    "/peers/block/{peer_uuid}",
    dependencies=[Depends(verify_local_auth)],
    summary="Block a peer: hide from peer list and reject all task/deposit traffic",
    tags=["Peer Management"],
)
async def block_peer(peer_uuid: str) -> dict:
    from nexus.storage.repositories import save_local_settings_to_db
    from nexus.telemetry.audit import record_audit_event

    peer_uuid = peer_uuid.strip()
    if not peer_uuid:
        raise HTTPException(400, "peer_uuid required")
    current = list(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])
    if peer_uuid not in current:
        current.append(peer_uuid)
        LOCAL_SETTINGS["blocked_peer_uuids"] = current
        await save_local_settings_to_db()
        await record_audit_event(
            "peer.blocked",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=peer_uuid,
        )
    return {"status": "ok", "blocked_count": len(current)}


@router.post(
    "/peers/unblock/{peer_uuid}",
    dependencies=[Depends(verify_local_auth)],
    summary="Unblock a previously-blocked peer",
    tags=["Peer Management"],
)
async def unblock_peer(peer_uuid: str) -> dict:
    from nexus.storage.repositories import save_local_settings_to_db
    from nexus.telemetry.audit import record_audit_event

    peer_uuid = peer_uuid.strip()
    current = list(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])
    if peer_uuid in current:
        current = [u for u in current if u != peer_uuid]
        LOCAL_SETTINGS["blocked_peer_uuids"] = current
        await save_local_settings_to_db()
        await record_audit_event(
            "peer.unblocked",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=peer_uuid,
        )
    return {"status": "ok", "blocked_count": len(current)}


# ---------------------------------------------------------------------------
# 1:1 direct messages with paired peers
# ---------------------------------------------------------------------------


def _dm_summary(m: DirectMessage) -> dict:
    return {
        "msg_id": m.msg_id,
        "direction": m.direction or "out",
        "sender_name": m.sender_name or "",
        "body": "" if m.deleted else (m.body or ""),
        "sent_at": m.sent_at or "",
        "deleted": bool(m.deleted),
        # Outbound delivery state for the ✓/⏳ indicator.
        "delivered": bool(m.delivered),
        "reply_to": m.reply_to or "",
        "reply_snippet": m.reply_snippet or "",
        "reply_sender": m.reply_sender or "",
        "attach_kind": m.attach_kind or "",
        "attach_name": m.attach_name or "",
        "attach_mime": m.attach_mime or "",
        "attach_size": int(m.attach_size or 0),
    }


async def _get_or_fetch_peer_enc_pub(target_ip: str) -> str:
    """Return the peer's cached X25519 pubkey, fetching + caching if absent."""
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).where(Peer.ip == target_ip))
        ).scalar_one_or_none()
        if peer is not None and (peer.peer_enc_pub or ""):
            return peer.peer_enc_pub
    try:
        res = await peer_http_post(target_ip, "/peer/enc_pubkey", {})
        pub = str((res or {}).get("enc_pubkey") or "")
    except Exception:
        pub = ""
    if pub:
        async with get_session() as db:
            peer = (
                await db.execute(select(Peer).where(Peer.ip == target_ip))
            ).scalar_one_or_none()
            if peer is not None:
                peer.peer_enc_pub = pub
                await db.commit()
    return pub


async def _resolve_dm_target(peer_uuid: str) -> str:
    """Resolve a peer's current address: the live UUID→IP map first, then the
    group roster address for a co-member who isn't a paired peer."""
    target_ip = resolve_uuid_to_ip(peer_uuid)
    if not target_ip or target_ip == peer_uuid:
        from nexus.storage.models import GroupMember
        async with get_session() as db:
            addr = (
                await db.execute(
                    select(GroupMember.peer_address).where(
                        (GroupMember.node_id == peer_uuid)
                        & (GroupMember.peer_address != "")
                    ).limit(1)
                )
            ).scalar_one_or_none()
        target_ip = addr or peer_uuid
    return target_ip


async def _deliver_dm(
    target_ip: str, msg_id: str, text: str, sent_at: str, my_name: str,
    reply: dict | None = None, attach: dict | None = None,
) -> bool:
    """Seal (E2E) + deliver a DM. Returns True on delivery, False if the peer
    was unreachable (the outbox loop will retry). Marks the row delivered."""
    import base64

    payload = {
        "msg_id": msg_id,
        "sent_at": sent_at,
        "from_uuid": get_or_create_node_uuid(),
        "from_name": my_name,
    }
    # Security F-007: sign the message identity + content so the recipient can
    # verify it really came from us (binds to our group pubkey, not the UUID).
    from nexus.security.group_keys import get_local_group_privkey
    from nexus.security.usage_receipt import (
        STMT_DM,
        dm_statement_payload,
        sign_statement,
    )
    payload["sig"] = sign_statement(
        STMT_DM,
        dm_statement_payload(msg_id, payload["from_uuid"], sent_at, text),
        get_local_group_privkey(),
    )
    if reply:
        payload.update(reply)
    # Attachment metadata rides cleartext; the file bytes are sealed below.
    if attach and attach.get("attach_kind"):
        payload["attach_kind"] = attach["attach_kind"]
        payload["attach_name"] = attach.get("attach_name") or ""
        payload["attach_mime"] = attach.get("attach_mime") or ""
        payload["attach_size"] = attach.get("attach_size") or 0
    enc_pub = await _get_or_fetch_peer_enc_pub(target_ip)
    if enc_pub:
        try:
            from nexus.security.group_ecies import ecies_seal

            payload["enc"] = base64.b64encode(
                ecies_seal(text.encode("utf-8"), enc_pub)
            ).decode("ascii")
            if attach and attach.get("attach_data"):
                payload["enc_attach"] = base64.b64encode(
                    ecies_seal(attach["attach_data"].encode("utf-8"), enc_pub)
                ).decode("ascii")
        except Exception:
            payload["body"] = text  # seal failed — legacy fallback
    else:
        # No key (old peer / unreachable) — plaintext over the existing
        # transport (TLS on the direct path).
        payload["body"] = text
        if attach and attach.get("attach_data"):
            payload["attach_data"] = attach["attach_data"]
    try:
        await peer_http_post(target_ip, "/peer/dm", payload)
    except Exception:
        _log.debug("DM delivery failed for %s", msg_id, exc_info=True)
        return False
    # Delivered — clear it from the outbox so the retry loop stops.
    try:
        async with get_session() as db:
            row = await db.get(DirectMessage, msg_id)
            if row is not None and not int(row.delivered or 0):
                row.delivered = 1
                await db.commit()
    except Exception:
        _log.debug("mark-delivered failed for %s", msg_id, exc_info=True)
    return True


@router.post(
    "/peers/{peer_uuid}/dm",
    dependencies=[Depends(verify_local_auth)],
    summary="Send a 1:1 direct message to a paired peer",
    tags=["Peer Management"],
)
async def send_direct_message(peer_uuid: str, payload: dict) -> dict:
    import base64 as _b64

    from nexus.utils.time import iso_now

    peer_uuid = (peer_uuid or "").strip()
    text = str(payload.get("body") or "").strip()
    attach_data = str(payload.get("attach_data") or "")
    attach_kind = ""
    attach_size = 0
    _foreign_raw = None
    if attach_data:
        try:
            raw = _b64.b64decode(attach_data)
        except Exception:
            raise HTTPException(400, "invalid attachment encoding")
        attach_size = len(raw)
        if attach_size > 5 * 1024 * 1024:
            # Sender-hosted — keep bytes on disk, recipient pulls.
            from nexus.runtime.chat_attachments import MAX_ATTACH_BYTES
            if attach_size > MAX_ATTACH_BYTES:
                raise HTTPException(413, "attachment too large (max 100MB)")
            attach_kind = "foreign"
            _foreign_raw = raw
            attach_data = ""  # don't store/transmit the bytes inline
        else:
            attach_kind = "inline"
    if not peer_uuid or (not text and not attach_data):
        raise HTTPException(400, "peer_uuid and a message or attachment required")
    if len(text) > 4000:
        raise HTTPException(400, "message too long (max 4000)")
    target_ip = await _resolve_dm_target(peer_uuid)
    msg_id = uuid.uuid4().hex
    if attach_kind == "foreign":
        from nexus.runtime.chat_attachments import store_blob
        store_blob(msg_id, _foreign_raw)
    sent_at = iso_now()
    my_name = str(LOCAL_SETTINGS.get("user_display_name") or "")
    reply = {
        "reply_to": str(payload.get("reply_to") or ""),
        "reply_snippet": str(payload.get("reply_snippet") or ""),
        "reply_sender": str(payload.get("reply_sender") or ""),
    }
    attach = {
        "attach_kind": attach_kind,
        "attach_name": str(payload.get("attach_name") or ""),
        "attach_mime": str(payload.get("attach_mime") or ""),
        "attach_size": attach_size,
        "attach_data": attach_data,
    }
    async with get_session() as db:
        db.add(DirectMessage(
            msg_id=msg_id,
            peer_uuid=peer_uuid,
            direction="out",
            sender_name=my_name,
            body=text,
            sent_at=sent_at,
            received_at=sent_at,
            reply_to=reply["reply_to"],
            reply_snippet=reply["reply_snippet"],
            reply_sender=reply["reply_sender"],
            attach_kind=attach["attach_kind"],
            attach_name=attach["attach_name"],
            attach_mime=attach["attach_mime"],
            attach_size=attach["attach_size"],
            attach_data=attach["attach_data"],
        ))
        await db.commit()
    # Deliver in the background so the UI returns instantly (seal + post can
    # take a moment on the relay fallback path).
    asyncio.create_task(
        _deliver_dm(target_ip, msg_id, text, sent_at, my_name, reply, attach)
    )
    return {"msg_id": msg_id, "sent_at": sent_at}


@router.get(
    "/peers/{peer_uuid}/dm",
    dependencies=[Depends(verify_local_auth)],
    summary="List the DM thread with a peer",
    tags=["Peer Management"],
)
async def list_direct_messages(peer_uuid: str, limit: int = 200) -> dict:
    peer_uuid = (peer_uuid or "").strip()
    limit = max(1, min(int(limit or 200), 500))
    async with get_session() as db:
        rows = (
            await db.execute(
                select(DirectMessage)
                .where(DirectMessage.peer_uuid == peer_uuid)
                .order_by(DirectMessage.sent_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    rows = list(reversed(rows))
    return {"peer_uuid": peer_uuid, "messages": [_dm_summary(m) for m in rows]}


@router.get(
    "/dm/summary",
    dependencies=[Depends(verify_local_auth)],
    summary="Per-peer inbound DM counters for unread badges",
    tags=["Peer Management"],
)
async def dm_summary() -> dict:
    """One row per conversation: inbound count + newest inbound timestamp.

    Lets the UI compute unread badges with a single query no matter how
    many peers exist — it only refetches a thread when a conversation's
    counters actually moved.
    """
    from sqlalchemy import func as _func

    async with get_session() as db:
        rows = (
            await db.execute(
                select(
                    DirectMessage.peer_uuid,
                    _func.count(),
                    _func.max(DirectMessage.sent_at),
                )
                .where(
                    DirectMessage.direction == "in",
                    DirectMessage.deleted == 0,
                )
                .group_by(DirectMessage.peer_uuid)
            )
        ).all()
    return {
        "peers": [
            {"peer_uuid": r[0], "in_count": int(r[1] or 0), "last_in_at": r[2] or ""}
            for r in rows
        ]
    }


@router.get(
    "/messages/storage",
    dependencies=[Depends(verify_local_auth)],
    summary="Message storage usage (group chat + DMs) for diagnostics",
    tags=["Peer Management"],
)
async def messages_storage() -> dict:
    from sqlalchemy import func as _func

    from nexus.storage.models import GroupMessage

    async with get_session() as db:
        gm = (
            await db.execute(
                select(
                    _func.count(),
                    _func.coalesce(_func.sum(_func.length(GroupMessage.body)), 0),
                ).select_from(GroupMessage)
            )
        ).first()
        dm = (
            await db.execute(
                select(
                    _func.count(),
                    _func.coalesce(_func.sum(_func.length(DirectMessage.body)), 0),
                ).select_from(DirectMessage)
            )
        ).first()
    return {
        "group_messages": {"count": int(gm[0] or 0), "bytes": int(gm[1] or 0)},
        "direct_messages": {"count": int(dm[0] or 0), "bytes": int(dm[1] or 0)},
    }


@router.delete(
    "/messages/all",
    dependencies=[Depends(verify_local_auth)],
    summary="Purge ALL stored messages (group chat + DMs) to free space",
    tags=["Peer Management"],
)
async def clear_all_messages() -> dict:
    from sqlalchemy import delete as _delete

    from nexus.storage.models import GroupMessage

    async with get_session() as db:
        g = await db.execute(_delete(GroupMessage))
        d = await db.execute(_delete(DirectMessage))
        await db.commit()
    return {
        "group_deleted": int(g.rowcount or 0),
        "dm_deleted": int(d.rowcount or 0),
    }


@router.get(
    "/peers/{peer_uuid}/dm/{msg_id}/attachment",
    dependencies=[Depends(verify_local_auth)],
    summary="Download a DM's inline attachment",
    tags=["Peer Management"],
)
async def get_dm_attachment(peer_uuid: str, msg_id: str):
    import base64 as _b64

    from fastapi import Response

    async with get_session() as db:
        m = await db.get(DirectMessage, msg_id)
        if m is None:
            raise HTTPException(404, "attachment not found")
        if (m.attach_kind or "") == "foreign":
            from nexus.runtime.chat_attachments import load_blob
            raw = load_blob(msg_id)
            if raw is None:
                raise HTTPException(425, "still downloading")
        elif m.attach_data or "":
            raw = _b64.b64decode(m.attach_data)
        else:
            raise HTTPException(404, "attachment not found")
    return Response(
        content=raw,
        media_type=m.attach_mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{(m.attach_name or "file")}"'},
    )


@router.get(
    "/dm/threads",
    dependencies=[Depends(verify_local_auth)],
    summary="List all DM conversations with counts (for the Messages hub)",
    tags=["Peer Management"],
)
async def list_dm_threads() -> dict:
    from sqlalchemy import func as _func

    async with get_session() as db:
        rows = (
            await db.execute(
                select(
                    DirectMessage.peer_uuid,
                    _func.count().label("n"),
                    _func.max(DirectMessage.sent_at).label("last_at"),
                    _func.max(DirectMessage.sender_name).label("name"),
                ).group_by(DirectMessage.peer_uuid)
            )
        ).all()
    threads = [
        {
            "peer_uuid": r[0],
            "count": int(r[1] or 0),
            "last_at": r[2] or "",
            "sender_name": r[3] or "",
        }
        for r in rows
    ]
    threads.sort(key=lambda t: t["last_at"], reverse=True)
    return {"threads": threads}


@router.delete(
    "/peers/{peer_uuid}/dm",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete an entire DM conversation (frees disk space)",
    tags=["Peer Management"],
)
async def delete_dm_conversation(peer_uuid: str) -> dict:
    from sqlalchemy import delete as _delete

    async with get_session() as db:
        res = await db.execute(
            _delete(DirectMessage).where(DirectMessage.peer_uuid == peer_uuid)
        )
        await db.commit()
    return {"peer_uuid": peer_uuid, "deleted": int(res.rowcount or 0)}


@router.delete(
    "/peers/{peer_uuid}/dm/{msg_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a DM from your local thread",
    tags=["Peer Management"],
)
async def delete_direct_message(peer_uuid: str, msg_id: str) -> dict:
    async with get_session() as db:
        row = await db.get(DirectMessage, msg_id)
        if row is None:
            raise HTTPException(404, "message not found")
        row.deleted = 1
        row.body = ""
        await db.commit()
    return {"msg_id": msg_id, "deleted": True}


@router.get(
    "/peers/{peer_uuid}/resource_exchange",
    dependencies=[Depends(verify_local_auth)],
    summary="Verified compute/storage exchanged with one peer",
    tags=["Peer Management"],
)
async def peer_resource_exchange(peer_uuid: str) -> dict:
    """Sum the counterparty-signed receipts between this node and
    *peer_uuid* so a friend can see "did they let me use their resources."
    Numbers are recomputed from verified receipts — neither side can edit them.
    """
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage.models import GroupMember, UsageReceipt

    me = get_local_group_pubkey()
    async with get_session() as db:
        their_pub = (await db.execute(
            select(GroupMember.pubkey).where(
                (GroupMember.node_id == peer_uuid) & (GroupMember.pubkey != "")
            ).limit(1)
        )).scalar_one_or_none()
        if not their_pub:
            return {"known": False}
        pair = {me, their_pub}
        rows = (await db.execute(
            select(UsageReceipt).where(
                UsageReceipt.provider_pubkey.in_(pair)
                & UsageReceipt.consumer_pubkey.in_(pair)
            )
        )).scalars().all()
    out = {"compute_secs": 0, "compute_used": 0, "storage_hosted": 0, "storage_used": 0}
    for r in rows:
        if r.provider_pubkey not in pair or r.consumer_pubkey not in pair:
            continue
        amt = int(r.amount or 0)
        if r.kind == "compute":
            out["compute_secs" if r.provider_pubkey == me else "compute_used"] += amt
        elif r.kind == "storage":
            out["storage_hosted" if r.provider_pubkey == me else "storage_used"] += amt
    return {"known": True, **out}


# ---------------------------------------------------------------------------
# /local/profile (node profiles)
# ---------------------------------------------------------------------------


@router.get(
    "/profile",
    dependencies=[Depends(verify_local_auth)],
    summary="This node's own profile (for editing)",
    tags=["Profile"],
)
async def get_my_profile() -> dict:
    from nexus.runtime.usage_receipts import global_usage_summary

    return {
        "display_name": str(LOCAL_SETTINGS.get("user_display_name", "") or ""),
        "about_me": str(LOCAL_SETTINGS.get("about_me", "") or ""),
        "hosted_services": list(LOCAL_SETTINGS.get("hosted_services") or []),
        "global_usage": await global_usage_summary(),
    }


@router.put(
    "/profile",
    dependencies=[Depends(verify_local_auth)],
    summary="Update this node's profile",
    tags=["Profile"],
)
async def update_my_profile(request: Request) -> dict:
    from nexus.core.config import normalize_hosted_services
    from nexus.storage.repositories import save_local_settings_to_db

    body = await request.json()
    if "about_me" in body:
        LOCAL_SETTINGS["about_me"] = str(body.get("about_me") or "")[:1000]
    if "display_name" in body:
        name = str(body.get("display_name") or "").strip()[:50]
        if name:
            LOCAL_SETTINGS["user_display_name"] = name
    if "hosted_services" in body:
        LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services(
            body.get("hosted_services")
        )
    await save_local_settings_to_db()
    return {
        "display_name": str(LOCAL_SETTINGS.get("user_display_name", "") or ""),
        "about_me": str(LOCAL_SETTINGS.get("about_me", "") or ""),
        "hosted_services": list(LOCAL_SETTINGS.get("hosted_services") or []),
    }


@router.get(
    "/peers/{peer_uuid}/profile",
    dependencies=[Depends(verify_local_auth)],
    summary="A connected peer's / co-member's profile",
    tags=["Profile"],
)
async def get_peer_profile(peer_uuid: str) -> dict:
    """Fetch *peer_uuid*'s advertised profile (about-me, services,
    verified global usage) and augment it with the groups we share + the
    verified resource exchange between this peer and the viewer."""
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage.models import Group, GroupMember

    # Groups in common — computed locally; I only hold groups I belong to, so a
    # group where the peer is a member is by definition shared with me.
    async with get_session() as db:
        gids = (await db.execute(
            select(GroupMember.group_id).where(GroupMember.node_id == peer_uuid)
        )).scalars().all()
        common = []
        for gid in dict.fromkeys(gids):
            g = await db.get(Group, gid)
            common.append({"id": gid, "name": (g.name if g else "") or gid})

    target_ip = await _resolve_dm_target(peer_uuid)
    res = await peer_http_post(target_ip, "/peer/profile", {})
    if res.get("status") != 200:
        return {"reachable": False, "groups_in_common": common}
    profile = res.get("body") or {}

    # Self-guard: if the request looped back to us (e.g. an unresolved peer),
    # never present our own profile as the peer's.
    if str(profile.get("pubkey") or "") == get_local_group_pubkey():
        return {"reachable": False, "reason": "self", "groups_in_common": common}

    profile["reachable"] = True
    profile["groups_in_common"] = common
    profile["exchange_with_you"] = await _exchange_with_pubkey(
        str(profile.get("pubkey") or "")
    )
    profile["reliability_with_you"] = await _reliability_with_peer(peer_uuid)
    return profile


async def _reliability_with_peer(peer_uuid: str) -> dict:
    """Of the tasks THIS node dispatched to *peer_uuid*, how many it
    completed vs failed. Computed from our own task records (trustworthy — the
    provider can't hide its failures from us). Failures are a reliability signal
    only; they are never credited as compute. Note: ``worker`` holds the last
    runner, so a task that failed on this peer then succeeded elsewhere counts
    as the later success — an approximation, deliberately."""
    from nexus.core.identity import resolve_uuid_to_ip
    from nexus.storage.models import TaskRecord

    ids = {peer_uuid}
    ip = resolve_uuid_to_ip(peer_uuid)
    if ip:
        ids.add(ip)
    async with get_session() as db:
        rows = (await db.execute(
            select(TaskRecord.status).where(TaskRecord.worker.in_(ids))
        )).scalars().all()
    ok = sum(1 for s in rows if s == "completed")
    failed = sum(1 for s in rows if s in ("failed", "disrupted"))
    total = ok + failed
    return {
        "ok": ok,
        "failed": failed,
        "success_rate": round(100 * ok / total) if total else None,
    }


async def _exchange_with_pubkey(their_pubkey: str) -> dict:
    """Verified resource exchange between this node and *their_pubkey*, from the
    receipts both sides signed. ``they_gave`` is what they provided to
    us; ``you_gave`` is what we provided to them."""
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage.models import UsageReceipt

    out = {"they_gave_compute_secs": 0, "you_gave_compute_secs": 0,
           "they_hosted_bytes": 0, "you_hosted_bytes": 0}
    me = get_local_group_pubkey()
    if not their_pubkey or their_pubkey == me:
        return out
    pair = {me, their_pubkey}
    async with get_session() as db:
        rows = (await db.execute(
            select(UsageReceipt).where(
                UsageReceipt.provider_pubkey.in_(pair)
                & UsageReceipt.consumer_pubkey.in_(pair)
            )
        )).scalars().all()
    for r in rows:
        if r.provider_pubkey not in pair or r.consumer_pubkey not in pair:
            continue
        amt = int(r.amount or 0)
        they_provided = r.provider_pubkey == their_pubkey
        if r.kind == "compute":
            out["they_gave_compute_secs" if they_provided else "you_gave_compute_secs"] += amt
        elif r.kind == "storage":
            out["they_hosted_bytes" if they_provided else "you_hosted_bytes"] += amt
    return out


# ---------------------------------------------------------------------------
# /local/service* (service-access grant lifecycle)
# ---------------------------------------------------------------------------


@router.post(
    "/peers/{peer_uuid}/services/{service_name}/request",
    dependencies=[Depends(verify_local_auth)],
    summary="Request access to a peer's advertised service",
    tags=["Services"],
)
async def request_service_access(peer_uuid: str, service_name: str, request: Request) -> dict:
    from nexus.runtime.service_grants import request_access

    body = await request.json()
    provider_pubkey = str(body.get("provider_pubkey") or "")
    if not provider_pubkey:
        raise HTTPException(400, "provider_pubkey required")
    return await request_access(peer_uuid, service_name, provider_pubkey)


@router.get(
    "/service_requests",
    dependencies=[Depends(verify_local_auth)],
    summary="Pending access requests awaiting my decision (host inbox)",
    tags=["Services"],
)
async def service_requests_inbox() -> dict:
    from nexus.runtime.service_grants import list_pending_requests
    return {"requests": await list_pending_requests()}


@router.post(
    "/service_requests/{grant_id}/{decision}",
    dependencies=[Depends(verify_local_auth)],
    summary="Approve or deny a pending service request",
    tags=["Services"],
)
async def decide_service_request(grant_id: str, decision: str) -> dict:
    from nexus.runtime.service_grants import decide_request

    if decision not in ("approve", "deny"):
        raise HTTPException(400, "decision must be approve or deny")
    res = await decide_request(grant_id, decision == "approve")
    if not res.get("ok"):
        raise HTTPException(404, res.get("error") or "not found")
    return res


@router.post(
    "/service_grants/{grant_id}/revoke",
    dependencies=[Depends(verify_local_auth)],
    summary="Revoke a service grant I issued",
    tags=["Services"],
)
async def revoke_service_grant(grant_id: str) -> dict:
    from nexus.runtime.service_grants import revoke_grant

    res = await revoke_grant(grant_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error") or "not found")
    return res


@router.get(
    "/service_grants",
    dependencies=[Depends(verify_local_auth)],
    summary="Service grants I issued and grants I hold",
    tags=["Services"],
)
async def my_service_grants() -> dict:
    from nexus.runtime.service_grants import list_grants
    return await list_grants()


@router.get(
    "/services/discover",
    dependencies=[Depends(verify_local_auth)],
    summary="Discover services advertised by connected peers + group co-members",
    tags=["Services"],
)
async def discover_network_services() -> dict:
    from nexus.runtime.service_grants import discover_services
    return await discover_services()


@router.post(
    "/peers/{peer_uuid}/services/{service_name}/cookbook",
    dependencies=[Depends(verify_local_auth)],
    summary="Copy a replicable service's cookbook to this machine",
    tags=["Services"],
)
async def copy_service_cookbook(peer_uuid: str, service_name: str) -> dict:
    from nexus.runtime.service_grants import replicate_cookbook

    res = await replicate_cookbook(peer_uuid, service_name)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "cannot copy")
    return res


@router.get(
    "/cookbooks",
    dependencies=[Depends(verify_local_auth)],
    summary="Cookbooks copied to this machine",
    tags=["Services"],
)
async def list_local_cookbooks() -> dict:
    from nexus.runtime.service_grants import list_cookbooks
    return list_cookbooks()


@router.get(
    "/replica_runners",
    dependencies=[Depends(verify_local_auth)],
    summary="Sandbox runners available on this machine for auto-run",
    tags=["Services"],
)
async def list_replica_runners() -> dict:
    from nexus.runtime.replica_runner import available_runners
    return {"runners": available_runners()}


@router.post(
    "/peers/{peer_uuid}/services/{service_name}/run",
    dependencies=[Depends(verify_local_auth)],
    summary="Auto-run a replicable service in a chosen sandbox",
    tags=["Services"],
)
async def run_service_replica(peer_uuid: str, service_name: str, request: Request) -> dict:
    from nexus.runtime.replica_runner import run_replica

    body = await request.json()
    res = await run_replica(
        peer_uuid, service_name,
        str(body.get("runner") or "docker"),
        bool(body.get("allow_outbound")),
        bool(body.get("agreed")),
    )
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "cannot run")
    return res


@router.get(
    "/replicas",
    dependencies=[Depends(verify_local_auth)],
    summary="Services replicated and running on this machine",
    tags=["Services"],
)
async def list_local_replicas() -> dict:
    from nexus.runtime.replica_runner import list_replicas
    return list_replicas()


@router.post(
    "/replicas/{replica_id}/stop",
    dependencies=[Depends(verify_local_auth)],
    summary="Stop a running replica",
    tags=["Services"],
)
async def stop_local_replica(replica_id: str) -> dict:
    from nexus.runtime.replica_runner import stop_replica
    res = await stop_replica(replica_id)
    if not res.get("ok"):
        raise HTTPException(404, res.get("error") or "not found")
    return res


@router.post(
    "/service_grants/{grant_id}/connect",
    dependencies=[Depends(verify_local_auth)],
    summary="Open a local tunnel to a granted service",
    tags=["Services"],
)
async def connect_service(grant_id: str) -> dict:
    from nexus.runtime.service_tunnel import open_tunnel
    res = await open_tunnel(grant_id)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "cannot connect")
    return res


@router.post(
    "/service_grants/{grant_id}/disconnect",
    dependencies=[Depends(verify_local_auth)],
    summary="Tear down a service tunnel",
    tags=["Services"],
)
async def disconnect_service(grant_id: str) -> dict:
    from nexus.runtime.service_tunnel import disconnect_tunnel
    return await disconnect_tunnel(grant_id)


@router.get(
    "/service_grants/{grant_id}/connect_status",
    dependencies=[Depends(verify_local_auth)],
    summary="Service tunnel status",
    tags=["Services"],
)
async def service_connect_status(grant_id: str) -> dict:
    from nexus.runtime.service_tunnel import tunnel_status
    return tunnel_status(grant_id)


@router.get(
    "/service_grants/{grant_id}/db_credentials",
    dependencies=[Depends(verify_local_auth)],
    summary="Fetch DBaaS credentials for a held, approved DB-service grant",
    tags=["Services"],
)
async def service_db_credentials(grant_id: str) -> dict:
    """Consumer side. For a DB-kind service we hold an approved grant
    for, fetch the provider-provisioned per-consumer connection (engine, db,
    user, password). Combine with the tunnel's local port for a full DSN."""
    from nexus.runtime.service_grants import fetch_db_credentials
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage import get_session
    from nexus.storage.models import ServiceGrant

    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        if g is None or g.consumer_pubkey != get_local_group_pubkey():
            raise HTTPException(404, "grant not found")
        if g.status != "approved":
            raise HTTPException(409, "grant not approved")
        provider_uuid, provider_pub, service = (
            g.provider_uuid, g.provider_pubkey, g.service_name)

    res = await fetch_db_credentials(provider_uuid, service, provider_pub)
    if not res.get("ok"):
        raise HTTPException(502, res.get("error") or "credential fetch failed")
    return res


# ---------------------------------------------------------------------------
# /local/plugins (/ A8 — in-app editor for drop-in plugin modules)
# ---------------------------------------------------------------------------


@router.get(
    "/plugins",
    dependencies=[Depends(verify_local_auth)],
    summary="All editable plugin kinds + their modules",
    tags=["Plugins"],
)
async def list_plugin_kinds() -> dict:
    from nexus.runtime.plugin_files import overview
    return {"kinds": overview()}


@router.get(
    "/plugins/{kind}/builtin/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Read a shipped built-in reference module's source (read-only)",
    tags=["Plugins"],
)
async def read_builtin_plugin(kind: str, name: str) -> dict:
    from nexus.runtime.plugin_files import builtin_source
    src = builtin_source(kind, name)
    if not src:
        raise HTTPException(404, "no such built-in")
    return src


# --- D1: plugin packages (share & install) ------------------------------------
# Defined BEFORE the generic /plugins/{kind}/{name} routes so the literal
# "packages"/"export"/"install" paths match first instead of being captured as
# a {kind}/{name} pair.


@router.post(
    "/plugins/export",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: bundle plugin modules into a portable package (optionally save it)",
    tags=["Plugins"],
)
async def export_plugin_package(payload: dict) -> dict:
    from nexus.runtime import plugin_packages

    try:
        pkg = plugin_packages.build_package(
            payload.get("items") or [],
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = {"package": pkg}
    if payload.get("save"):
        out["saved"] = plugin_packages.save_package(pkg)
        await write_audit_event(
            "plugin_package.saved", actor=get_node_identity(),
            details=f"name={pkg.get('name')} modules={len(pkg['modules'])}",
        )
    return out


@router.post(
    "/plugins/install",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: install plugin modules from an uploaded package (never executes)",
    tags=["Plugins"],
)
async def install_plugin_package(payload: dict) -> dict:
    from nexus.runtime import plugin_packages

    pkg = payload.get("package")
    if not isinstance(pkg, dict):
        raise HTTPException(400, "package must be a JSON object")
    try:
        result = plugin_packages.install_package(pkg, overwrite=bool(payload.get("overwrite")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await write_audit_event(
        "plugin_package.installed", actor=get_node_identity(),
        details=f"installed={result['installed']} skipped={result['skipped']} errors={result['errors']}",
    )
    return result


@router.get(
    "/plugins/packages",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: list saved plugin packages in the local library",
    tags=["Plugins"],
)
async def list_plugin_packages() -> dict:
    from nexus.runtime import plugin_packages
    return {"packages": plugin_packages.list_packages()}


@router.get(
    "/plugins/packages/{filename}",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: download a saved plugin package",
    tags=["Plugins"],
)
async def get_plugin_package(filename: str) -> dict:
    from nexus.runtime import plugin_packages
    try:
        return plugin_packages.read_package(filename)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.delete(
    "/plugins/packages/{filename}",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: delete a saved plugin package",
    tags=["Plugins"],
)
async def delete_plugin_package(filename: str) -> dict:
    from nexus.runtime import plugin_packages
    try:
        return plugin_packages.delete_package(filename)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post(
    "/plugins/packages/{filename}/install",
    dependencies=[Depends(verify_local_auth)],
    summary="D1: install a saved plugin package from the library",
    tags=["Plugins"],
)
async def install_saved_plugin_package(filename: str, payload: dict | None = None) -> dict:
    from nexus.runtime import plugin_packages
    try:
        pkg = plugin_packages.read_package(filename)
        result = plugin_packages.install_package(
            pkg, overwrite=bool((payload or {}).get("overwrite"))
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await write_audit_event(
        "plugin_package.installed", actor=get_node_identity(),
        details=f"from={filename} installed={result['installed']} skipped={result['skipped']}",
    )
    return result


@router.get(
    "/plugins/{kind}/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Read a plugin module's source",
    tags=["Plugins"],
)
async def read_plugin(kind: str, name: str) -> dict:
    from nexus.runtime.plugin_files import read_module
    try:
        src = read_module(kind, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not src:
        raise HTTPException(404, "no such module")
    return src


@router.put(
    "/plugins/{kind}/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Create/overwrite a plugin module (validates syntax; does not run it)",
    tags=["Plugins"],
)
async def write_plugin(kind: str, name: str, request: Request) -> dict:
    from nexus.runtime.plugin_files import write_module
    body = await request.json()
    try:
        return write_module(kind, name, str(body.get("source") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete(
    "/plugins/{kind}/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a plugin module",
    tags=["Plugins"],
)
async def delete_plugin(kind: str, name: str) -> dict:
    from nexus.runtime.plugin_files import delete_module
    try:
        res = delete_module(kind, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not res.get("ok"):
        code = 404 if res.get("error") == "not_found" else 409
        raise HTTPException(code, res.get("error") or "cannot delete")
    return res


@router.post(
    "/plugins/validate",
    dependencies=[Depends(verify_local_auth)],
    summary="Python-syntax check a plugin source (no execution)",
    tags=["Plugins"],
)
async def validate_plugin(request: Request) -> dict:
    from nexus.runtime.plugin_files import validate_source
    body = await request.json()
    return validate_source(str(body.get("source") or ""))


# ---------------------------------------------------------------------------
# /local/database  (wipe tasks)
# ---------------------------------------------------------------------------

@router.delete(
    "/database",
    dependencies=[Depends(verify_local_auth)],
    summary="Wipe all task records from the database",
)
async def clear_database() -> dict:
    from nexus.runtime import result_browser
    from nexus.storage import database as _db

    async with get_session() as db:
        await db.execute(delete(TaskRecord))
        await db.commit()
    # Reclaim the freed pages so the on-disk size (Diagnostics → Storage usage)
    # actually drops; SQLite keeps the file the same size on plain DELETE.
    try:
        if _db._engine is not None:
            async with _db._engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                await conn.execute(text("VACUUM"))
    except Exception:
        pass
    n = result_browser.delete_all_bundles()
    suffix = f" + {n} result bundle{'s' if n != 1 else ''}" if n else ""
    return {"message": f"Telemetry database wiped{suffix}."}


# ---------------------------------------------------------------------------
# Task actions
# ---------------------------------------------------------------------------

@router.get(
    "/task_manifest/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Return the parsed task.json manifest for cloning into the dispatcher",
    tags=["Task Lifecycle"],
)
async def local_task_manifest(task_id: str) -> dict:
    """Return the manifest dict so the UI can pre-fill the dispatcher form.

    Used by the "Clone" action on failed/completed task rows. Workspace
    files are NOT returned — the caller re-uploads a fresh zip.
    """
    from sqlalchemy.orm import undefer

    from nexus.scheduler.manifest import read_task_manifest

    async with get_session() as db:
        task = (
            await db.execute(
                select(TaskRecord)
                .options(undefer(TaskRecord.payload))
                .filter(TaskRecord.id == task_id)
            )
        ).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")
    manifest = read_task_manifest(task_payload=task.payload, cache_key=task.id)
    return {"task_id": task_id, "manifest": manifest}


@router.get(
    "/task_queue_insight/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Why is this task still queued?",
    tags=["Task Lifecycle"],
)
async def local_task_queue_insight(task_id: str) -> dict:
    """Per-worker dispatch diagnosis for a queued task.

    Re-runs the REAL scheduler gates (the same predicates dispatch uses)
    against every known worker and reports the first gate each one fails,
    so the UI can answer "why is this queued?" with the truth, not a guess.
    """
    from sqlalchemy.orm import undefer

    from nexus.scheduler.fitness import worker_fit_score, worker_unsupported_reason
    from nexus.scheduler.manifest import read_task_manifest
    from nexus.scheduler.selection import _allowed_targets
    from nexus.tasks.metadata import parse_task_env
    from nexus.utils import now_epoch

    async with get_session() as db:
        task = (
            await db.execute(
                select(TaskRecord)
                .options(undefer(TaskRecord.payload))
                .filter(TaskRecord.id == task_id)
            )
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(404, "Task not found")
        if task.status != "queued":
            return {"task_id": task_id, "status": task.status,
                    "summary": "Task is not queued.", "workers": [], "notes": []}
        processing = (
            await db.execute(
                select(TaskRecord).filter(TaskRecord.status == "processing")
            )
        ).scalars().all()

    metadata = extract_task_metadata(task)
    origin = metadata.get("requested_by") or "unknown"
    processing_same_origin = sum(
        1 for p in processing
        if (extract_task_metadata(p).get("requested_by") or "unknown") == origin
    )
    quota = int(LOCAL_SETTINGS["master_quota_per_origin"])

    target_groups = metadata.get("target_groups") or []
    group_pool = None
    if target_groups:
        from nexus.runtime.group_compute import build_group_worker_pool
        group_pool = await build_group_worker_pool(set(target_groups))
    allowed = _allowed_targets(
        set(metadata["preferred_workers"]), target_groups, group_pool, metadata
    )

    env = parse_task_env(task)
    fw_raw = env.get("NEXUS_META_FAILED_WORKERS", [])
    failed_workers = set(fw_raw) if isinstance(fw_raw, list) else set()

    manifest = read_task_manifest(task_payload=task.payload, cache_key=task.id)
    req_ram = int(manifest.get("ram_limit_mb", 512) or 512)
    req_cpu = int(manifest.get("cpu_limit_pct", 100) or 100)

    rows = []
    eligible = 0
    for cid, info in STATE.active_workers.items():
        if now_epoch() < float(STATE.worker_cooldown_until.get(cid, 0) or 0):
            reason = "cooling down after a recent failure"
        elif time.time() - float(info.get("last_seen", 0) or 0) > 12:
            reason = "offline (no heartbeat in the last 12s)"
        elif allowed is not None and cid not in allowed:
            reason = "not in this task's target set (picked workers/groups), or blocked for this dispatch"
        elif cid in failed_workers:
            reason = "already failed this task earlier"
        else:
            reason = worker_unsupported_reason(info, task)
            if reason is None and worker_fit_score(info, req_ram, req_cpu) is None:
                reason = "no dispatchable RAM right now (busy or capped)"
        if reason is None:
            eligible += 1
        rows.append({"worker": cid, "ok": reason is None,
                     "reason": reason or "eligible — picks it up on its next work request"})

    # Requirements only the worker enforces at start (its own settings are
    # not visible from here) — surfaced as notes, not per-worker verdicts.
    notes = []
    if bool(manifest.get("network_required")):
        notes.append("Declares it needs network access — a worker whose settings "
                     "disallow network tasks will fail it at start.")
    if str(manifest.get("runtime", "docker")) == "native":
        notes.append("Native runtime — workers with native execution disabled "
                     "will reject it at start.")

    if quota and processing_same_origin >= quota:
        summary = (f"Concurrency quota full: {processing_same_origin}/{quota} of this "
                   "origin's tasks are already processing — this one waits for a slot.")
    elif not STATE.active_workers:
        summary = ("No workers connected — pair with a peer in Network, or join a "
                   "group whose members run tasks.")
    elif allowed is not None and not allowed:
        summary = ("The target set is empty — the picked groups have no members "
                   "holding task:run, and/or every target is blocked.")
    elif eligible == 0:
        summary = "No connected worker currently qualifies — see the per-worker reasons."
    else:
        summary = (f"{eligible} worker{'s' if eligible != 1 else ''} qualify — the task "
                   "is waiting its turn in the queue; workers pull work every few seconds.")
    return {"task_id": task_id, "status": "queued", "summary": summary,
            "workers": rows, "notes": notes}


@router.post(
    "/requeue_task/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Re-queue a terminal task for another attempt",
    tags=["Task Lifecycle"],
)
async def local_requeue_task(task_id: str) -> dict:
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if not task:
            raise HTTPException(404)
        if extract_task_metadata(task)["coordination"] == "serving":
            raise HTTPException(400)
        if not set_task_status(
            task, "queued", "Manual re-queue from telemetry UI.", force=True
        ):
            raise HTTPException(409)
        task.worker = None
        await db.commit()

    STATE.disrupted_master_tasks.discard(task_id)
    await enqueue_task(task_id)
    incr_metric("tasks_requeued")
    await write_audit_event(
        "task_requeued", actor=get_node_identity(), task_id=task_id
    )
    await ws_manager.broadcast_ping()
    return {"status": "ok"}


@router.post(
    "/workflows/{workflow_id}/resume",
    dependencies=[Depends(verify_local_auth)],
    summary="C2: resume a stalled DAG — re-queue failed steps, re-arm blocked ones",
    tags=["Task Lifecycle"],
)
async def local_resume_workflow(workflow_id: str) -> dict:
    from nexus.tasks.workflow_resume import resume_workflow

    res = await resume_workflow(workflow_id)
    if res.get("found", 0) == 0:
        raise HTTPException(404, detail="No tasks found for this workflow.")
    await write_audit_event(
        "workflow.resumed", actor=get_node_identity(), task_id=workflow_id,
        details=f"requeued={len(res['requeued'])} rearmed={len(res['rearmed'])}",
    )
    await ws_manager.broadcast_ping()
    return {"status": "ok", **res}


@router.post(
    "/workflows/{workflow_id}/approve_step",
    dependencies=[Depends(verify_local_auth)],
    summary="A3 step gate: release the steps awaiting approval (the ready level)",
    tags=["Task Lifecycle"],
)
async def local_approve_workflow_step(workflow_id: str) -> dict:
    """Release every gated step of *workflow_id* that is waiting on approval —
    i.e. the current finished level's downstream frontier — into the queue."""
    released: list[str] = []
    async with get_session() as db:
        tasks = (
            (
                await db.execute(
                    select(TaskRecord).filter(
                        TaskRecord.parent_id == workflow_id,
                        TaskRecord.status == "awaiting_approval",
                    )
                )
            )
            .scalars()
            .all()
        )
        for task in tasks:
            if set_task_status(task, "queued", "Step approved by user."):
                await enqueue_task(task.id)
                released.append(task.id)
        await db.commit()
    if not released:
        raise HTTPException(404, detail="No steps are awaiting approval for this workflow.")
    await write_audit_event(
        "workflow.step_approved", actor=get_node_identity(), task_id=workflow_id,
        details=f"released={len(released)}",
    )
    await ws_manager.broadcast_ping()
    return {"status": "ok", "released": released}


@router.get(
    "/storage_usage",
    dependencies=[Depends(verify_local_auth)],
    summary="App storage breakdown by category (deletable vs not) for Diagnostics",
    tags=["Diagnostics"],
)
async def local_storage_usage() -> dict:
    """This node's on-disk footprint grouped into categories."""
    from nexus.runtime import storage_usage
    return storage_usage.scan()


@router.post(
    "/storage_usage/clear",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a deletable storage category (caches, artifacts, backups, stale DBs)",
    tags=["Diagnostics"],
)
async def local_storage_usage_clear(payload: dict) -> dict:
    """Wipe a single deletable category. Live data and hosted deposits refuse."""
    from nexus.runtime import storage_usage
    key = str((payload or {}).get("key") or "")
    try:
        res = storage_usage.clear(key)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    await write_audit_event(
        "storage.category_cleared", actor=get_node_identity(),
        details=f"key={key} freed={res['removed_bytes']}",
    )
    return {"status": "ok", **res}


@router.get(
    "/storage_usage/files",
    dependencies=[Depends(verify_local_auth)],
    summary="List the individual files inside a deletable storage category",
    tags=["Diagnostics"],
)
async def local_storage_usage_files(key: str) -> dict:
    from nexus.runtime import storage_usage
    try:
        files = storage_usage.list_files(key)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return {"key": key, "files": files}


@router.post(
    "/storage_usage/delete_file",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete one file from a deletable storage category",
    tags=["Diagnostics"],
)
async def local_storage_usage_delete_file(payload: dict) -> dict:
    from nexus.runtime import storage_usage
    key = str((payload or {}).get("key") or "")
    path = str((payload or {}).get("path") or "")
    try:
        res = storage_usage.delete_file(key, path)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    await write_audit_event(
        "storage.file_deleted", actor=get_node_identity(),
        details=f"key={key} freed={res['removed_bytes']}",
    )
    return {"status": "ok", **res}


@router.get(
    "/whats_new",
    dependencies=[Depends(verify_local_auth)],
    summary="B2: in-app changelog for the What's-new panel / notification bell",
    tags=["Diagnostics"],
)
async def local_whats_new() -> dict:
    """Return this build's version + the parsed changelog entries (newest first)."""
    from nexus import __version__
    from nexus.runtime.whats_new import load_entries

    entries = load_entries()
    return {
        "current": __version__,
        "latest": entries[0]["version"] if entries else __version__,
        "entries": entries,
    }


# --- DAG #4: node-local saved DAG templates -----------------------------------


@router.get(
    "/dag_templates",
    dependencies=[Depends(verify_local_auth)],
    summary="List saved DAG templates",
    tags=["Task Lifecycle"],
)
async def local_list_dag_templates() -> dict:
    tpls = LOCAL_SETTINGS.get("dag_templates") or {}
    return {"templates": [{"name": n, **v} for n, v in sorted(tpls.items())]}


@router.post(
    "/dag_templates",
    dependencies=[Depends(verify_local_auth)],
    summary="Save (create or replace) a DAG template",
    tags=["Task Lifecycle"],
)
async def local_save_dag_template(payload: dict) -> dict:
    from nexus.core.config import _normalize_dag_templates
    from nexus.storage.repositories import save_local_settings_to_db
    from nexus.utils.time import timestamp

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    try:
        steps = json.loads(payload.get("workflow_json") or "[]")
    except Exception:
        raise HTTPException(400, "workflow_json is not valid JSON")
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, "workflow_json must be a non-empty JSON array of steps")

    tpls = dict(LOCAL_SETTINGS.get("dag_templates") or {})
    tpls[name] = {
        "steps": steps,
        "description": str(payload.get("description") or ""),
        "created_at": timestamp(),
    }
    LOCAL_SETTINGS["dag_templates"] = _normalize_dag_templates(tpls)
    await save_local_settings_to_db()
    await write_audit_event("dag_template.saved", actor=get_node_identity(), details=f"name={name}")
    return {"status": "ok", "name": name, "count": len(LOCAL_SETTINGS["dag_templates"])}


@router.delete(
    "/dag_templates/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a saved DAG template",
    tags=["Task Lifecycle"],
)
async def local_delete_dag_template(name: str) -> dict:
    from nexus.storage.repositories import save_local_settings_to_db

    tpls = dict(LOCAL_SETTINGS.get("dag_templates") or {})
    if name not in tpls:
        raise HTTPException(404, "no such template")
    tpls.pop(name, None)
    LOCAL_SETTINGS["dag_templates"] = tpls
    await save_local_settings_to_db()
    await write_audit_event("dag_template.deleted", actor=get_node_identity(), details=f"name={name}")
    return {"status": "ok"}


# --- Node-local saved dispatch-settings profiles ------------------------------


@router.get(
    "/dispatch_templates",
    dependencies=[Depends(verify_local_auth)],
    summary="List saved dispatch-settings profiles",
    tags=["Task Lifecycle"],
)
async def local_list_dispatch_templates() -> dict:
    tpls = LOCAL_SETTINGS.get("dispatch_templates") or {}
    return {"templates": [{"name": n, **v} for n, v in sorted(tpls.items())]}


@router.post(
    "/dispatch_templates",
    dependencies=[Depends(verify_local_auth)],
    summary="Save (create or replace) a dispatch-settings profile",
    tags=["Task Lifecycle"],
)
async def local_save_dispatch_template(payload: dict) -> dict:
    from nexus.core.config import _normalize_dispatch_templates
    from nexus.storage.repositories import save_local_settings_to_db
    from nexus.utils.time import timestamp

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    settings = payload.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise HTTPException(400, "settings must be a non-empty object")

    tpls = dict(LOCAL_SETTINGS.get("dispatch_templates") or {})
    tpls[name] = {
        "settings": settings,
        "description": str(payload.get("description") or ""),
        "created_at": timestamp(),
    }
    LOCAL_SETTINGS["dispatch_templates"] = _normalize_dispatch_templates(tpls)
    await save_local_settings_to_db()
    await write_audit_event("dispatch_template.saved", actor=get_node_identity(), details=f"name={name}")
    return {"status": "ok", "name": name, "count": len(LOCAL_SETTINGS["dispatch_templates"])}


@router.delete(
    "/dispatch_templates/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a saved dispatch-settings profile",
    tags=["Task Lifecycle"],
)
async def local_delete_dispatch_template(name: str) -> dict:
    from nexus.storage.repositories import save_local_settings_to_db

    tpls = dict(LOCAL_SETTINGS.get("dispatch_templates") or {})
    if name not in tpls:
        raise HTTPException(404, "no such template")
    tpls.pop(name, None)
    LOCAL_SETTINGS["dispatch_templates"] = tpls
    await save_local_settings_to_db()
    await write_audit_event("dispatch_template.deleted", actor=get_node_identity(), details=f"name={name}")
    return {"status": "ok"}


# --- D3: outbound webhooks ----------------------------------------------------


def _mask_webhook(hook: dict) -> dict:
    """Public view of a subscription — never echo the signing secret back."""
    return {
        "id": hook.get("id"),
        "url": hook.get("url"),
        "events": hook.get("events") or [],
        "enabled": hook.get("enabled", True),
        "description": hook.get("description", ""),
        "has_secret": bool(hook.get("secret")),
    }


@router.get(
    "/webhooks",
    dependencies=[Depends(verify_local_auth)],
    summary="D3: list webhook subscriptions, the event catalog, and recent deliveries",
    tags=["Events"],
)
async def local_list_webhooks() -> dict:
    from nexus.runtime.webhooks import WEBHOOK_EVENTS, recent_deliveries

    hooks = LOCAL_SETTINGS.get("webhooks") or []
    return {
        "webhooks": [_mask_webhook(h) for h in hooks],
        "events": WEBHOOK_EVENTS,
        "deliveries": recent_deliveries(),
    }


@router.post(
    "/webhooks",
    dependencies=[Depends(verify_local_auth)],
    summary="D3: create or update (by id) a webhook subscription",
    tags=["Events"],
)
async def local_save_webhook(payload: dict) -> dict:
    from nexus.core.config import _normalize_webhooks
    from nexus.storage.repositories import save_local_settings_to_db

    url = str(payload.get("url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "url must be an http(s) URL")
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(400, "events must be a non-empty list")

    hooks = list(LOCAL_SETTINGS.get("webhooks") or [])
    hook_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex[:12]
    entry = {
        "id": hook_id,
        "url": url,
        "events": events,
        "secret": str(payload.get("secret") or ""),
        "enabled": payload.get("enabled", True),
        "description": str(payload.get("description") or ""),
    }
    # Upsert by id: a blank secret on update keeps the existing one.
    replaced = False
    for i, h in enumerate(hooks):
        if h.get("id") == hook_id:
            if not entry["secret"]:
                entry["secret"] = h.get("secret", "")
            hooks[i] = entry
            replaced = True
            break
    if not replaced:
        hooks.append(entry)

    LOCAL_SETTINGS["webhooks"] = _normalize_webhooks(hooks)
    await save_local_settings_to_db()
    await write_audit_event(
        "webhook.saved", actor=get_node_identity(), details=f"id={hook_id} url={url}"
    )
    return {"status": "ok", "id": hook_id, "count": len(LOCAL_SETTINGS["webhooks"])}


@router.delete(
    "/webhooks/{hook_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="D3: delete a webhook subscription",
    tags=["Events"],
)
async def local_delete_webhook(hook_id: str) -> dict:
    from nexus.storage.repositories import save_local_settings_to_db

    hooks = list(LOCAL_SETTINGS.get("webhooks") or [])
    kept = [h for h in hooks if h.get("id") != hook_id]
    if len(kept) == len(hooks):
        raise HTTPException(404, "no such webhook")
    LOCAL_SETTINGS["webhooks"] = kept
    await save_local_settings_to_db()
    await write_audit_event(
        "webhook.deleted", actor=get_node_identity(), details=f"id={hook_id}"
    )
    return {"status": "ok"}


@router.post(
    "/webhooks/{hook_id}/test",
    dependencies=[Depends(verify_local_auth)],
    summary="D3: send a test event to one webhook subscription",
    tags=["Events"],
)
async def local_test_webhook(hook_id: str) -> dict:
    from nexus.runtime import webhooks as _webhooks

    hooks = LOCAL_SETTINGS.get("webhooks") or []
    hook = next((h for h in hooks if h.get("id") == hook_id), None)
    if not hook:
        raise HTTPException(404, "no such webhook")
    result = await _webhooks._deliver(
        hook, "webhook.test", {"message": "Test delivery from NexusGrid"}, get_node_identity()
    )
    return {"status": "ok", "result": result}


# --- C7: one-click DBaaS engine bring-up --------------------------------------


@router.get(
    "/dbaas/engines",
    dependencies=[Depends(verify_local_auth)],
    summary="C7: list database engines this node can start with one click",
    tags=["Services"],
)
async def local_dbaas_engines() -> dict:
    from nexus.runtime import db_engine
    return {"engines": db_engine.list_engines()}


@router.post(
    "/dbaas/start_engine",
    dependencies=[Depends(verify_local_auth)],
    summary="C7: start a managed local DB engine and return its admin DSN",
    tags=["Services"],
)
async def local_dbaas_start_engine(payload: dict) -> dict:
    """Launch a managed engine container (loopback-only) and return the admin DSN
    to paste into a service's db_provider config. Blocking Docker work runs off
    the event loop."""
    from nexus.runtime import db_engine

    engine = str((payload or {}).get("engine") or "").strip().lower()
    if engine not in db_engine.list_engines():
        raise HTTPException(400, detail=f"unsupported engine '{engine}'")
    try:
        info = await asyncio.to_thread(db_engine.start_engine, engine)
    except Exception as exc:
        raise HTTPException(502, detail=f"could not start {engine}: {exc}")
    await write_audit_event(
        "dbaas.engine_started", actor=get_node_identity(),
        details=f"engine={engine} container={info.get('container')}",
    )
    return info


@router.post(
    "/dbaas/stop_engine",
    dependencies=[Depends(verify_local_auth)],
    summary="C7: stop + remove a managed local DB engine container",
    tags=["Services"],
)
async def local_dbaas_stop_engine(payload: dict) -> dict:
    from nexus.runtime import db_engine

    container = str((payload or {}).get("container") or "").strip()
    if not container:
        raise HTTPException(400, detail="container name required")
    removed = await asyncio.to_thread(db_engine.stop_engine, container)
    if not removed:
        raise HTTPException(404, detail="no such managed engine container")
    await write_audit_event(
        "dbaas.engine_stopped", actor=get_node_identity(), details=f"container={container}",
    )
    return {"status": "ok", "container": container}


@router.post(
    "/disrupt_task/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Disrupt a running task on a remote worker",
    tags=["Task Lifecycle"],
)
async def local_disrupt_processing_task(task_id: str) -> dict:
    """Disrupt a ``processing`` task — local or on a trusted remote worker."""
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if not task or task.status != "processing":
            raise HTTPException(404)
        worker_ip = task.worker
        stop_signal_sent = False

        if worker_ip == get_node_identity():
            stop_signal_sent = await mark_task_interrupted(task_id)
        elif worker_ip:
            peer = (
                await db.execute(
                    select(Peer).filter(
                        Peer.ip == worker_ip, Peer.status == "trusted"
                    )
                )
            ).scalar_one_or_none()
            if peer and peer.their_auth_token:
                try:
                    async with httpx.AsyncClient(
                        headers={
                            "X-Cluster-Key": str(peer.their_auth_token),
                            "X-Node-Address": get_node_identity(),
                        }
                    ) as client_http:
                        res = await client_http.post(
                            f"http://{worker_ip}/peer/disrupt_task/{task_id}",
                            timeout=5.0,
                        )
                    stop_signal_sent = res.status_code == 200
                except Exception:
                    stop_signal_sent = False

        STATE.disrupted_master_tasks.add(task_id)
        set_task_status(
            task,
            "disrupted",
            "Manual disrupt issued from telemetry UI.",
            force=True,
        )
        task.worker = None
        task.logs = (task.logs or "") + (
            f"[{timestamp()}] [MASTER] Manual disrupt issued. "
            f"Worker: {worker_ip or 'unknown'}.\n"
        )
        await db.commit()

    incr_metric("tasks_disrupted")
    await write_audit_event(
        "task_disrupted",
        actor=get_node_identity(),
        task_id=task_id,
        severity="warning",
        details=f"worker={worker_ip or 'unknown'}",
    )
    return {"status": "ok"}


@router.post(
    "/cancel_task/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Cancel a queued or waiting task",
    tags=["Task Lifecycle"],
)
async def local_cancel_task(task_id: str) -> dict:
    """Mark a non-processing task as disrupted (keeps it visible in telemetry)."""
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if not task:
            raise HTTPException(404)
        if task.status == "processing":
            raise HTTPException(
                409, detail="Use disrupt_task for processing tasks."
            )
        if task.status in TERMINAL_STATES:
            raise HTTPException(409, detail="Task already in terminal state.")
        old_status = task.status
        set_task_status(
            task,
            "disrupted",
            f"Manually disrupted from telemetry UI (was {old_status}).",
            force=True,
        )
        task.worker = None
        task.logs = (task.logs or "") + (
            f"[{timestamp()}] [MASTER] Task disrupted by user. "
            f"Previous status: {old_status}.\n"
        )
        await db.commit()
    STATE.disrupted_master_tasks.add(task_id)
    incr_metric("tasks_disrupted")
    await write_audit_event(
        "task_disrupted",
        actor=get_node_identity(),
        task_id=task_id,
        severity="warning",
    )
    return {"status": "ok"}


@router.delete(
    "/task/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a terminal task from telemetry",
    tags=["Task Lifecycle"],
)
async def local_delete_task(task_id: str) -> dict:
    """Permanently remove a terminal-state task from the DB."""
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if not task:
            raise HTTPException(404)
        if task.status not in TERMINAL_STATES:
            raise HTTPException(
                409, detail="Only terminal-state tasks can be deleted."
            )
        await db.execute(delete(TaskRecord).where(TaskRecord.id == task_id))
        await db.commit()
    STATE.disrupted_master_tasks.discard(task_id)
    await write_audit_event(
        "task_deleted",
        actor=get_node_identity(),
        task_id=task_id,
        severity="warning",
    )
    return {"status": "ok"}


@router.post(
    "/preempt_local_worker_task/{task_id:path}",
    dependencies=[Depends(verify_local_auth)],
    summary="Preempt a task running on the local worker",
    tags=["Task Lifecycle"],
)
async def preempt_local_worker_task(task_id: str) -> dict:
    """Preempt a running task. Accepts both the shadow id and the remote id."""
    original_id = task_id
    if task_id.startswith("remote__"):
        parts = task_id.split("__", 2)
        if len(parts) == 3:
            original_id = parts[2]
    # Use ``preempt_running_task`` (flag + synchronous container.stop /
    # kill_process_tree) rather than ``mark_task_preempted`` (flag only).
    # The handler's response message claims container presence, which only
    # the combined helper can honour. It also restores Phase-1's synchronous
    # teardown — the flag-only path deferred the stop to the executor
    # watchdog's ~2 s poll tick.
    from nexus.runtime.worker_state import preempt_running_task

    preempted = await preempt_running_task(original_id)
    if not preempted:
        preempted = await preempt_running_task(task_id)
    return {
        "status": "ok",
        "preempted": preempted,
        "message": (
            "Preemption signal sent."
            if preempted
            else "No running container found for this task."
        ),
    }


# ---------------------------------------------------------------------------
# /local/settings
# ---------------------------------------------------------------------------

@router.post(
    "/settings",
    dependencies=[Depends(verify_local_auth)],
    summary="Update all node settings",
    tags=["Settings"],
)
async def local_update_settings(
    mode: str = Form(...),
    max_ram: int = Form(...),
    data_retention: str = Form("delete"),
    gdrive_key: str = Form(""),
    node_online: bool = Form(True),
    sharing_mode: str = Form("shared"),
    max_serving_masters: int = Form(2),
    lease_seconds: int = Form(30),
    master_quota_per_origin: int = Form(3),
    retry_backoff_base_sec: int = Form(5),
    worker_cooldown_sec: int = Form(20),
    allowed_images: str = Form("python:3.11-slim,node:20-slim,gcc:latest"),
    node_region: str = Form("local"),
    node_tags: str = Form(""),
    node_gpu: bool = Form(False),
    max_gpu_pct: int = Form(80),
    native_runtime_enabled: bool = Form(False),
    require_worker_consent: bool = Form(False),
    consent_timeout_sec: int = Form(10),
    consent_max_strikes: int = Form(3),
    queue_timeout_sec: int = Form(0),
    user_display_name: str = Form(""),
    relay_server_url: str = Form(""),
    relay_grid_key: str = Form(""),
    relay_enabled: bool = Form(True),
    allow_cross_region_workers: bool = Form(True),
    accept_cross_region_tasks: bool = Form(True),
    security_profile: str = Form("maximum"),
    allow_network_tasks: bool = Form(False),
    enable_task_scanning: bool = Form(True),
    hide_profile: bool = Form(True),
    require_venv_isolation: bool = Form(False),
    cache_venvs: bool = Form(False),
    idle_auto_accept: bool = Form(False),
    idle_threshold_sec: int = Form(300),
    foreign_storage_accept_offers: bool = Form(True),
    storage_max_total_gb: int = Form(5),
    storage_window_chunks: int = Form(32),
    fs_auto_offer_timeout_sec: int = Form(300),
    fs_transit_max_retries: int = Form(5),
    fs_transit_chunk_ack_timeout_sec: int = Form(30),
    fs_transit_silence_timeout_sec: int = Form(60),
    fs_transit_abandoned_chunk_ttl_hours: int = Form(24),
    fs_auto_rescue: bool = Form(True),
    fs_auto_rescue_mode: str = Form("folder_then_cloud"),
    fs_auto_rescue_trigger: str = Form("eviction"),
    fs_auto_rescue_days: int = Form(2),
    fs_auto_rescue_dir: str = Form(""),
    fs_auto_rescue_cloud_cred: str = Form(""),
    fs_auto_rescue_rclone_targets: str = Form(""),
) -> dict:
    """Persist a full settings update; also preempts local tasks if going offline."""
    was_online = LOCAL_SETTINGS.get("node_online", True)
    old_relay_url = LOCAL_SETTINGS.get("relay_server_url", "")
    old_relay_key = LOCAL_SETTINGS.get("relay_grid_key", "")
    old_relay_enabled = LOCAL_SETTINGS.get("relay_enabled", True)
    old_display_name = str(LOCAL_SETTINGS.get("user_display_name", "") or "")

    effective_gpu = bool(node_gpu)
    if effective_gpu and not detect_gpu():
        effective_gpu = False
        _log.warning(
            "node_gpu was requested but no GPU detected; forcing to False."
        )

    LOCAL_SETTINGS.update(
        {
            "mode": mode,
            "max_ram_pct": max_ram,
            "data_retention": data_retention,
            "gdrive_key": gdrive_key,
            "node_online": bool(node_online),
            "sharing_mode": sharing_mode,
            "max_serving_masters": max_serving_masters,
            "lease_seconds": lease_seconds,
            "master_quota_per_origin": master_quota_per_origin,
            "retry_backoff_base_sec": retry_backoff_base_sec,
            "worker_cooldown_sec": worker_cooldown_sec,
            "allowed_images": split_csv(allowed_images),
            "node_region": node_region,
            "node_tags": split_csv(node_tags),
            "node_gpu": effective_gpu,
            "max_gpu_pct": max(10, min(95, int(max_gpu_pct))),
            "native_runtime_enabled": bool(native_runtime_enabled),
            "require_worker_consent": bool(require_worker_consent),
            "consent_timeout_sec": max(3, min(60, int(consent_timeout_sec))),
            "consent_max_strikes": max(0, min(10, int(consent_max_strikes))),
            "queue_timeout_sec": max(0, int(queue_timeout_sec)),
            "user_display_name": str(user_display_name).strip()[:50],
            "relay_server_url": str(relay_server_url).strip(),
            "relay_grid_key": str(relay_grid_key).strip(),
            "relay_enabled": bool(relay_enabled),
            "allow_cross_region_workers": bool(allow_cross_region_workers),
            "accept_cross_region_tasks": bool(accept_cross_region_tasks),
            "security_profile": (
                str(security_profile).strip()
                if str(security_profile).strip() in ("maximum", "standard", "relaxed")
                else "maximum"
            ),
            "allow_network_tasks": bool(allow_network_tasks),
            "enable_task_scanning": bool(enable_task_scanning),
            "hide_profile": bool(hide_profile),
            "require_venv_isolation": bool(require_venv_isolation),
            "cache_venvs": bool(cache_venvs),
            "idle_auto_accept": bool(idle_auto_accept),
            "idle_threshold_sec": max(30, min(86_400, int(idle_threshold_sec))),
            # Foreign-storage opt-out + pledge size.
            "foreign_storage_accept_offers": bool(foreign_storage_accept_offers),
            "storage_max_total_gb": max(1, int(storage_max_total_gb)),
            "storage_window_chunks": max(2, min(128, int(storage_window_chunks))),
            # Transit tuning — classic posted these but the handler dropped
            # them; clamps mirror nexus.core.config's load-time bounds.
            "fs_auto_offer_timeout_sec": max(30, min(86_400, int(fs_auto_offer_timeout_sec))),
            "fs_transit_max_retries": max(1, min(20, int(fs_transit_max_retries))),
            "fs_transit_chunk_ack_timeout_sec": max(5, min(300, int(fs_transit_chunk_ack_timeout_sec))),
            "fs_transit_silence_timeout_sec": max(10, min(600, int(fs_transit_silence_timeout_sec))),
            "fs_transit_abandoned_chunk_ttl_hours": max(1, min(24, int(fs_transit_abandoned_chunk_ttl_hours))),
            # Auto-rescue (depositor-side salvage). Clamps mirror
            # nexus.core.config's load-time bounds.
            "fs_auto_rescue": bool(fs_auto_rescue),
            "fs_auto_rescue_mode": (
                str(fs_auto_rescue_mode).strip().lower()
                if str(fs_auto_rescue_mode).strip().lower() in (
                    "folder_then_cloud", "cloud_then_folder", "folder_only", "cloud_only"
                ) else "folder_then_cloud"
            ),
            "fs_auto_rescue_trigger": (
                str(fs_auto_rescue_trigger).strip().lower()
                if str(fs_auto_rescue_trigger).strip().lower() in ("eviction", "days")
                else "eviction"
            ),
            "fs_auto_rescue_days": max(1, min(30, int(fs_auto_rescue_days))),
            "fs_auto_rescue_dir": str(fs_auto_rescue_dir or ""),
            "fs_auto_rescue_cloud_cred": str(fs_auto_rescue_cloud_cred or ""),
            "fs_auto_rescue_rclone_targets": split_csv(fs_auto_rescue_rclone_targets),
        }
    )

    if was_online and not bool(node_online):
        # Preempt every locally-running task so shutting off comes clean.
        from nexus.runtime.worker_state import (
            _LOCAL_WORKER_STATE,
            preempt_running_task,
        )

        async with STATE.worker_state_lock:
            active_tids = [
                t["task_id"] for t in _LOCAL_WORKER_STATE["active_tasks"]
            ]
        for tid in active_tids:
            await preempt_running_task(tid)

    # Wake relay loop immediately if relay-facing settings changed.
    new_relay_url = str(relay_server_url).strip()
    new_relay_key = str(relay_grid_key).strip()
    if (
        new_relay_url != old_relay_url
        or new_relay_key != old_relay_key
        or bool(relay_enabled) != old_relay_enabled
    ):
        STATE.relay_settings_changed.set()

    await save_local_settings_to_db()

    # Propagate display-name changes into the group system so members'
    # rosters don't show the stale name. Best-effort on the remote leg.
    new_display_name = str(user_display_name).strip()[:50]
    if new_display_name != old_display_name:
        try:
            from nexus.api.groups import propagate_local_display_name
            await propagate_local_display_name(new_display_name)
        except Exception:
            _log.debug("display-name propagation failed", exc_info=True)

    await write_audit_event(
        "settings_updated",
        actor=get_node_identity(),
        details="Local settings updated.",
    )
    return {"message": "Node settings updated."}


# Maps the form-field name the classic endpoint takes to the
# LOCAL_SETTINGS key it lands in, for every field whose names differ or
# whose stored shape isn't the wire shape (csv vs list).
_SETTINGS_FIELD_TO_KEY = {
    "max_ram": "max_ram_pct",
}
_SETTINGS_CSV_FIELDS = {"allowed_images", "node_tags", "fs_auto_rescue_rclone_targets"}
_SETTINGS_BOOL_FIELDS = {
    "node_online", "node_gpu", "native_runtime_enabled",
    "require_worker_consent", "relay_enabled", "allow_cross_region_workers",
    "accept_cross_region_tasks", "allow_network_tasks",
    "enable_task_scanning", "hide_profile", "require_venv_isolation",
    "cache_venvs", "idle_auto_accept", "foreign_storage_accept_offers",
    "fs_auto_rescue",
}
_SETTINGS_INT_FIELDS = {
    "max_ram", "max_serving_masters", "lease_seconds",
    "master_quota_per_origin", "retry_backoff_base_sec",
    "worker_cooldown_sec", "max_gpu_pct", "consent_timeout_sec",
    "consent_max_strikes", "queue_timeout_sec", "idle_threshold_sec",
    "storage_max_total_gb", "storage_window_chunks",
    "fs_auto_offer_timeout_sec", "fs_transit_max_retries",
    "fs_transit_chunk_ack_timeout_sec", "fs_transit_silence_timeout_sec",
    "fs_transit_abandoned_chunk_ttl_hours", "fs_auto_rescue_days",
}
_SETTINGS_STR_FIELDS = {
    "mode", "data_retention", "gdrive_key", "sharing_mode", "node_region",
    "user_display_name", "relay_server_url", "relay_grid_key",
    "security_profile",
    "fs_auto_rescue_mode", "fs_auto_rescue_trigger", "fs_auto_rescue_dir",
    "fs_auto_rescue_cloud_cred",
}
_SETTINGS_ALL_FIELDS = (
    _SETTINGS_BOOL_FIELDS | _SETTINGS_INT_FIELDS | _SETTINGS_STR_FIELDS
    | _SETTINGS_CSV_FIELDS
)
# Mirror of local_update_settings' Form defaults, used only when a key is
# missing from LOCAL_SETTINGS entirely (fresh node edge case).
_SETTINGS_FORM_DEFAULTS = {
    "mode": "user", "max_ram": 80, "data_retention": "delete",
    "gdrive_key": "", "node_online": True, "sharing_mode": "shared",
    "max_serving_masters": 2, "lease_seconds": 30,
    "master_quota_per_origin": 3, "retry_backoff_base_sec": 5,
    "worker_cooldown_sec": 20,
    "allowed_images": "python:3.11-slim,node:20-slim,gcc:latest",
    "node_region": "local", "node_tags": "", "node_gpu": False,
    "max_gpu_pct": 80, "native_runtime_enabled": False,
    "require_worker_consent": False, "consent_timeout_sec": 10,
    "consent_max_strikes": 3, "queue_timeout_sec": 0,
    "user_display_name": "", "relay_server_url": "", "relay_grid_key": "",
    "relay_enabled": True, "allow_cross_region_workers": True,
    "accept_cross_region_tasks": True, "security_profile": "maximum",
    "allow_network_tasks": False, "enable_task_scanning": True,
    "hide_profile": True, "require_venv_isolation": False,
    "cache_venvs": False, "idle_auto_accept": False,
    "idle_threshold_sec": 300, "foreign_storage_accept_offers": True,
    "storage_max_total_gb": 5, "storage_window_chunks": 32,
    "fs_auto_offer_timeout_sec": 300, "fs_transit_max_retries": 5,
    "fs_transit_chunk_ack_timeout_sec": 30, "fs_transit_silence_timeout_sec": 60,
    "fs_transit_abandoned_chunk_ttl_hours": 24,
    "fs_auto_rescue": True, "fs_auto_rescue_mode": "folder_then_cloud",
    "fs_auto_rescue_trigger": "eviction",
    "fs_auto_rescue_days": 2, "fs_auto_rescue_dir": "",
    "fs_auto_rescue_cloud_cred": "", "fs_auto_rescue_rclone_targets": "",
}


@router.post(
    "/settings_partial",
    dependencies=[Depends(verify_local_auth)],
    summary="Update a subset of node settings (JSON; unspecified keys keep their value)",
    tags=["Settings"],
)
async def local_update_settings_partial(payload: dict = Body(...)) -> dict:
    """Safe partial update for the v3 UI.

    Only the keys present in the JSON body change. Every other field is
    re-submitted with its current value through ``local_update_settings``,
    so all clamping and side effects (offline preempt, relay wake,
    display-name propagation, GPU detection) apply identically to both
    UIs. A ``gdrive_key`` of ``"***"`` (the /network mask) never
    overwrites the stored key.
    """
    unknown = [k for k in payload if k not in _SETTINGS_ALL_FIELDS]
    if unknown:
        raise HTTPException(400, f"unknown settings field(s): {unknown}")

    def _current(field: str):
        key = _SETTINGS_FIELD_TO_KEY.get(field, field)
        val = LOCAL_SETTINGS.get(key)
        return _SETTINGS_FORM_DEFAULTS.get(field) if val is None else val

    args: dict = {}
    for field in _SETTINGS_ALL_FIELDS:
        provided = field in payload
        raw = payload[field] if provided else _current(field)
        if field == "gdrive_key" and provided and str(raw) == "***":
            raw = _current(field)
        if field in _SETTINGS_CSV_FIELDS:
            if isinstance(raw, (list, tuple)):
                raw = ",".join(str(x) for x in raw)
            args[field] = str(raw or "")
        elif field in _SETTINGS_BOOL_FIELDS:
            args[field] = bool(raw)
        elif field in _SETTINGS_INT_FIELDS:
            args[field] = int(raw or 0)
        else:
            args[field] = str(raw if raw is not None else "")
    return await local_update_settings(**args)


# ---------------------------------------------------------------------------
# /local/benchmark
# ---------------------------------------------------------------------------

_benchmark_lock = asyncio.Lock()


@router.post(
    "/benchmark",
    dependencies=[Depends(verify_local_auth)],
    summary="Run the local CPU+IO benchmark and persist the result",
    tags=["Settings"],
)
async def local_benchmark() -> dict:
    """Run the self-bench in a worker thread, store the score in settings."""
    from nexus.scheduler.benchmark import run_benchmark

    if _benchmark_lock.locked():
        return {"status": "busy", "message": "A benchmark is already running."}

    async with _benchmark_lock:
        result = await asyncio.to_thread(run_benchmark)

    LOCAL_SETTINGS["benchmark_score"] = float(result["score"])
    LOCAL_SETTINGS["benchmark_at"] = str(result["ran_at"])
    await save_local_settings_to_db()
    await broadcast_ui_update({"type": "state_changed"})
    return {"status": "ok", **result}


# ---------------------------------------------------------------------------
# /local/services 
# ---------------------------------------------------------------------------

def _read_service_manifest(task: TaskRecord) -> dict | None:
    """Return parsed task.json if this is a service task, else None."""
    from nexus.scheduler.manifest import read_task_manifest

    manifest = read_task_manifest(
        task_payload=task.payload, cache_key=task.id
    )
    if str(manifest.get("runtime", "")).lower() != "service":
        return None
    return manifest


def _service_status_summary(task: TaskRecord, manifest: dict) -> dict:
    """Project a service task into the dict the UI consumes."""
    from nexus.runtime.service_kinds import connection_string

    ports = list(manifest.get("expose_ports") or [])
    container_port = int(ports[0]) if ports else 0
    kind = str(manifest.get("service_kind", "tcp") or "tcp")

    tunnel_rec = STATE.service_tunnels.get(task.id) or {}
    local_port = int(tunnel_rec.get("port") or 0)
    tls_on = bool(
        (STATE.service_records.get(task.id) or {}).get("tls_terminate")
        or manifest.get("tls_terminate")
    )
    cs = (
        connection_string(kind, local_port, tls=tls_on) if local_port else ""
    )
    listeners = tunnel_rec.get("listeners") or {}
    port_strings = [
        {
            "container_port": int(cp),
            "host_port": int(entry.get("host_port") or 0),
            "connection_string": connection_string(
                kind, int(entry.get("host_port") or 0), tls=tls_on
            ),
        }
        for cp, entry in sorted(listeners.items())
        if int(entry.get("host_port") or 0)
    ]
    is_active = bool(task.status in ("processing", "serving") and task.worker)
    return {
        "task_id": task.id,
        "worker": task.worker or "",
        "image": str(manifest.get("image", "")),
        "service_kind": kind,
        "expose_ports": ports,
        "container_port": container_port,
        "local_port": local_port,
        "connection_string": cs,
        "ports": port_strings,
        "status": "active" if is_active else str(task.status),
        "raw_status": str(task.status),
    }


@router.get(
    "/services",
    dependencies=[Depends(verify_local_auth)],
    summary="List service tasks I have access to",
    tags=["Services"],
)
async def list_services() -> dict:
    from sqlalchemy.orm import undefer

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(TaskRecord).options(undefer(TaskRecord.payload))
                )
            )
            .scalars()
            .all()
        )

    services: list[dict] = []
    for task in rows:
        manifest = _read_service_manifest(task)
        if not manifest:
            continue
        if task.status in TERMINAL_STATES and task.status != "completed":
            # Show completed (graceful stop) but skip cancelled/failed/disrupted
            # by default — UI can flip this.
            if task.status != "completed":
                continue
        services.append(_service_status_summary(task, manifest))
    return {"services": services}


@router.post(
    "/services/{task_id}/start",
    dependencies=[Depends(verify_local_auth)],
    summary="Open a local TCP listener tunnelled to a service container",
    tags=["Services"],
)
async def start_service_tunnel(task_id: str) -> dict:
    from nexus.networking.tunnel import (
        ensure_local_listener,
        ensure_local_listeners,
        ensure_local_udp_listener,
    )
    from nexus.runtime.service_kinds import connection_string

    async with get_session() as db:
        from sqlalchemy.orm import undefer

        task = (
            await db.execute(
                select(TaskRecord)
                .options(undefer(TaskRecord.payload))
                .filter(TaskRecord.id == task_id)
            )
        ).scalar_one_or_none()

    if task is None:
        raise HTTPException(404, "Task not found")
    manifest = _read_service_manifest(task)
    if manifest is None:
        raise HTTPException(400, "Task is not a service")
    if not task.worker:
        raise HTTPException(409, "Service has no assigned worker yet")
    if task.status not in ("processing", "serving"):
        raise HTTPException(409, f"Service not active (status={task.status})")

    # Cache the manifest's first exposed port + service_kind for the master
    # tunnel; the worker resolves container_port → host_port locally.
    ports = list(manifest.get("expose_ports") or [])
    if not ports:
        raise HTTPException(400, "Service manifest has no expose_ports")
    container_port = int(ports[0])
    kind = str(manifest.get("service_kind", "tcp") or "tcp")

    # The tunnel module needs a record describing this service so it can
    # answer "which container port goes to this stream?". On the master we
    # synthesise a minimal record (the worker has the real one with host
    # ports — we just need the container-side port for tunnel_open).
    image = str(manifest.get("image", "") or "").strip()
    rate_limit_mb_s = int(manifest.get("rate_limit_mb_s", 0) or 0)
    tls_terminate = bool(manifest.get("tls_terminate", False))
    session_replay = bool(manifest.get("session_replay", False))
    shared_tunnel = bool(manifest.get("shared_tunnel", False))
    protocol = str(manifest.get("protocol", "tcp") or "tcp")
    async with STATE.service_lock:
        STATE.service_records.setdefault(
            task_id,
            {
                "task_id": task_id,
                "expose_ports": ports,
                "master_ip": "",  # we are the master
                "worker_id": task.worker,
                "service_kind": kind,
                "image": image,
                "rate_limit_mb_s": rate_limit_mb_s,
                "tls_terminate": tls_terminate,
                "session_replay": session_replay,
                "shared_tunnel": shared_tunnel,
                "protocol": protocol,
            },
        )
        STATE.service_records[task_id]["expose_ports"] = ports
        if image:
            STATE.service_records[task_id]["image"] = image
        STATE.service_records[task_id]["rate_limit_mb_s"] = rate_limit_mb_s
        STATE.service_records[task_id]["tls_terminate"] = tls_terminate
        STATE.service_records[task_id]["session_replay"] = session_replay
        STATE.service_records[task_id]["shared_tunnel"] = shared_tunnel
        STATE.service_records[task_id]["protocol"] = protocol

    if protocol == "udp":
        host_port = await ensure_local_udp_listener(
            task_id, task.worker, container_port
        )
        port_map = {container_port: host_port}
    else:
        port_map = await ensure_local_listeners(task_id, task.worker)
        if not port_map:
            local_port = await ensure_local_listener(task_id, task.worker)
            port_map = {container_port: local_port}
    local_port = int(port_map.get(container_port, next(iter(port_map.values()))))
    cs = connection_string(kind, local_port, tls=tls_terminate)

    # Project every container port as its own connection
    # string so the UI can show one row per exposed port.
    port_strings = [
        {
            "container_port": int(cp),
            "host_port": int(hp),
            "connection_string": connection_string(kind, int(hp), tls=tls_terminate),
        }
        for cp, hp in sorted(port_map.items())
    ]

    await record_audit_event(
        "service_tunnel_started",
        actor=task_id,
        task_id=task_id,
        severity="info",
        details=(
            f"local_port={local_port} worker={task.worker} kind={kind} "
            f"ports={list(port_map.values())}"
        ),
    )
    return {
        "status": "ok",
        "task_id": task_id,
        "port": local_port,
        "container_port": container_port,
        "service_kind": kind,
        "connection_string": cs,
        "ports": port_strings,
    }


@router.get(
    "/services/{task_id}/inspector",
    dependencies=[Depends(verify_local_auth)],
    summary="HTTP inspector ring buffer (last 100 entries)",
    tags=["Services"],
)
async def inspect_http_service(task_id: str) -> dict:
    ring = STATE.service_http_inspector.get(task_id)
    entries = list(ring) if ring is not None else []
    return {"task_id": task_id, "entries": entries, "count": len(entries)}


@router.get(
    "/services/{task_id}/replay",
    dependencies=[Depends(verify_local_auth)],
    summary="session-replay byte ring (1 MB cap, opt-in)",
    tags=["Services"],
)
async def replay_service(task_id: str) -> dict:
    import base64

    state = STATE.service_replay_buffers.get(task_id) or {}
    entries = state.get("entries") or []
    return {
        "task_id": task_id,
        "total_bytes": int(state.get("total", 0) or 0),
        "entries": [
            {
                "ts": ts,
                "direction": direction,
                "b64": base64.b64encode(chunk).decode("ascii"),
                "bytes": len(chunk),
            }
            for ts, direction, chunk in entries
        ],
    }


@router.post(
    "/services/{task_id}/stop",
    dependencies=[Depends(verify_local_auth)],
    summary="Close the local listener and ask the worker to stop the container",
    tags=["Services"],
)
async def stop_service_tunnel(task_id: str) -> dict:
    from nexus.networking.tunnel import close_local_listener

    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Task not found")

    await close_local_listener(task_id)

    # Tell the worker to stop the service container.
    if task.worker:
        try:
            from nexus.networking.tunnel import _send_to_peer

            await _send_to_peer(
                task.worker,
                {"type": "service_stop", "task_id": task_id, "reason": "manual"},
            )
        except Exception:
            pass

    await record_audit_event(
        "service_tunnel_stopped",
        actor=task_id,
        task_id=task_id,
        severity="info",
        details=f"worker={task.worker or ''}",
    )
    return {"status": "ok", "task_id": task_id}


# ---------------------------------------------------------------------------
# /local/update — central signed auto-update
# ---------------------------------------------------------------------------

@router.get(
    "/update/check",
    dependencies=[Depends(verify_local_auth)],
    summary="Check the signed release manifest for a newer version",
    tags=["Settings"],
)
async def local_update_check() -> dict:
    from nexus.runtime import updater

    return await updater.check()


@router.post(
    "/update/apply",
    dependencies=[Depends(verify_local_auth)],
    summary="Download the verified update and relaunch into it",
    tags=["Settings"],
)
async def local_update_apply() -> dict:
    from nexus.runtime import updater

    try:
        return await updater.apply()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---------------------------------------------------------------------------
# /local/relay_status
# ---------------------------------------------------------------------------

@router.get(
    "/relay_status",
    dependencies=[Depends(verify_local_auth)],
    summary="Get relay connection status",
    tags=["Settings"],
)
async def local_relay_status() -> dict:
    return {
        "connected": STATE.relay_connected,
        "relay_url": get_relay_url(),
        "relay_peers": list(STATE.relay_peers.keys()),
        "relay_peer_count": len(STATE.relay_peers),
        "last_error": STATE.relay_last_error,
    }


# ---------------------------------------------------------------------------
# Consent UI endpoints (worker side)
# ---------------------------------------------------------------------------

@router.get(
    "/pending_offers",
    dependencies=[Depends(verify_local_auth)],
    summary="List pending consent task offers",
    tags=["Task Lifecycle"],
)
async def local_pending_offers() -> dict:
    """Task offers awaiting user consent on this worker (UI poll target)."""
    async with STATE.worker_pending_offers_lock:
        offers = []
        for tid, rec in STATE.worker_pending_offers.items():
            elapsed = time.time() - rec["received_at"]
            remaining = max(0, rec["timeout"] - elapsed)
            offers.append(
                {
                    "task_id": tid,
                    "master_ip": rec["master_ip"],
                    "offer": rec["offer_data"],
                    "remaining_sec": round(remaining, 1),
                    "decision": rec["decision"],
                }
            )
    return {"offers": offers}


@router.post(
    "/consent_respond/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Accept or decline a pending task offer",
    tags=["Task Lifecycle"],
)
async def local_consent_respond(
    task_id: str, decision: str = Form(...)
) -> dict:
    async with STATE.worker_pending_offers_lock:
        offer = STATE.worker_pending_offers.get(task_id)
        if not offer:
            return {
                "status": "error",
                "message": "No pending offer for this task.",
            }
        if decision not in ("accept", "decline"):
            return {
                "status": "error",
                "message": "Decision must be 'accept' or 'decline'.",
            }
        offer["decision"] = decision
        offer["decision_event"].set()
    return {"status": "ok", "decision": decision}


# ---------------------------------------------------------------------------
# /local/shutdown
# ---------------------------------------------------------------------------

@router.post(
    "/shutdown",
    dependencies=[Depends(verify_local_auth)],
    summary="Gracefully shut down the node",
)
async def graceful_shutdown() -> dict:
    """Flip the node offline, preempt active tasks, persist settings."""
    from nexus.runtime.worker_state import (
        _LOCAL_WORKER_STATE,
        preempt_running_task,
    )

    # P8: notify peers about every in-flight foreign-storage deposit so
    # they classify the upcoming silence as a graceful pause (not a
    # send_failed). Best-effort; runs BEFORE we trigger anything else so
    # the WS pipes are still up.
    try:
        await _broadcast_storage_pause_on_shutdown()
    except Exception:
        pass

    LOCAL_SETTINGS["node_online"] = False
    async with STATE.worker_state_lock:
        active_tids = [
            t["task_id"] for t in _LOCAL_WORKER_STATE["active_tasks"]
        ]
    for tid in active_tids:
        await preempt_running_task(tid)
    await save_local_settings_to_db()
    await write_audit_event(
        "node_shutdown",
        actor=get_node_identity(),
        details="Graceful shutdown initiated.",
    )
    return {
        "status": "ok",
        "message": "Node going offline. Tasks preempted.",
    }


async def _broadcast_storage_pause_on_shutdown() -> None:
    """P8: emit one ``storage_pause`` frame per in-flight deposit so the
    other side flips the row to ``paused_<role>_shutdown`` immediately
    instead of waiting for the silence timeout to fire.

    Sends from both roles: depositor rows that are still ``transferring``
    or ``paused_*`` get a pause toward the host; host rows do the same
    toward the depositor. Failures are silently absorbed — we are
    shutting down anyway, the receiver will fall back to its silence-
    timeout detection if delivery fails.
    """
    from nexus.networking.storage_pump import build_storage_pause
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session

    in_flight_states = (
        "transferring",
        "paused_send_failed",
        "paused_silent",
        "paused_host_shutdown",
        "paused_host_down",
        "paused_depositor_shutdown",
        "paused_depositor_down",
    )
    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.status.in_(in_flight_states)
                    )
                )
            )
            .scalars()
            .all()
        )
    for row in rows:
        try:
            if row.role == "depositor":
                if not row.host_uuid:
                    continue
                await _send_to_peer(
                    row.host_uuid,
                    build_storage_pause(row.deposit_id, reason="depositor_shutdown"),
                )
            elif row.role == "host":
                if not row.depositor_uuid:
                    continue
                await _send_to_peer(
                    row.depositor_uuid,
                    build_storage_pause(row.deposit_id, reason="host_shutdown"),
                )
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Cache admin — venv / node_modules / pip wheel cache
# ---------------------------------------------------------------------------

@router.get(
    "/venv_cache_info",
    dependencies=[Depends(verify_local_auth)],
    summary="Report venv, node_modules, and pip wheel cache usage",
)
async def local_venv_cache_info() -> dict:
    """Return per-entry sizes for each of the three shared caches."""
    root = str(venv_cache_root())
    entries = []
    total_bytes = 0
    try:
        for name in os.listdir(root):
            entry_path = os.path.join(root, name)
            if not os.path.isdir(entry_path):
                continue
            size = dir_size_bytes(entry_path)
            total_bytes += size
            entries.append({"key": name, "size_mb": round(size / (1024 * 1024), 1)})
    except FileNotFoundError:
        pass

    node_root = str(node_cache_root())
    node_entries = []
    node_total = 0
    try:
        for name in os.listdir(node_root):
            entry_path = os.path.join(node_root, name)
            if not os.path.isdir(entry_path):
                continue
            size = dir_size_bytes(entry_path)
            node_total += size
            node_entries.append(
                {"key": name, "size_mb": round(size / (1024 * 1024), 1)}
            )
    except FileNotFoundError:
        pass

    pip_root = str(pip_wheel_cache_dir())
    wheels = []
    pip_bytes = 0
    try:
        for dp, _dn, fns in os.walk(pip_root):
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                pip_bytes += sz
                if fn.endswith(".whl"):
                    # Wheel filename convention: {dist}-{version}-{py}-{abi}-{plat}.whl
                    parts = fn.split("-")
                    pkg = parts[0] if parts else fn
                    ver = parts[1] if len(parts) > 1 else ""
                    wheels.append(
                        {
                            "file": fn,
                            "package": pkg,
                            "version": ver,
                            "size_mb": round(sz / (1024 * 1024), 2),
                            "rel_path": os.path.relpath(fp, pip_root),
                        }
                    )
    except Exception:
        pass
    wheels.sort(key=lambda w: (w["package"].lower(), w["version"]))

    return {
        "enabled": bool(LOCAL_SETTINGS.get("cache_venvs", False)),
        "uv_available": bool(detect_uv()),
        "root": root,
        "entries": entries,
        "total_mb": round(total_bytes / (1024 * 1024), 1),
        "node_root": node_root,
        "node_entries": node_entries,
        "node_total_mb": round(node_total / (1024 * 1024), 1),
        "pip_cache_root": pip_root,
        "pip_cache_mb": round(pip_bytes / (1024 * 1024), 1),
        "pip_wheels": wheels,
    }


@router.post(
    "/clear_venv_cache",
    dependencies=[Depends(verify_local_auth)],
    summary="Clear cached virtual environments or packages",
)
async def local_clear_venv_cache(scope: str = Form("venv")) -> dict:
    """Wipe cache entries. ``scope`` ∈ ``venv | node | pip | all``."""
    removed = 0
    if scope in ("venv", "all"):
        root = str(venv_cache_root())
        try:
            for name in os.listdir(root):
                ep = os.path.join(root, name)
                if os.path.isdir(ep):
                    await asyncio.to_thread(shutil.rmtree, ep, ignore_errors=True)
                    removed += 1
        except FileNotFoundError:
            pass
    if scope in ("node", "all"):
        root = str(node_cache_root())
        try:
            for name in os.listdir(root):
                ep = os.path.join(root, name)
                if os.path.isdir(ep):
                    await asyncio.to_thread(shutil.rmtree, ep, ignore_errors=True)
                    removed += 1
        except FileNotFoundError:
            pass
    if scope in ("pip", "all"):
        root = str(pip_wheel_cache_dir())
        try:
            for name in os.listdir(root):
                ep = os.path.join(root, name)
                if os.path.isdir(ep):
                    await asyncio.to_thread(shutil.rmtree, ep, ignore_errors=True)
                    removed += 1
                else:
                    try:
                        os.remove(ep)
                        removed += 1
                    except OSError:
                        pass
        except FileNotFoundError:
            pass
    return {"status": "ok", "removed": removed}


@router.post(
    "/delete_cache_entry",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete a specific cache entry by kind and key",
)
async def local_delete_cache_entry(
    kind: str = Form(...), key: str = Form(...)
) -> dict:
    """Delete a specific cache entry.

    ``kind`` ∈ ``venv | node | pip_wheel | pip_package``.

    * ``venv`` / ``node`` — *key* is a hash directory name.
    * ``pip_wheel`` — *key* is a ``rel_path`` returned by ``venv_cache_info``.
    * ``pip_package`` — *key* is a package name; every matching wheel is removed.
    """
    # Traversal guard (pip_wheel is exempt because rel_path may contain separators)
    if ".." in key or key.startswith("/") or key.startswith("\\") or ":" in key[:3]:
        if kind != "pip_wheel":
            raise HTTPException(400, detail="Invalid key.")

    removed = 0
    if kind == "venv":
        target = os.path.join(str(venv_cache_root()), os.path.basename(key))
        if os.path.isdir(target):
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            removed = 1
    elif kind == "node":
        target = os.path.join(str(node_cache_root()), os.path.basename(key))
        if os.path.isdir(target):
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
            removed = 1
    elif kind == "pip_wheel":
        root = str(pip_wheel_cache_dir())
        target = os.path.normpath(os.path.join(root, key))
        if os.path.commonpath([root, target]) != os.path.normpath(root):
            raise HTTPException(400, detail="Path escapes pip cache root.")
        if os.path.isfile(target):
            try:
                os.remove(target)
                removed = 1
            except OSError:
                pass
    elif kind == "pip_package":
        pkg = key.lower().replace("-", "_")
        root = str(pip_wheel_cache_dir())
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if not fn.endswith(".whl"):
                    continue
                dist = fn.split("-", 1)[0].lower().replace("-", "_")
                if dist == pkg:
                    try:
                        os.remove(os.path.join(dp, fn))
                        removed += 1
                    except OSError:
                        pass
    else:
        raise HTTPException(400, detail=f"Unknown kind: {kind}")
    return {"status": "ok", "removed": removed}


@router.post(
    "/prewarm_venv",
    dependencies=[Depends(verify_local_auth)],
    summary="Start a background venv prewarm job",
)
async def local_prewarm_venv(requirements: str = Form(...)) -> dict:
    """Fire off an async venv build; returns a ``job_id`` to poll."""
    req_text = requirements.strip()
    if not req_text:
        raise HTTPException(400, detail="requirements body is empty")
    job_id = uuid.uuid4().hex[:12]
    prewarm_job_set(job_id, status="queued")
    asyncio.create_task(run_prewarm(job_id, req_text))
    return {"status": "ok", "job_id": job_id}


@router.get(
    "/prewarm_status/{job_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Poll prewarm job progress",
)
async def local_prewarm_status(job_id: str) -> dict:
    job = PREWARM_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail="No such job.")
    return {
        "status": job.get("status", "unknown"),
        "log": job.get("log", ""),
        "key": job.get("key"),
        "elapsed_sec": round(time.time() - job.get("started_at", time.time()), 1),
    }


# ---------------------------------------------------------------------------
# Task-log tail + scan-imports + workflow deployment
# ---------------------------------------------------------------------------

@router.get(
    "/task_log_tail/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Stream task log tail with cursor pagination",
    tags=["Task Lifecycle"],
)
async def local_task_log_tail(task_id: str, since: int = 0) -> dict:
    lines, cursor = await task_log_tail(task_id, since)
    return {"task_id": task_id, "lines": lines, "cursor": cursor}


@router.post(
    "/task_log_tail/{task_id}/save",
    dependencies=[Depends(verify_local_auth)],
    summary="B4: persist the current live log buffer as a result artifact",
    tags=["Task Lifecycle"],
)
async def local_save_task_log(task_id: str) -> dict:
    """Snapshot the task/service live log buffer into its result bundle so it
    appears in the result/artifact browser. Services have no completed-task
    bundle, so this is how their streamed logs become a saved artifact."""
    from nexus.runtime import result_browser

    lines, _ = await task_log_tail(task_id, 0)
    if not lines:
        # Completed tasks keep no live buffer — fall back to the persisted log
        # so "Save as artifact" still works after the task has finished.
        async with get_session() as db:
            rec = await db.get(TaskRecord, task_id)
        stored = (rec.logs if rec else "") or ""
        lines = stored.split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
    if not lines:
        raise HTTPException(404, detail="No log to save for this task.")
    name = result_browser.write_log_artifact(task_id, lines)
    return {"message": f"Saved {len(lines)} log lines as artifact {name}.", "file": name}


@router.post(
    "/scan_imports",
    dependencies=[Depends(verify_local_auth)],
    summary="Scan uploaded workspace for dependencies",
    tags=["Task Lifecycle"],
)
async def local_scan_imports(
    request: Request,
    file: UploadFile = File(...),
    entrypoint: str = Form("python main.py"),
) -> dict:
    """Extract and scan a workspace zip for Python/JS/C++ dependencies."""
    max_bytes = get_max_result_bytes()
    enforce_content_length(request, max_bytes, label="Workspace zip")
    zip_bytes = await file.read()
    enforce_actual_size(zip_bytes, max_bytes, label="Workspace zip")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                safe_extractall(zf, tmp)
        except Exception as e:
            raise HTTPException(400, detail=f"Invalid zip file: {e}")
        return scan_workspace_dependencies(tmp, entrypoint)


@router.post(
    "/add_workflow",
    dependencies=[Depends(verify_local_auth)],
    summary="Deploy a task or DAG workflow to the grid",
    tags=["Task Lifecycle"],
)
async def local_add_workflow(
    request: Request,
    workflow_id: str = Form(...),
    workflow_json: str = Form(...),
    file: UploadFile | None = File(None),
    preferred_workers: str = Form("[]"),
    preferred_worker: str = Form(""),
    target_groups: str = Form("[]"),
    blocked_members: str = Form("[]"),
    priority: int = Form(50),
    retry_max: int = Form(2),
    required_tags: str = Form(""),
    require_gpu: bool = Form(False),
    preferred_region: str = Form(""),
    orphan_policy: str = Form("retry"),
    queue_timeout_sec: int = Form(0),
    lease_seconds: int = Form(0),
    retry_backoff_base_sec: int = Form(0),
    one_step_per_node: bool = Form(False),
    prefer_reliable_workers: str = Form(""),
    step_gate: str = Form(""),
) -> dict:
    """Expand a DAG JSON, zip per-task bundles, insert rows, enqueue ready tasks."""
    max_bytes = get_max_result_bytes()
    enforce_content_length(request, max_bytes, label="Workflow zip")
    priority = max(0, min(100, int(priority or 50)))
    retry_max = max(1, min(6, int(retry_max or 2)))
    # Reliability override is tri-state: "" inherits the node default, otherwise
    # an explicit on/off for this whole dispatch (task / service / DAG).
    _pr = str(prefer_reliable_workers or "").strip().lower()
    prefer_reliable = None if _pr == "" else _pr in ("1", "true", "yes", "on")
    # Step gate is the same tri-state: "" inherits the node default, otherwise an
    # explicit on/off holding each DAG level for approval before the next runs.
    _sg = str(step_gate or "").strip().lower()
    gate = None if _sg == "" else _sg in ("1", "true", "yes", "on")
    # Per-dispatch scheduler overrides; 0 = this node's settings apply.
    lease_seconds = max(0, min(3600, int(lease_seconds or 0)))
    retry_backoff_base_sec = max(0, min(600, int(retry_backoff_base_sec or 0)))
    workflow = json.loads(workflow_json)
    # File upload is optional when every task supplies a
    # workspace_source — the worker fetches the workspace from cloud
    # post-extract. We synthesise an empty zip stub so the existing
    # repackage loop below still produces a valid bundle with task.json.
    if file is not None:
        zip_bytes = await file.read()
        enforce_actual_size(zip_bytes, max_bytes, label="Workflow zip")
    else:
        zip_bytes = b""
    expanded_tasks: list[dict] = []
    slice_map: dict[str, list[str]] = {}
    requested_tags = split_csv(required_tags)

    # Normalize preferred_workers: accept JSON array or comma-separated
    try:
        parsed_targets = json.loads(preferred_workers or "[]")
        requested_targets = (
            [str(ip).strip() for ip in parsed_targets if str(ip).strip()]
            if isinstance(parsed_targets, list)
            else []
        )
    except Exception:
        requested_targets = [
            ip.strip() for ip in str(preferred_workers or "").split(",") if ip.strip()
        ]

    legacy_target = preferred_worker.strip()
    if legacy_target:
        requested_targets.append(legacy_target)
    requested_targets = list(dict.fromkeys(requested_targets))

    # Group-scoped compute. Empty = grid-wide (default behaviour).
    try:
        parsed_groups = json.loads(target_groups or "[]")
        requested_groups = (
            [str(g).strip() for g in parsed_groups if str(g).strip()]
            if isinstance(parsed_groups, list)
            else []
        )
    except Exception:
        requested_groups = [
            g.strip() for g in str(target_groups or "").split(",") if g.strip()
        ]
    requested_groups = list(dict.fromkeys(requested_groups))

    # Per-dispatch blocked members (node UUIDs excluded from the
    # group pool for this task only). Empty = nobody blocked.
    try:
        parsed_blocked = json.loads(blocked_members or "[]")
        requested_blocked = (
            [str(m).strip() for m in parsed_blocked if str(m).strip()]
            if isinstance(parsed_blocked, list)
            else []
        )
    except Exception:
        requested_blocked = [
            m.strip() for m in str(blocked_members or "").split(",") if m.strip()
        ]
    requested_blocked = list(dict.fromkeys(requested_blocked))

    # Per-step targeting: each step may override the dispatch-level targets /
    # overrides; resolve once (1:1 with workflow) so we can validate the union
    # of requested workers and reuse the merged values during expansion.
    from nexus.tasks.step_targeting import resolve_step_targeting

    _dispatch_defaults = {
        "preferred_workers": requested_targets,
        "target_groups": requested_groups,
        "blocked_members": requested_blocked,
        "required_tags": requested_tags,
        "require_gpu": require_gpu,
        "preferred_region": preferred_region,
        "priority": priority,
        "retry_max": retry_max,
        "retry_backoff_base": retry_backoff_base_sec or None,
        "lease_seconds": lease_seconds or None,
        "queue_timeout_sec": queue_timeout_sec,
        "orphan_policy": orphan_policy,
    }
    eff_targeting = [resolve_step_targeting(t, _dispatch_defaults) for t in workflow]

    # Every requested worker (dispatch-level or per-step) must be a trusted peer
    all_targets = sorted(
        {w for eff in eff_targeting for w in (eff.get("preferred_workers") or [])}
    )
    if all_targets:
        async with get_session() as db:
            trusted_peers = (
                (
                    await db.execute(
                        select(Peer).filter(
                            Peer.ip.in_(all_targets),
                            Peer.status == "trusted",
                            Peer.role.in_(["worker", "dual"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            trusted_ids = {peer.ip for peer in trusted_peers}
            invalid_targets = [w for w in all_targets if w not in trusted_ids]
            if invalid_targets:
                raise HTTPException(
                    400,
                    detail=(
                        "Selected worker is not a trusted compute peer: "
                        + ", ".join(invalid_targets)
                    ),
                )

    # Expand the DAG JSON into per-task dicts with slice handling
    for idx, t in enumerate(workflow):
        eff = eff_targeting[idx]
        base_id = f"{workflow_id}_{t['id']}"
        deps: list[str] = []
        for d in t.get("depends_on", []):
            parent_base = f"{workflow_id}_{d}"
            if parent_base in slice_map:
                deps.extend(slice_map[parent_base])
            else:
                deps.append(parent_base)

        slice_count = t.get("slice_count", 1)
        manifest = {
            "entrypoint": t.get("entrypoint"),
            "runtime": t.get("runtime", "docker"),
            "image": t.get("image", ""),
            "setup_cmd": t.get("setup_cmd", ""),
            "ram_limit_mb": t.get("ram_limit", 1024),
            "cpu_limit_pct": t.get("cpu_limit", 100),
            "cloud_uri": t.get("cloud_uri", ""),
        }
        # Per-task capability requests. The worker's own settings stay the
        # ceiling/floor: network only runs if the worker allows network
        # tasks; isolation/no-cache/scanning/profile can only tighten.
        # Cross-region is the dispatcher's own preference (both ways).
        for key in (
            "network_required", "require_venv_isolation", "no_venv_cache",
            "enable_task_scanning", "allow_cross_region",
        ):
            if key in t:
                manifest[key] = bool(t[key])
        if str(t.get("security_profile") or "").strip() in ("standard", "maximum"):
            manifest["security_profile"] = str(t["security_profile"]).strip()
        # Pass through cloud task-data sources for both batch and
        # service runtimes. Validate now so the user gets the error at
        # submit time, not on the worker mid-fetch.
        if "data_sources" in t:
            manifest["data_sources"] = t["data_sources"]
        if "workspace_source" in t:
            manifest["workspace_source"] = t["workspace_source"]
        try:
            normalised_sources = validate_data_sources(manifest)
        except ServiceManifestError as exc:
            raise HTTPException(
                400, detail=f"task {t.get('id')!r}: {exc}"
            )
        manifest["data_sources"] = normalised_sources["data_sources"]
        if normalised_sources["workspace_source"]:
            manifest["workspace_source"] = normalised_sources["workspace_source"]
        # Audit attachment so depositors see which tasks reference
        # cloud data — separate from the dispatch-time transmit event.
        attached_count = len(normalised_sources["data_sources"]) + (
            1 if normalised_sources["workspace_source"] else 0
        )
        if attached_count:
            # Depositor must accept the current task-data terms
            # before any task with cloud sources is dispatched. Bumping the
            # version constant forces re-acceptance.
            from nexus.security.task_data_terms import (
                current_terms_text,
                current_version,
                is_current_accepted,
            )
            if not is_current_accepted():
                raise HTTPException(
                    status_code=412,
                    detail={
                        "message": "Accept task-data terms before dispatching cloud-sourced tasks",
                        "version": current_version(),
                        "terms": current_terms_text(),
                    },
                )
            await write_audit_event(
                "task.data_source_attached",
                actor=get_node_identity(),
                task_id=base_id,
                details=f"sources={attached_count}",
            )
        # Pass through service-runtime fields so users can
        # submit redis/postgres/etc via the same /local/add_workflow path.
        if str(manifest["runtime"]).lower() == "service":
            for key in (
                "expose_ports",
                "duration_sec",
                "idle_timeout_sec",
                "service_kind",
                "network_required",
                "replicas",
                "replica_strategy",
                "snapshot_interval_sec",
                "snapshot_paths",
                "primary_selection",
                "depends_on",
                "environment",
                "rate_limit_mb_s",
                "tls_terminate",
                "session_replay",
                "shared_tunnel",
                "protocol",
            ):
                if key in t:
                    manifest[key] = t[key]

        if slice_count > 1:
            slice_map[base_id] = []
            for i in range(slice_count):
                sub_id = f"{base_id}_p{i}"
                slice_map[base_id].append(sub_id)
                expanded_tasks.append(
                    {
                        "id": sub_id,
                        "parent_id": workflow_id,
                        "depends_on": ",".join(deps),
                        "status": "waiting" if deps else "queued",
                        "env": build_task_metadata(
                            {
                                "NEXUS_SLICE_INDEX": str(i),
                                "NEXUS_SLICE_TOTAL": str(slice_count),
                            },
                            coordination_role="requested",
                            requested_by=get_node_identity(),
                            display_id=sub_id,
                            preferred_workers=eff.get("preferred_workers") or None,
                            target_groups=eff.get("target_groups") or None,
                            blocked_members=eff.get("blocked_members") or None,
                            priority=eff.get("priority", priority),
                            retry_max=eff.get("retry_max", retry_max),
                            retry_backoff_base=eff.get("retry_backoff_base") or None,
                            lease_seconds=eff.get("lease_seconds") or None,
                            required_tags=eff.get("required_tags"),
                            require_gpu=eff.get("require_gpu", require_gpu),
                            preferred_region=eff.get("preferred_region", preferred_region),
                            orphan_policy=eff.get("orphan_policy", orphan_policy),
                            queue_timeout_sec=eff.get("queue_timeout_sec", queue_timeout_sec),
                            one_step_per_node=one_step_per_node,
                            prefer_reliable_workers=prefer_reliable,
                            step_gate=gate,
                        ),
                        "manifest": manifest,
                    }
                )
        else:
            slice_map[base_id] = [base_id]
            expanded_tasks.append(
                {
                    "id": base_id,
                    "parent_id": workflow_id,
                    "depends_on": ",".join(deps),
                    "status": "waiting" if deps else "queued",
                    "env": build_task_metadata(
                        {},
                        coordination_role="requested",
                        requested_by=get_node_identity(),
                        display_id=base_id,
                        preferred_workers=eff.get("preferred_workers") or None,
                        target_groups=eff.get("target_groups") or None,
                        blocked_members=eff.get("blocked_members") or None,
                        priority=eff.get("priority", priority),
                        retry_max=eff.get("retry_max", retry_max),
                        retry_backoff_base=eff.get("retry_backoff_base") or None,
                        lease_seconds=eff.get("lease_seconds") or None,
                        required_tags=eff.get("required_tags"),
                        require_gpu=eff.get("require_gpu", require_gpu),
                        preferred_region=eff.get("preferred_region", preferred_region),
                        orphan_policy=eff.get("orphan_policy", orphan_policy),
                        queue_timeout_sec=eff.get("queue_timeout_sec", queue_timeout_sec),
                        one_step_per_node=one_step_per_node,
                        prefer_reliable_workers=prefer_reliable,
                        step_gate=gate,
                    ),
                    "manifest": manifest,
                }
            )

    # If no zip was uploaded, every task must supply a
    # workspace_source — otherwise the worker has nothing to run.
    if not zip_bytes:
        missing = [
            et["id"]
            for et in expanded_tasks
            if not et["manifest"].get("workspace_source")
        ]
        if missing:
            raise HTTPException(
                400,
                detail=(
                    "Workflow zip is required when any task lacks "
                    "workspace_source: missing on " + ", ".join(missing)
                ),
            )
        # Synthesize a minimal in-memory zip so the per-task repackage loop
        # below has something to read from.
        stub_io = io.BytesIO()
        with zipfile.ZipFile(stub_io, "w"):
            pass
        zip_bytes = stub_io.getvalue()

    # Persist + enqueue. Reject up-front if any expanded task ID already
    # exists so the user gets a clear 409 instead of an opaque IntegrityError.
    requested_ids = [et["id"] for et in expanded_tasks]
    async with get_session() as db:
        existing = (
            (
                await db.execute(
                    select(TaskRecord.id).filter(TaskRecord.id.in_(requested_ids))
                )
            )
            .scalars()
            .all()
        )
    if existing:
        raise HTTPException(
            409,
            detail=(
                f"Task ID(s) already exist: {', '.join(sorted(existing))}. "
                "Pick a different workflow ID, or delete/clone the existing task."
            ),
        )

    async with get_session() as db:
        for et in expanded_tasks:
            task_image = str(et["manifest"].get("image", "")).strip()
            if (
                et["manifest"].get("runtime", "docker") == "docker"
                and task_image
                and not image_allowed(task_image)
            ):
                raise HTTPException(
                    400, detail=f"Image '{task_image}' blocked by allowlist policy."
                )
            in_zip = io.BytesIO(zip_bytes)
            out_zip_io = io.BytesIO()
            with zipfile.ZipFile(in_zip, "r") as in_z:
                with zipfile.ZipFile(out_zip_io, "w") as out_z:
                    for item in in_z.namelist():
                        out_z.writestr(item, in_z.read(item))
                    out_z.writestr("task.json", json.dumps(et["manifest"]))

            payload_bytes = out_zip_io.getvalue()
            et["env"]["NEXUS_META_PAYLOAD_SIG"] = sign_bytes(
                "task_bundle", et["id"], payload_bytes
            )
            log_str = (
                f"[{timestamp()}] [MASTER] Sliced & Queued. "
                f"Runtime: {et['manifest'].get('runtime')}\n"
                if et["status"] == "queued"
                else (
                    f"[{timestamp()}] [DAG PLANNER] Task registered. "
                    f"WAITING on ({et['depends_on']}).\n"
                )
            )
            db.add(
                TaskRecord(
                    id=et["id"],
                    parent_id=et["parent_id"],
                    status=et["status"],
                    depends_on=et["depends_on"],
                    env_vars=json.dumps(et["env"]),
                    payload=payload_bytes,
                    logs=log_str,
                )
            )

            if et["status"] == "queued":
                await enqueue_task(et["id"])
                incr_metric("tasks_queued")
        await db.commit()

        await ws_manager.broadcast_ping()
        await write_audit_event(
            "workflow_deployed",
            actor=get_node_identity(),
            task_id=workflow_id,
            details=(
                f"tasks={len(expanded_tasks)}, priority={priority}, "
                f"retry_max={retry_max}"
            ),
        )
    # Consumption is no longer self-counted at dispatch — it is
    # recorded from the counterparty-signed usage receipt when the task
    # completes (see issue_compute_receipt). This keeps pool numbers tamper-proof.
    return {"message": f"DAG Deployed: {len(expanded_tasks)} sub-tasks generated."}


# ---------------------------------------------------------------------------
# /local/network — the big one
# ---------------------------------------------------------------------------

_WORKER_DEFAULTS: dict = {
    "status": "offline",
    "online": False,
    "cpu": 0,
    "ram": 0,
    "free_ram": 0,
    "dispatch_ram_cap_mb": 0,
    "active_task": None,
    "active_tasks": [],
    "serving_master": None,
    "serving_masters": [],
    "connected_masters": [],
    "connected_master_count": 0,
    "active_task_count": 0,
    "last_update": 0,
    "last_result_status": None,
    "last_result_at": 0,
    "last_result_master": None,
    "capabilities": {},
    "node_identity": "",
}


@router.get(
    "/network",
    dependencies=[Depends(verify_local_auth)],
    summary="Full network state snapshot with delta support",
    tags=["Diagnostics"],
)
async def local_get_network_graph(since: int = 0) -> dict:
    """Single-stop payload for the UI: tasks, workers, settings, metrics, alerts."""
    cache = _get_network_cache()
    now = time.time()
    current_rev = cache["revision"]

    # Client-side delta: if caller already has this revision and cache is warm,
    # short-circuit. Invalidation (ts=0.0 by broadcast_ui_update) forces rebuild.
    if (
        since > 0
        and since >= current_rev
        and cache["data"] is not None
        and (now - cache["ts"]) < NETWORK_CACHE_TTL
    ):
        return {"revision": current_rev, "unchanged": True}
    if cache["data"] is not None and (now - cache["ts"]) < NETWORK_CACHE_TTL:
        return cache["data"]

    # --- Tasks --------------------------------------------------------
    async with get_session() as db:
        tasks: dict[str, dict] = {}
        for t in (await db.execute(select(TaskRecord))).scalars().all():
            metadata = extract_task_metadata(t)
            tasks[t.id] = {
                "display_id": metadata["display_id"],
                "parent_id": t.parent_id or "",
                "status": t.status,
                "worker": t.worker,
                "logs": t.logs,
                "coordination": metadata["coordination"],
                "requested_by": metadata["requested_by"],
                "preferred_worker": metadata["preferred_worker"],
                "preferred_workers": metadata["preferred_workers"],
                "target_groups": metadata["target_groups"],
                "priority": metadata["priority"],
                "retry_max": metadata["retry_max"],
                "retry_count": metadata["retry_count"],
                "timeline": metadata["timeline"],
                "started_at": metadata["started_at"],
                "completed_at": metadata["completed_at"],
                "elapsed_secs": metadata["elapsed_secs"],
                "coordination_text": metadata["coordination_text"],
                "has_download": metadata["has_download"],
                "can_requeue": metadata["coordination"] != "serving"
                and t.status in ("failed", "completed", "disrupted"),
                "can_cancel": metadata["coordination"] != "serving"
                and t.status
                in ("waiting", "queued", "retrying", "preempted", "lease_expired"),
                "can_disrupt": metadata["coordination"] != "serving"
                and t.status == "processing",
                "can_delete": t.status in TERMINAL_STATES,
                "can_preempt_local": metadata["coordination"] == "serving"
                and t.status == "processing",
            }
        trusted_compute_peers = (
            (
                await db.execute(
                    select(Peer).filter(
                        Peer.status == "trusted",
                        Peer.role.in_(["worker", "dual"]),
                    )
                )
            )
            .scalars()
            .all()
        )

    # --- Workers map: seed with offline defaults for every trusted peer ----
    workers: dict[str, dict] = {}
    for peer in trusted_compute_peers:
        workers[peer.ip] = {**_WORKER_DEFAULTS, "node_identity": peer.ip}

    # Overlay live heartbeat data
    for ip, data in STATE.active_workers.items():
        stats = dict(data.get("stats", {}))
        stats["online"] = True
        stats["last_seen"] = float(data.get("last_seen", 0) or 0)
        base = workers.get(ip, {**_WORKER_DEFAULTS, "node_identity": ip})
        workers[ip] = {**base, **stats}

    # Overlay presence: a peer that is known-online via any liveness signal
    # (UDP beacon, WS handshake, relay peer_list, FS probe) but isn't
    # currently heartbeating as a compute worker should still render as
    # online in the topology. Without this, foreign-storage-only pairings
    # (and single-machine multi-node test setups where only one process
    # can bind the discovery port) show every peer as permanently offline.
    for ip in list(workers.keys()):
        if workers[ip].get("online"):
            continue
        entry = STATE.peer_presence.get(ip) or {}
        if entry.get("status") == "online":
            workers[ip] = {
                **workers[ip],
                "online": True,
                "status": workers[ip].get("status") or "idle",
                "last_seen": float(entry.get("last_seen", 0) or 0),
                "connection_type": workers[ip].get("connection_type") or "lan",
            }

    # display_ip (masked if peer hides profile)
    for ip in list(workers.keys()):
        peer_hidden = False
        _disc_uuid, _disc_entry = lookup_discovered_peer(ip)
        if _disc_entry and len(_disc_entry) > 4:
            peer_hidden = _disc_entry[4]
        workers[ip]["display_ip"] = MASKED_IP_PLACEHOLDER if peer_hidden else ip
        workers[ip]["hide_profile"] = peer_hidden

    # --- Local worker --------------------------------------------------
    local_worker = await get_local_worker_snapshot()
    local_worker["node_identity"] = get_node_identity()
    local_worker["cpu"] = psutil.cpu_percent(interval=None)
    local_worker["ram"] = psutil.virtual_memory().percent
    local_worker["free_ram"] = psutil.virtual_memory().available // (1024 * 1024)
    try:
        _proc = psutil.Process()
        local_worker["process_ram_mb"] = round(
            _proc.memory_info().rss / (1024 * 1024), 1
        )
    except Exception:
        local_worker["process_ram_mb"] = 0
    _, _local_dispatch_cap = get_dispatch_capacity_mb()
    local_worker["dispatch_ram_cap_mb"] = _local_dispatch_cap
    local_worker["user_display_name"] = str(
        LOCAL_SETTINGS.get("user_display_name", "") or ""
    )
    if detect_gpu():
        local_worker["gpu_stats"] = get_gpu_stats()
    local_worker["net_io"] = sample_net_bandwidth()
    # P6: include foreign-storage bytes this node is currently hosting for
    # other peers — the user's complaint was that "App MB" only showed
    # process RAM and ignored hosted FS bytes. Cached for 15 s so the
    # diagnostics refresh doesn't repeatedly walk the cache dir.
    try:
        local_worker["foreign_storage_hosted_mb"] = (
            await _foreign_storage_hosted_mb()
        )
    except Exception:
        local_worker["foreign_storage_hosted_mb"] = 0

    # --- peer_names map: identifier → display_name ---------------------
    from nexus.core.identity import _UUID_TO_IP  # module-private, intentional

    peer_names: dict[str, str] = {}
    try:
        my_name = str(LOCAL_SETTINGS.get("user_display_name", "") or "")
        if my_name:
            peer_names[get_node_identity()] = my_name
            peer_names[get_or_create_node_uuid()] = my_name
        for _wip, _wdata in STATE.active_workers.items():
            _wstats = _wdata.get("stats", {}) if isinstance(_wdata, dict) else {}
            _wname = str(_wstats.get("user_display_name", "") or "")
            if not _wname:
                continue
            peer_names[_wip] = _wname
            _wnid = _wstats.get("node_identity", "")
            if _wnid:
                peer_names[_wnid] = _wname
        # P6: also include DB-stored display names for every trusted peer,
        # so the diagnostics UI can show friendly labels for peers who are
        # currently offline (and therefore absent from active_workers).
        for _peer in trusted_compute_peers:
            _name = (_peer.display_name or "").strip()
            if _name and _peer.ip not in peer_names:
                peer_names[_peer.ip] = _name
        for _uuid, _ipport in _UUID_TO_IP.items():
            if _ipport in peer_names and _uuid not in peer_names:
                peer_names[_uuid] = peer_names[_ipport]
    except Exception:
        pass

    safe_settings = {k: v for k, v in LOCAL_SETTINGS.items() if k != "gdrive_key"}
    safe_settings["gdrive_key"] = "***" if LOCAL_SETTINGS.get("gdrive_key") else ""

    cache["revision"] += 1
    result = {
        "workers": workers,
        "tasks": tasks,
        "settings": safe_settings,
        "local_worker": local_worker,
        "metrics": dict(STATE.metrics),
        "alerts": list(STATE.alerts),
        "has_gpu": detect_gpu(),
        "peer_names": peer_names,
        "revision": cache["revision"],
    }
    cache["data"] = result
    cache["ts"] = time.time()
    return result


# ---------------------------------------------------------------------------
# Alerts / diagnostics / presence-history / audit / metrics / download
# ---------------------------------------------------------------------------

# P6: cache disk-walk results so the diagnostics tab refresh doesn't repeatedly
# stat thousands of chunk files. 15 s feels right — long enough to amortize a
# walk across a polling refresh, short enough to feel live to the user.
_DISK_CACHE: dict[str, dict | float] = {"ts": 0.0, "data": {}}


def _walk_dir_bytes(path) -> int:
    """Total bytes of every regular file under *path*, recursively.

    Synchronous on purpose — callers wrap in ``asyncio.to_thread`` so a
    large foreign-storage tree doesn't block the event loop.
    """
    from pathlib import Path as _P
    total = 0
    p = _P(path)
    if not p.exists():
        return 0
    for entry in p.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


async def _foreign_storage_hosted_mb() -> int:
    """Total disk bytes the foreign-storage tree consumes (cached 15 s)."""
    import asyncio as _asyncio
    from nexus.core import cache_dir, get_node_port

    fs_dir = cache_dir(get_node_port()) / "foreign_storage"
    cached = _DISK_CACHE.get("data") or {}
    if (
        time.time() - float(_DISK_CACHE.get("ts", 0) or 0) < 15
        and "foreign_storage" in cached
    ):
        return int(cached["foreign_storage"] / (1024 * 1024))
    fs_bytes = await _asyncio.to_thread(_walk_dir_bytes, fs_dir)
    cached["foreign_storage"] = fs_bytes
    _DISK_CACHE["data"] = cached
    _DISK_CACHE["ts"] = time.time()
    return int(fs_bytes / (1024 * 1024))


@router.get(
    "/disk_breakdown",
    dependencies=[Depends(verify_local_auth)],
    summary="P6: per-category bytes the app is using on disk",
    tags=["Diagnostics"],
)
async def local_disk_breakdown() -> dict:
    """Bucket the on-disk footprint of this node so the user can see where
    the bytes are going. Categories: foreign_storage (hosted ciphertext),
    foreign_storage_uploads (depositor-side staging), services (
    service-task workspaces), completed_tasks, and the SQLite DB.

    Cached for 15 s; walks can be slow on large caches.
    """
    import asyncio as _asyncio
    from pathlib import Path as _P
    from nexus.core import cache_dir, get_node_port
    from nexus.core.paths import BASE_DIR

    if time.time() - float(_DISK_CACHE.get("ts", 0) or 0) < 15:
        cached = dict(_DISK_CACHE.get("data") or {})
        if cached.get("complete"):
            return {"bytes_by_category": cached.get("bytes_by_category", {}),
                    "total_bytes": cached.get("total_bytes", 0)}

    port = get_node_port()
    cache_root = cache_dir(port)
    targets: dict[str, _P] = {
        "foreign_storage": cache_root / "foreign_storage",
        "foreign_storage_uploads": cache_root / "foreign_storage_uploads",
        "services": cache_root / "services",
        "completed_tasks": BASE_DIR / "completed_tasks",
    }
    sizes: dict[str, int] = {}
    for name, path in targets.items():
        sizes[name] = await _asyncio.to_thread(_walk_dir_bytes, path)

    # SQLite DB lives at <BASE_DIR>/nexus_mod_<port>.db (one file per node port).
    db_path = BASE_DIR / f"nexus_mod_{int(port)}.db"
    try:
        sizes["db"] = (
            db_path.stat().st_size if db_path.is_file() else 0
        )
    except OSError:
        sizes["db"] = 0

    total = sum(sizes.values())
    _DISK_CACHE["data"] = {
        # Keep the foreign_storage byte count fresh for the App-MB caller.
        "foreign_storage": sizes.get("foreign_storage", 0),
        "bytes_by_category": sizes,
        "total_bytes": total,
        "complete": True,
    }
    _DISK_CACHE["ts"] = time.time()
    return {"bytes_by_category": sizes, "total_bytes": total}


# ---- node-global pool-usage analytics (group_id="*") -----------

@router.get(
    "/pool_usage",
    dependencies=[Depends(verify_local_auth)],
    summary="Node-global pool-usage history across all groups",
    tags=["Diagnostics"],
)
async def local_pool_usage(range: str = "7d") -> dict:
    from nexus.runtime.group_compute_telemetry import (
        GLOBAL_GROUP, fetch_buckets, range_to_since,
    )
    buckets = await fetch_buckets(GLOBAL_GROUP, range_to_since(range))
    return {"range": range, "buckets": buckets}


@router.get(
    "/pool_usage/export",
    dependencies=[Depends(verify_local_auth)],
    summary="Export node-global pool-usage buckets (CSV/JSON)",
    tags=["Diagnostics"],
)
async def local_pool_usage_export(format: str = "json"):
    from nexus.runtime.group_compute_telemetry import (
        GLOBAL_GROUP, buckets_csv, fetch_buckets,
    )
    rows = await fetch_buckets(GLOBAL_GROUP)
    if format == "csv":
        return Response(
            content=buckets_csv(rows), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="pool_global.csv"'},
        )
    return {"buckets": rows}


@router.get(
    "/pool_usage/retention",
    dependencies=[Depends(verify_local_auth)],
    summary="Read pool-usage retention (days)",
    tags=["Diagnostics"],
)
async def local_pool_retention_get() -> dict:
    from nexus.runtime.group_compute_telemetry_rollup import DEFAULT_RETENTION_DAYS
    raw = LOCAL_SETTINGS.get("pool_telemetry_retention_days", DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return {"days": max(0, days)}


@router.post(
    "/pool_usage/retention",
    dependencies=[Depends(verify_local_auth)],
    summary="Set pool-usage retention (0 = unlimited)",
    tags=["Diagnostics"],
)
async def local_pool_retention_set(payload: dict) -> dict:
    days = max(0, min(3650, int(payload.get("days") or 0)))
    LOCAL_SETTINGS["pool_telemetry_retention_days"] = days
    await save_local_settings_to_db()
    return {"days": days}


@router.post(
    "/pool_usage/purge",
    dependencies=[Depends(verify_local_auth)],
    summary="Delete pool-usage buckets older than ``before`` (ISO8601)",
    tags=["Diagnostics"],
)
async def local_pool_usage_purge(payload: dict) -> dict:
    from nexus.runtime.group_compute_telemetry import GLOBAL_GROUP, purge_before

    before = str(payload.get("before") or "").strip()
    if not before:
        raise HTTPException(400, "'before' is required")
    # Purge both the global rollup and every per-group history this node holds.
    from nexus.storage.models import GroupComputeBucket
    async with get_session() as db:
        gids = (
            await db.execute(select(GroupComputeBucket.group_id).distinct())
        ).scalars().all()
    pruned = 0
    for gid in set(gids) | {GLOBAL_GROUP}:
        pruned += await purge_before(gid, before)
    return {"pruned": pruned, "before": before}


@router.get(
    "/alerts",
    dependencies=[Depends(verify_local_auth)],
    summary="Get active system alerts",
    tags=["Diagnostics"],
)
async def local_get_alerts() -> dict:
    return {"alerts": list(STATE.alerts)}


@router.get(
    "/diagnostics",
    dependencies=[Depends(verify_local_auth)],
    summary="Aggregated diagnostics: network state, alerts, and metrics",
    tags=["Diagnostics"],
)
async def local_diagnostics() -> dict:
    net = await local_get_network_graph()
    rollup = compute_cluster_rollup(net)
    issues = analyze_cluster_health(net)
    return {
        "local_worker": net.get("local_worker"),
        "workers": net.get("workers", {}),
        "rollup": rollup,
        "issues": issues,
        "recent_alerts": list(STATE.alerts)[-20:],
        "metrics": dict(STATE.metrics),
        # P6: ship the live identifier→display-name map so the renderer can
        # show friendly labels instead of raw nexus_ UUIDs. The map is built
        # by ``local_get_network_graph`` and now also includes trusted-peer
        # display_names for currently-offline peers.
        "peer_names": net.get("peer_names", {}),
    }


@router.get(
    "/presence_history",
    dependencies=[Depends(verify_local_auth)],
    summary="Query peer presence history timeline",
    tags=["Diagnostics"],
)
async def local_presence_history(peer: str = "", limit: int = 100) -> dict:
    limit = max(1, min(500, int(limit)))
    async with get_session() as db:
        q = select(PresenceEvent)
        if peer:
            q = q.filter(PresenceEvent.peer_ip == peer)
        rows = (await db.execute(q)).scalars().all()
    rows = sorted(rows, key=lambda r: float(r.ts or 0), reverse=True)[:limit]
    return {
        "events": [
            {
                "peer_ip": row.peer_ip,
                "status": row.status,
                "source": row.source,
                "ts": float(row.ts or 0),
            }
            for row in rows
        ]
    }


@router.get(
    "/audit",
    dependencies=[Depends(verify_local_auth)],
    summary="Retrieve audit event log",
    tags=["Diagnostics"],
)
async def local_get_audit(limit: int = 200) -> dict:
    limit = max(1, min(1000, int(limit)))
    async with get_session() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    rows = sorted(rows, key=lambda r: float(r.ts or 0), reverse=True)[:limit]
    return {
        "events": [
            {
                "ts": float(row.ts or 0),
                "action": row.action,
                "actor": row.actor,
                "task_id": row.task_id,
                "severity": row.severity,
                "details": row.details,
            }
            for row in rows
        ]
    }


@router.get(
    "/audit/export",
    dependencies=[Depends(verify_local_auth)],
    summary="Export the audit log as CSV or JSON (filterable)",
    tags=["Diagnostics"],
)
async def local_export_audit(
    format: str = "csv", severity: str = "", since: float = 0.0, limit: int = 5000
) -> Response:
    from nexus.telemetry import audit_export

    limit = max(1, min(20000, int(limit)))
    async with get_session() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    rows = sorted(rows, key=lambda r: float(r.ts or 0), reverse=True)[:limit]
    events = [
        {"ts": float(r.ts or 0), "action": r.action, "actor": r.actor,
         "task_id": r.task_id, "severity": r.severity, "details": r.details}
        for r in rows
    ]
    events = audit_export.filter_events(events, severity=severity, since=since)
    if format == "json":
        return Response(content=json.dumps({"events": events}),
                        media_type="application/json")
    return Response(
        content=audit_export.events_to_csv(events),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit_log.csv"'},
    )


@router.get(
    "/metrics",
    dependencies=[Depends(verify_local_auth)],
    summary="Prometheus-format metrics",
    tags=["Diagnostics"],
)
async def local_get_metrics() -> Response:
    metrics = STATE.metrics
    lines = [
        f"nexus_queue_depth {metrics.get('queue_depth', 0)}",
        f"nexus_processing_depth {metrics.get('processing_depth', 0)}",
        f"nexus_active_workers {metrics.get('active_workers', 0)}",
        f"nexus_tasks_dispatched {metrics.get('tasks_dispatched', 0)}",
        f"nexus_tasks_completed {metrics.get('tasks_completed', 0)}",
        f"nexus_tasks_failed {metrics.get('tasks_failed', 0)}",
        f"nexus_task_retries {metrics.get('task_retries', 0)}",
        f"nexus_tasks_requeued {metrics.get('tasks_requeued', 0)}",
    ]
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )


@router.get(
    "/download/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Download completed task result archive",
    tags=["Task Lifecycle"],
)
async def local_download_result(task_id: str) -> FileResponse:
    """Zip the extracted result directory and stream it back."""
    safe_id = task_id.replace("..", "").replace("/", "_").replace("\\", "_")
    task_dir = os.path.join("completed_tasks", safe_id)
    if not os.path.isdir(task_dir):
        raise HTTPException(
            404, detail="No completed output found for this task."
        )
    archive_path = os.path.join("completed_tasks", f"{safe_id}_result")
    shutil.make_archive(archive_path, "zip", task_dir)
    return FileResponse(
        f"{archive_path}.zip",
        media_type="application/zip",
        filename=f"{safe_id}.zip",
    )


# --- B3: result/artifact browser (per-file listing + preview/download) --------


@router.get(
    "/results",
    dependencies=[Depends(verify_local_auth)],
    summary="B3: list completed-task result bundles",
    tags=["Task Lifecycle"],
)
async def local_list_results() -> dict:
    from nexus.runtime import result_browser
    return {"bundles": result_browser.list_bundles()}


@router.get(
    "/results/{task_id}/files",
    dependencies=[Depends(verify_local_auth)],
    summary="B3: list files inside one result bundle",
    tags=["Task Lifecycle"],
)
async def local_list_result_files(task_id: str) -> dict:
    from nexus.runtime import result_browser

    files = result_browser.list_files(task_id)
    if files is None:
        raise HTTPException(404, detail="No result bundle for this task.")
    return {"task_id": task_id, "files": files,
            "text_preview_max": result_browser.TEXT_PREVIEW_MAX}


@router.get(
    "/results/{task_id}/file",
    dependencies=[Depends(verify_local_auth)],
    summary="B3: fetch one file from a result bundle (preview/download)",
    tags=["Task Lifecycle"],
)
async def local_get_result_file(task_id: str, path: str) -> FileResponse:
    from nexus.runtime import result_browser

    target = result_browser.resolve_file(task_id, path)
    if target is None:
        raise HTTPException(404, detail="File not found in this result bundle.")
    return FileResponse(str(target), filename=target.name)


@router.delete(
    "/results/{task_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="B3: delete one result bundle from disk",
    tags=["Task Lifecycle"],
)
async def local_delete_result(task_id: str) -> dict:
    from nexus.runtime import result_browser

    if not result_browser.delete_bundle(task_id):
        raise HTTPException(404, detail="No result bundle for this task.")
    return {"message": f"Result artifacts for {task_id} deleted."}


# --- E5: node backup (export) -------------------------------------------------


@router.get(
    "/backup",
    dependencies=[Depends(verify_local_auth)],
    summary="E5: download a node backup — normal (DB + identity) or full (+ on-disk data)",
    tags=["Diagnostics"],
)
async def local_backup(full: bool = False) -> FileResponse:
    """Build and stream a backup zip. Contains private keys + the at-rest
    secret — a full identity clone; store it securely. ``full=1`` also bundles
    plugins, saved artifacts, and hosted deposit bytes."""
    import tempfile as _tf
    from datetime import datetime
    from pathlib import Path as _P

    from nexus.runtime import backup as _backup

    d = _tf.mkdtemp(prefix="nexus_backup_")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kind = "full-backup" if full else "backup"
    dest = _P(d) / f"nexus-{kind}-{stamp}.zip"
    await asyncio.to_thread(_backup.build_backup, dest, full)
    return FileResponse(str(dest), media_type="application/zip", filename=dest.name)


@router.post(
    "/restore",
    dependencies=[Depends(verify_local_auth)],
    summary="E5: upload a backup zip; applied automatically on next node start",
    tags=["Diagnostics"],
)
async def local_restore(file: UploadFile = File(...)) -> dict:
    """Validate + stage an uploaded backup. It's applied on the next start
    (before the DB opens), so the running node is never overwritten in place."""
    from nexus.runtime import backup as _backup

    data = await file.read()
    pending = _backup.pending_restore_path()
    pending.write_bytes(data)
    ok, reason = _backup.validate_backup_zip(pending)
    if not ok:
        try:
            pending.unlink()
        except OSError:
            pass
        raise HTTPException(400, detail=f"Not a valid node backup: {reason}")
    too_new = _backup.restore_too_new(pending)
    if too_new is not None:
        from nexus.storage.models import SCHEMA_VERSION
        try:
            pending.unlink()
        except OSError:
            pass
        raise HTTPException(
            400,
            detail=(
                f"This backup is from a newer version (data format v{too_new} > "
                f"this node's v{SCHEMA_VERSION}). Update NexusGrid first, then restore."
            ),
        )
    # Auto-detect the kind from the manifest so the UI can tell the user what a
    # restart will restore (no separate "full" upload needed).
    is_full = False
    try:
        import json as _json
        import zipfile as _zip
        with _zip.ZipFile(pending) as _z:
            is_full = bool(_json.loads(_z.read("manifest.json")).get("full"))
    except Exception:
        pass
    kind = "Full backup" if is_full else "Backup"
    return {"status": "staged", "full": is_full,
            "message": f"{kind} staged — restart the node to finish restoring."}


###############################################################################
# Foreign-storage host endpoints
###############################################################################


def _foreign_storage_quota_used_gb() -> float:
    """Sum encrypted bytes the host currently holds."""
    from nexus.runtime.foreign_storage_quota import used_gb

    return used_gb()


@router.get(
    "/foreign_storage/quota",
    dependencies=[Depends(verify_local_auth)],
    summary="foreign-storage quota usage on this host",
    tags=["Foreign Storage"],
)
async def foreign_storage_quota() -> dict:
    from nexus.runtime.foreign_storage_quota import (
        auto_opt_out_reason,
        disk_free_gb,
        effective_free_gb,
        is_accepting_offers,
        is_effectively_accepting,
        used_gb,
    )

    pledge_gb = float(LOCAL_SETTINGS.get("storage_max_total_gb", 5) or 5)
    used = used_gb()
    return {
        "used_gb": round(used, 3),
        "total_gb": int(pledge_gb),  # legacy field, kept for old UI clients
        "pledge_gb": round(pledge_gb, 3),
        "free_gb": round(effective_free_gb(), 3),
        "disk_free_gb": round(disk_free_gb(), 3),
        "accepting": is_effectively_accepting(),
        "manual_opt_in": is_accepting_offers(),
        "auto_opt_out_reason": auto_opt_out_reason(),
        "per_depositor_gb": int(
            LOCAL_SETTINGS.get("storage_max_per_depositor_gb", 5) or 5
        ),
    }


@router.get(
    "/foreign_storage/pick_save_file",
    dependencies=[Depends(verify_local_auth)],
    summary="open a native OS Save-As dialog, return the absolute path",
    tags=["Foreign Storage"],
)
async def foreign_storage_pick_save_file(default_name: str = "") -> dict:
    """Show a native Save-As dialog for the deposit download flow.

    Mirrors :func:`foreign_storage_pick_file` (open dialog) but uses
    ``asksaveasfilename`` so the depositor can pick a destination path
    without having to type one.
    """
    def _pick(initial: str) -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            return (
                filedialog.asksaveasfilename(
                    parent=root,
                    initialfile=initial or "",
                )
                or ""
            )
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    try:
        path = await asyncio.to_thread(_pick, default_name)
    except Exception as exc:
        raise HTTPException(500, f"native save dialog unavailable: {exc}")
    return {"path": path}


@router.get(
    "/foreign_storage/pick_file",
    dependencies=[Depends(verify_local_auth)],
    summary="open a native OS file picker, return the absolute path",
    tags=["Foreign Storage"],
)
async def foreign_storage_pick_file() -> dict:
    """Open a native open-file dialog on the depositor's desktop.

    The browser deliberately hides absolute paths from JS (the value
    you'd see in `<input type="file">` is the literal string
    ``C:\\fakepath\\name.ext``). Since the NexusGrid backend runs on
    the user's own machine, we can pop a Tk dialog from here and
    return the real path so the deposit endpoint can stream the file
    directly — no copy into the cache dir.
    """
    def _pick() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            return filedialog.askopenfilename(parent=root) or ""
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    try:
        path = await asyncio.to_thread(_pick)
    except Exception as exc:
        raise HTTPException(500, f"native file picker unavailable: {exc}")
    return {"path": path}


@router.get(
    "/foreign_storage/pick_directory",
    dependencies=[Depends(verify_local_auth)],
    summary="Round 1: open a native OS directory picker, return the absolute path",
    tags=["Foreign Storage"],
)
async def foreign_storage_pick_directory() -> dict:
    """Native folder picker for the download save_to flow.

    Sister to :func:`foreign_storage_pick_save_file`; the user wants
    to choose a destination *folder* and have the manifest filename
    appended automatically (handled in the download endpoint).
    """
    def _pick() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            return filedialog.askdirectory(parent=root, mustexist=True) or ""
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    try:
        path = await asyncio.to_thread(_pick)
    except Exception as exc:
        raise HTTPException(500, f"native directory picker unavailable: {exc}")
    return {"path": path}


@router.post(
    "/foreign_storage/upload_temp",
    dependencies=[Depends(verify_local_auth)],
    summary="receive a browser-picked file, stash it, return the path",
    tags=["Foreign Storage"],
)
async def foreign_storage_upload_temp(file: UploadFile = File(...)) -> dict:
    """Stage a browser-picked file under the cache dir.

    The depositor's standard ``/deposit`` endpoint reads from a local
    filesystem path. Browsers can't expose absolute paths for security, so
    when the user clicks ``Choose File`` we receive the bytes here, write
    them to a per-deposit temp directory, and hand the path back.
    """
    import tempfile
    from pathlib import Path

    from nexus.core import cache_dir, get_node_port

    base = cache_dir(get_node_port()) / "foreign_storage_uploads"
    base.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="upload-", dir=str(base)))
    safe_name = Path(file.filename or "upload.bin").name
    out = tmp_dir / safe_name
    total = 0
    with out.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    # Register the staged path so the deposit endpoint can hand cleanup to
    # the post-transfer hook. Without this the temp file lingers forever.
    STATE.upload_temp_dirs_by_path[str(out)] = str(tmp_dir)
    return {"file_path": str(out), "size_bytes": total, "filename": safe_name}


@router.post(
    "/file_info",
    dependencies=[Depends(verify_local_auth)],
    summary="stat a local file (used by Foreign Storage auto-pick)",
    tags=["Foreign Storage"],
)
async def local_file_info(payload: dict) -> dict:
    from pathlib import Path

    raw = str(payload.get("file_path") or "").strip()
    if not raw:
        return {"exists": False, "is_file": False, "size_bytes": 0}
    p = Path(raw)
    try:
        if not p.exists():
            return {"exists": False, "is_file": False, "size_bytes": 0}
        if not p.is_file():
            return {"exists": True, "is_file": False, "size_bytes": 0}
        return {"exists": True, "is_file": True, "size_bytes": p.stat().st_size}
    except OSError:
        return {"exists": False, "is_file": False, "size_bytes": 0}


async def _fetch_peer_capacities() -> tuple[list[dict], int]:
    """Query every trusted peer's foreign-storage capacity in parallel.

    Returns ``(rows, total_trusted)`` where ``rows`` is each peer's
    capacity dict (``available`` / ``accepting`` / ``free_gb`` / …) and
    ``total_trusted`` is the count of trusted DB rows queried.
    Used by ``/foreign_storage/peer_capacities`` (UI) and by the P2
    Auto-mode deposit path (top-K candidate selection).
    """
    import asyncio as _asyncio

    import httpx
    from nexus.core.identity import get_node_identity
    from nexus.networking.worker_client import _peer_request
    from nexus.storage import Peer, get_session

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(Peer).filter(
                        Peer.status.in_(
                            [
                                "trusted",
                                "trusted_pending_in",
                                "trusted_pending_out",
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    async def _fetch_one(peer: Peer) -> dict:
        from nexus.core.identity import resolve_uuid_to_ip

        peer_uuid = peer.ip
        # Always re-resolve at call time — the cached resolved_ip column can
        # be stale by minutes; resolve_uuid_to_ip pulls from the live discovery
        # cache populated by beacons.
        resolved = resolve_uuid_to_ip(peer_uuid)
        if resolved == peer_uuid:
            resolved = peer.resolved_ip or peer_uuid
        # Outbound peer calls authenticate with ``their_auth_token`` — the
        # value the remote node has stored as ``my_auth_token`` for us.
        token = peer.their_auth_token or ""
        # Prefer a live display name from active_workers (the heartbeat
        # carries ``user_display_name`` and the DB row is often stale or
        # empty for brand-new pairings). Falls back to the DB column.
        live_name = ""
        worker_entry = STATE.active_workers.get(peer_uuid, {})
        if worker_entry:
            live_name = str(
                worker_entry.get("stats", {}).get("user_display_name", "") or ""
            )
        # P4: don't even attempt the HTTP probe for peers presence has
        # already flagged offline. Saves up to 4 s per offline peer per
        # refresh and lets the UI's "Offline" badge surface immediately
        # instead of waiting for the next round of timeouts.
        from nexus.telemetry import presence
        presence_offline = presence.is_peer_offline(peer_uuid)
        base = {
            "peer_uuid": peer_uuid,
            "display_name": live_name or peer.display_name or "",
            "resolved_ip": resolved,
            "available": False,
            "accepting": False,
            "free_gb": 0.0,
            "pledge_gb": 0.0,
            "used_gb": 0.0,
            "presence_offline": presence_offline,
        }
        if not token or presence_offline:
            return base
        headers = {"X-Cluster-Key": token, "X-Node-Address": get_node_identity()}
        try:
            async with httpx.AsyncClient(verify=False, timeout=4.0) as client:
                res = await _peer_request(
                    client,
                    "GET",
                    peer_uuid,
                    resolved,
                    "/peer/foreign_storage_capacity",
                    headers=headers,
                )
                if res.status_code != 200:
                    # Probe reached the peer but they're refusing — leave
                    # presence untouched (a 4xx/5xx response is still a
                    # liveness signal, just not for foreign storage).
                    return base
                payload = res.json()
        except Exception:
            # Probe failed outright (timeout, connection refused). This is a
            # better offline signal than the WS heartbeat for foreign-storage-
            # only pairings, where the peer never opens a worker WS. Drive
            # presence from it so the topology view stops showing the peer
            # as offline when they're actually reachable for FS / compute.
            presence.mark_peer_offline(peer_uuid, source="fs_probe")
            return base
        # Probe succeeded — peer is reachable. Keep presence in sync so
        # downstream consumers (topology, available-hosts list, dispatch
        # picker) agree on the liveness signal.
        presence.mark_peer_online(peer_uuid, source="fs_probe")
        return {
            **base,
            "available": True,
            "accepting": bool(payload.get("accepting", False)),
            "free_gb": float(payload.get("free_gb", 0.0) or 0.0),
            "pledge_gb": float(payload.get("pledge_gb", 0.0) or 0.0),
            "used_gb": float(payload.get("used_gb", 0.0) or 0.0),
            "presence_offline": False,
        }

    results = await _asyncio.gather(*(_fetch_one(p) for p in rows))
    return results, len(rows)


@router.get(
    "/foreign_storage/peer_capacities",
    dependencies=[Depends(verify_local_auth)],
    summary="fan-out free-GB query to all trusted peers",
    tags=["Foreign Storage"],
)
async def foreign_storage_peer_capacities() -> dict:
    """Return per-trusted-peer foreign-storage capacity, sorted by free_gb desc.

    Each peer is queried with a short timeout. Peers that don't respond (offline,
    older versions without the endpoint, opted-out and dropped) get a row with
    ``available=False`` so the UI can render them grayed-out instead of silently
    dropping them.
    """
    results, total = await _fetch_peer_capacities()
    # Accepting+available peers first (sorted by free_gb desc), then everyone else.
    accepting = sorted(
        (r for r in results if r["available"] and r["accepting"]),
        key=lambda r: r["free_gb"],
        reverse=True,
    )
    other = [r for r in results if not (r["available"] and r["accepting"])]
    ordered = accepting + other
    return {"peers": ordered[:20], "total_trusted": total}


@router.get(
    "/foreign_storage/incoming",
    dependencies=[Depends(verify_local_auth)],
    summary="pending deposit offers awaiting host accept/decline",
    tags=["Foreign Storage"],
)
async def foreign_storage_incoming() -> dict:
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "host",
                        ForeignStorageDeposit.status == "offered",
                    )
                )
            )
            .scalars()
            .all()
        )
        depositor_uuids = {r.depositor_uuid for r in rows if r.depositor_uuid}
        name_by_uuid = await _resolve_peer_display_names(db, depositor_uuids)
    return {
        "offers": [
            {
                "deposit_id": r.deposit_id,
                "depositor_uuid": r.depositor_uuid,
                "depositor_display_name": name_by_uuid.get(r.depositor_uuid, ""),
                "filename": r.filename or "",
                "total_bytes": int(r.total_bytes or 0),
                "chunk_count": int(r.chunk_count or 0),
                "transport": r.transport,
                "ttl_days": int(r.ttl_days or 0),
                # Host's incoming-offer view intentionally omits
                # ``password_hint`` — it is the depositor's own memory
                # aid and should never surface in any host-facing
                # context. Defense-in-depth with the wire-frame builder
                # and the workflow handler, which also drop the field.
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.get(
    "/foreign_storage/hosted",
    dependencies=[Depends(verify_local_auth)],
    summary="deposits this node currently holds",
    tags=["Foreign Storage"],
)
async def foreign_storage_hosted() -> dict:
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit)
                    .filter(
                        ForeignStorageDeposit.role == "host",
                        ForeignStorageDeposit.status.in_(
                            (
                                "accepted",
                                "transferring",
                                "stored",
                                "eviction_requested",
                                "in_db_grace",
                            )
                        ),
                    )
                    .order_by(ForeignStorageDeposit.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        depositor_uuids = {r.depositor_uuid for r in rows if r.depositor_uuid}
        name_by_uuid = await _resolve_peer_display_names(db, depositor_uuids)
    return {
        "deposits": [
            {
                "deposit_id": r.deposit_id,
                "depositor_uuid": r.depositor_uuid,
                "depositor_display_name": name_by_uuid.get(r.depositor_uuid, ""),
                "status": r.status,
                "filename": r.filename or "",
                "total_bytes": int(r.total_bytes or 0),
                "created_at": r.created_at or "",
                "ttl_at": r.ttl_at,
                "eviction_requested_at": r.eviction_requested_at or "",
                # Lets the host UI render the "safe-delete in N days"
                # countdown after they click Evict — the lifecycle pass
                # transitions to ``in_db_grace`` 1 day after the request
                # if the depositor doesn't respond, then purges
                # ``eviction_total_days - 1`` days after that. UI maths
                # off these timestamps + the configured total.
                "db_grace_at": r.db_grace_at or "",
                "eviction_total_days": int(r.eviction_total_days or 0),
                # Surface the view-grant flag so the UI can show
                # the Unlock/Preview/Lock action group on shared rows.
                "host_view_granted_at": int(r.host_view_granted_at or 0),
                # Once the host has clicked Open at least once we have a
                # plaintext directory on disk — UI uses this to decide
                # between "Open Folder" (re-open existing) vs "Open"
                # (first-time materialize).
                "host_view_decrypted_dir": str(r.host_view_decrypted_dir or ""),
            }
            for r in rows
        ]
    }


@router.post(
    "/foreign_storage/respond/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="host accepts or declines a deposit offer",
    tags=["Foreign Storage"],
)
async def foreign_storage_respond(
    deposit_id: str, payload: dict
) -> dict:
    from nexus.networking.storage_pump import build_storage_offer_response
    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.crypto import sign_bytes
    from nexus.security.foreign_storage_terms import host_terms_text
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    action = str(payload.get("action") or "").lower()
    if action not in {"accept", "decline"}:
        raise HTTPException(400, "action must be 'accept' or 'decline'")
    if action == "accept" and not payload.get("host_tc_signed"):
        raise HTTPException(400, "host_tc_signed required to accept")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status != "offered":
            raise HTTPException(409, f"deposit already {row.status}")

        accepted = action == "accept"
        host_tc_text = host_terms_text()
        host_tc_bytes = host_tc_text.encode("utf-8")
        host_tc_sha = _sha256_hex(host_tc_bytes)
        host_sig = ""
        cloud_url_to_pull = ""
        if accepted:
            host_sig = sign_bytes(
                "foreign_storage_terms", deposit_id, host_tc_bytes
            )
            row.status = "accepted"
            row.host_signature = host_sig
            # Set up the host pump entry so receive_chunk can land bytes.
            from nexus.networking.storage_pump import deposit_dir

            dpath = deposit_dir(deposit_id, row.depositor_uuid)
            STATE.foreign_storage_pumps[deposit_id] = {
                "role": "host",
                "peer_uuid": row.depositor_uuid,
                "deposit_id": deposit_id,
                "total_bytes": int(row.total_bytes or 0),
                "chunk_count": int(row.chunk_count or 0),
                "dir": str(dpath),
                "received_idx": -1,
                "last_chunk_at": time.time(),
                "started_at": time.time(),
                "last_progress_emit": 0.0,
                "status": "transferring",
                "transport": row.transport,
            }
            if row.transport == "cloud_url" and row.cloud_url:
                cloud_url_to_pull = row.cloud_url
        else:
            row.status = "declined"
        await db.commit()
        depositor_uuid = row.depositor_uuid

    await _send_to_peer(
        depositor_uuid,
        build_storage_offer_response(
            deposit_id, accepted, host_tc_sha, host_sig,
            reason="host_declined" if not accepted else "",
        ),
    )
    await record_audit_event(
        "storage.deposit_accepted" if accepted else "storage.deposit_declined",
        actor=depositor_uuid,
        task_id=deposit_id,
        severity="info",
    )
    if cloud_url_to_pull:
        # Stub: pull the depositor's pre-encrypted blob from
        # The URL they already uploaded to. will swap this for a
        # proper cloud-storage SDK (gdrive / s3 / r2 / b2).
        asyncio.create_task(
            _pull_cloud_deposit(
                deposit_id, depositor_uuid, cloud_url_to_pull
            ),
            name=f"nexus.foreign_storage.cloud_pull.{deposit_id}",
        )
    return {"status": "ok", "accepted": accepted}


async def _pull_cloud_deposit(
    deposit_id: str, depositor_uuid: str, url: str
) -> None:
    """Stub: download a depositor's encrypted blob from *url*.

    The depositor uploaded the already-AES-GCM-encrypted bytes
    out-of-band; we just slice them back into the same per-chunk on-disk
    layout the streaming path produces, so retrieval works uniformly.
    """
    from nexus.networking.storage_pump import (
        CHUNK_CIPHERTEXT_BYTES,
        build_storage_complete,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    pump = STATE.foreign_storage_pumps.get(deposit_id) or {}
    dpath = pump.get("dir")
    if not dpath:
        return

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            blob = resp.content
    except Exception as exc:
        await record_audit_event(
            "storage.cloud_pull_failed",
            actor=depositor_uuid,
            task_id=deposit_id,
            severity="warning",
            details=str(exc),
        )
        return

    from pathlib import Path

    base = Path(dpath)
    chunks: list[bytes] = []
    idx = 0
    while idx * CHUNK_CIPHERTEXT_BYTES < len(blob):
        chunks.append(
            blob[idx * CHUNK_CIPHERTEXT_BYTES : (idx + 1) * CHUNK_CIPHERTEXT_BYTES]
        )
        idx += 1
    for i, chunk in enumerate(chunks):
        (base / f"chunk_{i:08d}.enc").write_bytes(chunk)

    pump["received_idx"] = len(chunks) - 1
    pump["last_chunk_at"] = time.time()
    pump["status"] = "stored"

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "stored"
            row.chunk_count = len(chunks)
            await db.commit()

    await _send_to_peer(
        depositor_uuid,
        build_storage_complete(deposit_id, depositor_signature=""),
    )
    await record_audit_event(
        "storage.deposit_completed",
        actor=depositor_uuid,
        task_id=deposit_id,
        details=f"transport=cloud_url chunks={len(chunks)}",
    )


@router.post(
    "/foreign_storage/resend_offer/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Depositor: re-offer a previously declined deposit to the same host",
    tags=["Foreign Storage"],
)
async def foreign_storage_resend_offer(deposit_id: str) -> dict:
    """Re-send the original offer frame for a ``declined`` deposit.

    Row metadata (salt, sealed manifest, depositor signature, etc.)
    survives the decline, so we can rebuild the same offer without
    re-asking for the password. If the host accepts this time, the
    depositor will need a cached key + file path to actually
    transfer — same Resume flow that already covers post-restart
    deposits.
    """
    from hashlib import sha256 as _sha
    from nexus.networking.storage_pump import build_storage_offer
    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.foreign_storage_terms import DEFAULT_DEPOSITOR_TERMS
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status != "declined":
            raise HTTPException(
                409,
                f"only declined deposits can be re-sent (state={row.status})",
            )
        host_uuid = row.host_uuid
        if not host_uuid:
            raise HTTPException(409, "no host_uuid on row (auto-mode rows can't be re-sent)")
        # Rebuild the offer frame from persisted metadata. Re-sign the terms
        # with the pair key — the stored row signature is self-keyed and
        # Would never verify on the host.
        from nexus.runtime.foreign_storage_workflow import peer_signing_key
        from nexus.security.crypto import sign_bytes as _sign_bytes

        _tc_bytes = DEFAULT_DEPOSITOR_TERMS.encode("utf-8")
        offer_frame = build_storage_offer(
            deposit_id=deposit_id,
            total_bytes=int(row.total_bytes or 0),
            chunk_count=int(row.chunk_count or 0),
            salt=bytes(row.salt or b""),
            password_hint=row.password_hint or "",
            ttl_days=int(row.ttl_days or 30),
            transport=row.transport or "stream",
            cloud_url=row.cloud_url or "",
            depositor_tc=_sha(_tc_bytes).hexdigest(),
            depositor_signature=_sign_bytes(
                "foreign_storage_terms",
                deposit_id,
                _tc_bytes,
                key=await peer_signing_key(host_uuid),
            ),
            filename=row.filename or "",
        )

    sent = await _send_to_peer(host_uuid, offer_frame)
    if not sent:
        raise HTTPException(503, "could not reach host")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one()
        # Move the row back to the active offered state so it surfaces
        # in My Deposits again. If the host declines again, the
        # offer-response handler will flip it back to "declined" and
        # the row returns to Histories.
        row.status = "offered"
        row.host_signature = ""
        await db.commit()

    await record_audit_event(
        "storage.deposit_resent",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"host={host_uuid}",
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/cancel_eviction/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Host: cancel a pending eviction (accidental Evict click)",
    tags=["Foreign Storage"],
)
async def foreign_storage_cancel_eviction(deposit_id: str) -> dict:
    """Cancel an eviction that hasn't yet entered DB grace.

    Once chunks move off disk into the DB grace blob we don't restore
    them — at that point the host has committed to letting go.
    """
    from nexus.networking.storage_pump import build_storage_eviction_cancelled
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status != "eviction_requested":
            raise HTTPException(
                409,
                f"cannot cancel eviction in state {row.status} "
                "(only eviction_requested can be cancelled)",
            )
        row.status = "stored"
        row.eviction_requested_at = ""
        row.db_grace_at = ""
        row.eviction_total_days = 0
        depositor_uuid = row.depositor_uuid
        await db.commit()

    await _send_to_peer(
        depositor_uuid, build_storage_eviction_cancelled(deposit_id)
    )
    await record_audit_event(
        "storage.eviction_cancelled",
        actor=deposit_id,
        task_id=deposit_id,
        severity="info",
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/eviction/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="host requests early eviction of a deposit",
    tags=["Foreign Storage"],
)
async def foreign_storage_eviction(
    deposit_id: str,
    payload: dict = Body(default_factory=dict),
) -> dict:
    from nexus.networking.storage_pump import build_storage_eviction_request
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import iso_now

    # Per-deposit countdown: each Evict click carries its own
    # ``total_days``. Min 1 day so the depositor always has at least
    # some window to react; no max — the host can park a friend's
    # data for as long as they want. We still honor the legacy
    # ``evict_total_days`` setting as a fallback for any caller that
    # doesn't supply the field, but the UI prompts on every click.
    try:
        requested_days = int(payload.get("total_days") or 0)
    except (TypeError, ValueError):
        requested_days = 0
    if requested_days <= 0:
        requested_days = int(LOCAL_SETTINGS.get("evict_total_days", 3) or 3)
    total_days = max(1, requested_days)

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status not in {"stored", "transferring"}:
            raise HTTPException(409, f"cannot evict in state {row.status}")
        row.status = "eviction_requested"
        row.eviction_requested_at = iso_now()
        # Stamp the per-deposit countdown so the UI's math survives the
        # host running a second Evict against a different deposit with
        # a different value.
        row.eviction_total_days = total_days
        depositor_uuid = row.depositor_uuid
        await db.commit()

    await _send_to_peer(
        depositor_uuid,
        build_storage_eviction_request(
            deposit_id,
            response_window_days=1,
            total_days=total_days,
        ),
    )
    await record_audit_event(
        "storage.eviction_requested",
        actor=deposit_id,
        task_id=deposit_id,
        severity="info",
        details=f"total_days={total_days}",
    )
    return {"status": "ok", "total_days": total_days}


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


###############################################################################
# Foreign-storage depositor endpoints
###############################################################################


@router.post(
    "/foreign_storage/deposit",
    dependencies=[Depends(verify_local_auth)],
    summary="kick off an encrypted deposit toward a peer",
    tags=["Foreign Storage"],
)
async def foreign_storage_deposit(payload: dict) -> dict:
    """Start a deposit. Synchronous up to the offer; transfer runs as a task."""
    import os
    from pathlib import Path

    from nexus.networking.storage_pump import (
        build_storage_offer,
        transfer_deposit,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.crypto import sign_bytes
    from nexus.security.deposit_crypto import (
        SALT_BYTES,
        derive_key,
    )
    from nexus.security.foreign_storage_terms import (
        DEFAULT_DEPOSITOR_TERMS,
    )
    from nexus.networking.storage_pump import CHUNK_PLAINTEXT_BYTES
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import timestamp

    target = str(payload.get("target_peer") or payload.get("target_peer_ip") or "")
    file_path = str(payload.get("file_path") or "")
    password = str(payload.get("password") or "")
    ttl_days = int(payload.get("ttl_days") or 30)
    transport = str(payload.get("transport") or "stream")
    cloud_url = str(payload.get("cloud_url") or "")
    password_hint = str(payload.get("password_hint") or "")
    # Per-deposit transfer-window override; 0/absent = node setting.
    dep_window = int(payload.get("window_chunks") or 0)
    if dep_window:
        dep_window = max(2, min(128, dep_window))
    # Per-deposit transit overrides (clamps mirror the settings bounds).
    dep_ack = int(payload.get("ack_timeout_sec") or 0)
    if dep_ack:
        dep_ack = max(5, min(300, dep_ack))
    dep_retries = int(payload.get("transit_retries") or 0)
    if dep_retries:
        dep_retries = max(1, min(20, dep_retries))
    dep_offer_timeout = int(payload.get("offer_timeout_sec") or 0)
    if dep_offer_timeout:
        dep_offer_timeout = max(30, min(86_400, dep_offer_timeout))
    # Per user spec: only queue-on-offline when the depositor explicitly
    # picked a target. Best-fit auto-pick and fan-out auto-mode both must
    # fail fast (503) instead of waiting hours for a host the user did
    # not consciously choose. The client passes ``queue_if_offline=false``
    # for any auto-derived target.
    queue_if_offline = bool(payload.get("queue_if_offline", True))
    if not (target and file_path and password):
        raise HTTPException(400, "target_peer, file_path, password required")
    # P2 Auto mode: ``target == "auto"`` asks us to fan out to top-3
    # accepting peers; first to accept wins. Blocked-peer + transport
    # validation still apply.
    auto_mode = target == "auto"
    if auto_mode:
        # Fan-out path never queues — the all-fail branch raises 503.
        queue_if_offline = False
    # Batch C: refuse to deposit to a peer the user has blocked.
    if not auto_mode and target in set(LOCAL_SETTINGS.get("blocked_peer_uuids") or []):
        raise HTTPException(403, "target peer is blocked")
    if transport not in {"stream", "cloud_url"}:
        raise HTTPException(400, "transport must be 'stream' | 'cloud_url'")

    fpath = Path(file_path)
    if not fpath.is_file():
        raise HTTPException(404, f"file not found: {file_path}")
    total_bytes = fpath.stat().st_size
    chunk_count = (total_bytes + CHUNK_PLAINTEXT_BYTES - 1) // CHUNK_PLAINTEXT_BYTES

    # Count a group-scoped deposit as storage the depositor uses from
    # that group's pool. (Hosted-side bytes are credited on the host once the
    # deposit offer carries target_groups — fast-follow.)
    _dep_groups = [
        str(g).strip() for g in (payload.get("target_groups") or []) if str(g).strip()
    ]
    if _dep_groups:
        from nexus.runtime.group_compute import record_compute_stat
        for _g in _dep_groups:
            await record_compute_stat(_g, storage_bytes_used=int(total_bytes))

    # P8.8: SHA-256 of the full plaintext, sealed into the manifest below.
    # On Resume the depositor re-hashes the user-picked file and compares —
    # any mismatch aborts the transfer (file changed since deposit time).
    import hashlib as _hashlib
    _h = _hashlib.sha256()
    with fpath.open("rb") as _fh:
        for _block in iter(lambda: _fh.read(1024 * 1024), b""):
            _h.update(_block)
    file_sha256_hex = _h.hexdigest()
    file_mtime_ns = fpath.stat().st_mtime_ns

    # P2: in auto mode resolve the candidate set before persisting the row.
    # Filter out blocked peers + peers without enough free space, then take
    # top-3 by free_gb. Bail with 503 if nobody fits — caller must retry.
    auto_candidates: list[str] = []
    if auto_mode:
        required_gb = total_bytes / (1024 ** 3)
        blocked = set(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])
        results, _total = await _fetch_peer_capacities()
        eligible = [
            r for r in results
            if r["available"]
            and r["accepting"]
            and r["peer_uuid"] not in blocked
            and r["free_gb"] >= required_gb
        ]
        # Group-scoped storage. When the deposit names target
        # groups, only that group's task:run members are eligible hosts.
        target_groups = [
            str(g).strip()
            for g in (payload.get("target_groups") or [])
            if str(g).strip()
        ]
        if target_groups:
            from nexus.runtime.group_compute import (
                group_member_uuids_with_task_run,
            )
            allowed_uuids = await group_member_uuids_with_task_run(
                set(target_groups)
            )
            eligible = [r for r in eligible if r["peer_uuid"] in allowed_uuids]
        eligible.sort(key=lambda r: r["free_gb"], reverse=True)
        auto_candidates = [r["peer_uuid"] for r in eligible[:3]]
        if not auto_candidates:
            raise HTTPException(
                503,
                "no accepting peers with enough free space"
                + (" in the selected group(s)" if target_groups else "")
                + " for auto-mode deposit",
            )

    deposit_id = uuid.uuid4().hex
    # If this path came from /upload_temp, attach the temp dir to
    # the deposit so the post-transfer hook can free the disk.
    staged_dir = STATE.upload_temp_dirs_by_path.pop(file_path, None)
    if staged_dir:
        STATE.upload_temp_dirs_by_deposit[deposit_id] = staged_dir
    salt = os.urandom(SALT_BYTES)
    derived = derive_key(password, salt)

    depositor_tc_bytes = DEFAULT_DEPOSITOR_TERMS.encode("utf-8")
    depositor_tc_sha = _sha256_hex(depositor_tc_bytes)
    # Local accountability record (self-keyed). The on-the-wire signature is
    # re-made per target with the pair key below — the host verifies with
    # the same key, which the default node-local secret can never satisfy
    # across nodes.
    depositor_sig = sign_bytes(
        "foreign_storage_terms", deposit_id, depositor_tc_bytes
    )
    from nexus.runtime.foreign_storage_workflow import peer_signing_key

    async def _wire_sig(peer_id: str) -> str:
        return sign_bytes(
            "foreign_storage_terms",
            deposit_id,
            depositor_tc_bytes,
            key=await peer_signing_key(peer_id),
        )

    # Seal a small manifest so the unlock endpoint can verify
    # passwords without round-tripping to the host. Held depositor-side
    # only; the host never sees this blob.
    from nexus.security.deposit_crypto import seal_manifest as _seal

    sealed_manifest = _seal(
        derived,
        {
            "deposit_id": deposit_id,
            "filename": fpath.name,
            "size": total_bytes,
            # P8.8: file-change detection. sha256 is authoritative on Resume;
            # mtime_ns is a fast pre-check inside the pump per chunk read.
            "sha256": file_sha256_hex,
            "mtime_ns": int(file_mtime_ns),
        },
    )

    from datetime import datetime as _dt, timezone as _tz
    async with get_session() as db:
        db.add(
            ForeignStorageDeposit(
                deposit_id=deposit_id,
                role="depositor",
                depositor_uuid=LOCAL_SETTINGS.get("node_uuid", ""),
                # P2: in auto mode ``host_uuid`` stays empty until a candidate
                # accepts; the offer-response handler sets it on the win.
                host_uuid="" if auto_mode else target,
                status="offering_multi" if auto_mode else "offered",
                total_bytes=total_bytes,
                chunk_count=chunk_count,
                transport=transport,
                cloud_url=cloud_url,
                salt=salt,
                password_hint=password_hint,
                ttl_days=ttl_days,
                window_chunks=dep_window,
                ack_timeout_sec=dep_ack,
                transit_retries=dep_retries,
                offer_timeout_sec=dep_offer_timeout,
                created_at=_dt.now(_tz.utc).isoformat(),
                depositor_signature=depositor_sig,
                encrypted_manifest=sealed_manifest,
                # Surface the original filename in the UI so users see
                # something they recognise; deposit_id stays the unique
                # key. Sealed in manifest too, but reading it requires
                # the password — having a copy in the column lets the
                # listing UI show a name without unlocking every row.
                filename=fpath.name,
            )
        )
        await db.commit()

    # P1 (send-while-offline): cache the derived key + file path BEFORE the
    # send attempt so the retry pass can fulfil the offer later if the
    # target is currently unreachable. The store also covers the happy
    # path (chunk pump reads it on storage_offer_response).
    from nexus.runtime import foreign_storage_keys

    foreign_storage_keys.store(deposit_id, derived, file_path=str(fpath))

    async def _offer_frame_for(peer_id: str) -> dict:
        return build_storage_offer(
            deposit_id=deposit_id,
            total_bytes=total_bytes,
            chunk_count=chunk_count,
            salt=salt,
            password_hint=password_hint,
            ttl_days=ttl_days,
            transport=transport,
            cloud_url=cloud_url,
            depositor_tc=depositor_tc_sha,
            depositor_signature=await _wire_sig(peer_id),
            filename=fpath.name,
        )

    # P2: in auto mode, fan out to all candidates; partial-send is OK as
    # long as at least one offer landed (deliverable peer wins). All-fail
    # rolls back so the user can retry instead of waiting on a stranded row.
    if auto_mode:
        import asyncio as _asyncio

        async def _send_signed(c: str) -> bool:
            return await _send_to_peer(c, await _offer_frame_for(c))

        send_results = await _asyncio.gather(
            *(_send_signed(c) for c in auto_candidates)
        )
        landed = [c for c, ok in zip(auto_candidates, send_results) if ok]
        if not landed:
            foreign_storage_keys.drop(deposit_id)
            async with get_session() as db:
                row = (
                    await db.execute(
                        select(ForeignStorageDeposit).filter(
                            ForeignStorageDeposit.deposit_id == deposit_id
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.status = "withdrawn"
                    await db.commit()
            raise HTTPException(503, "could not reach any candidate peer")
        STATE.foreign_storage_auto_candidates[deposit_id] = list(landed)
        STATE.foreign_storage_auto_started_at[deposit_id] = time.time()
        await record_audit_event(
            "storage.auto_offer_fan_out",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            details=f"candidates={len(landed)} bytes={total_bytes}",
        )
        return {
            "status": "offering_multi",
            "deposit_id": deposit_id,
            "total_bytes": total_bytes,
            "chunk_count": chunk_count,
            "candidates": landed,
            "timeout_sec": int(
                LOCAL_SETTINGS.get("fs_auto_offer_timeout_sec", 300) or 300
            ),
        }

    sent = await _send_to_peer(target, await _offer_frame_for(target))
    if not sent:
        attempts = getattr(STATE, "last_send_attempts", {}).get(target, [])
        detail = (
            "Could not reach target peer. Transport attempts: "
            + " | ".join(attempts)
            if attempts
            else "Could not reach target peer (no transport attempted)."
        )
        # Per user spec: only queue when the depositor explicitly chose
        # this target. Auto-derived targets (best-fit / fan-out) drop
        # the row and fail fast so the user can re-pick instead of
        # waiting on a host they didn't consciously select.
        if not queue_if_offline:
            foreign_storage_keys.drop(deposit_id)
            async with get_session() as db:
                row = (
                    await db.execute(
                        select(ForeignStorageDeposit).filter(
                            ForeignStorageDeposit.deposit_id == deposit_id
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await db.delete(row)
                    await db.commit()
            raise HTTPException(503, detail)
        # P1: instead of rolling back, queue the offer. A lifecycle pass
        # retries when the target's presence flips back to online. Old
        # behaviour (immediate 503 + rollback) gave the user no
        # graceful path when the host was momentarily unreachable.
        _log.info(
            "[FOREIGN-STORAGE] Deposit %s -> %s queued offline: %s",
            deposit_id, target[:24], detail,
        )
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = "queued_offline"
                await db.commit()
        await record_audit_event(
            "storage.deposit_queued_offline",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            details=f"target={target} bytes={total_bytes}",
        )
        return {
            "status": "queued_offline",
            "deposit_id": deposit_id,
            "total_bytes": total_bytes,
            "chunk_count": chunk_count,
            "message": "Target is offline; offer will be delivered when they reconnect (24 h TTL).",
        }
    await record_audit_event(
        "storage.deposit_offered",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"target={target} bytes={total_bytes}",
    )

    return {
        "status": "ok",
        "deposit_id": deposit_id,
        "total_bytes": total_bytes,
        "chunk_count": chunk_count,
    }


@router.get(
    "/foreign_storage/my_deposits",
    dependencies=[Depends(verify_local_auth)],
    summary="deposits this node owns elsewhere",
    tags=["Foreign Storage"],
)
async def foreign_storage_my_deposits() -> dict:
    from nexus.core.config import effective_auto_rescue
    from nexus.storage import ForeignStorageDeposit, Peer, get_session

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit)
                    .filter(ForeignStorageDeposit.role == "depositor")
                    .order_by(ForeignStorageDeposit.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        # Enrich with display_name so the UI doesn't show raw UUIDs.
        host_uuids = {r.host_uuid for r in rows if r.host_uuid}
        name_by_uuid = await _resolve_peer_display_names(db, host_uuids)
    return {
        "deposits": [
            {
                "deposit_id": r.deposit_id,
                "host_uuid": r.host_uuid,
                "host_display_name": name_by_uuid.get(r.host_uuid, ""),
                "status": r.status,
                "filename": r.filename or "",
                "total_bytes": int(r.total_bytes or 0),
                "transport": r.transport,
                "ttl_days": int(r.ttl_days or 0),
                "ttl_at": r.ttl_at or "",
                "created_at": r.created_at or "",
                "eviction_requested_at": r.eviction_requested_at or "",
                # Surfaced so the UI can compute "safe delete after" and
                # "you have N days to respond" countdowns without
                # round-tripping the lifecycle scheduler.
                "db_grace_at": r.db_grace_at or "",
                "eviction_total_days": int(r.eviction_total_days or 0),
                # The depositor's own memory aid, shown when password
                # entry fails. The host never sees this field —
                # ``/foreign_storage/incoming`` intentionally omits it.
                "password_hint": r.password_hint or "",
                # Lets the UI render Share/Revoke state per row.
                "host_view_granted_at": int(r.host_view_granted_at or 0),
                # P8: pause/resume telemetry the UI needs to render the
                # "Paused — auto-retrying" vs "Paused — action needed"
                # split and the chunks-acked progress hint.
                "chunk_count": int(r.chunk_count or 0),
                "transferred_chunks": int(r.transferred_chunks or 0),
                "retry_count": int(r.retry_count or 0),
                "pause_reason": r.pause_reason or "",
                # Per-deposit auto-rescue config (override merged over the
                # node defaults) — drives the Auto-rescue button + its panel.
                "auto_rescue": effective_auto_rescue(r.deposit_id),
            }
            for r in rows
        ]
    }


# Statuses we consider "terminal" — no further lifecycle action is
# possible. Surface in the Histories panel rather than the active
# My Deposits / Hosting tables. ``purged`` is the normal end-state
# (TTL or eviction-grace cleanup ran); ``withdrawn`` covers depositor
# cancellation; ``failed_in_transit`` is auto-retry exhaustion;
# ``declined`` / ``rejected`` are pre-storage refusals.
_FS_TERMINAL_STATUSES = (
    "purged",
    "withdrawn",
    "failed_in_transit",
    "declined",
    "rejected",
)


@router.get(
    "/foreign_storage/histories",
    dependencies=[Depends(verify_local_auth)],
    summary="Terminal-state foreign-storage rows (both depositor + host roles)",
    tags=["Foreign Storage"],
)
async def foreign_storage_histories() -> dict:
    """Combined view of deposits that have reached a terminal state.

    Returned for both ``role == "depositor"`` and ``role == "host"`` so
    the UI can render one panel for the user's complete history without
    a second round-trip. Sorted newest-first by ``purged_at`` if set,
    else ``created_at``.
    """
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit)
                    .filter(
                        ForeignStorageDeposit.status.in_(_FS_TERMINAL_STATUSES)
                    )
                    .order_by(ForeignStorageDeposit.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        counterparty_uuids = {
            (r.depositor_uuid if r.role == "host" else r.host_uuid)
            for r in rows
            if (r.depositor_uuid if r.role == "host" else r.host_uuid)
        }
        name_by_uuid = await _resolve_peer_display_names(db, counterparty_uuids)
    return {
        "histories": [
            {
                "deposit_id": r.deposit_id,
                "role": r.role,
                "counterparty_uuid": (
                    r.depositor_uuid if r.role == "host" else r.host_uuid
                ),
                "counterparty_display_name": name_by_uuid.get(
                    r.depositor_uuid if r.role == "host" else r.host_uuid, ""
                ),
                "status": r.status,
                "filename": r.filename or "",
                "total_bytes": int(r.total_bytes or 0),
                "created_at": r.created_at or "",
                "purged_at": r.purged_at or "",
                "evicted_at": r.evicted_at or "",
            }
            for r in rows
        ]
    }


async def _resolve_peer_display_names(db, uuids: set[str]) -> dict[str, str]:
    """Map peer UUID → best display name (live worker > DB col)."""
    from nexus.storage import Peer

    if not uuids:
        return {}
    out: dict[str, str] = {}
    rows = (
        (await db.execute(select(Peer).filter(Peer.ip.in_(list(uuids)))))
        .scalars()
        .all()
    )
    for p in rows:
        live = ""
        worker_entry = STATE.active_workers.get(p.ip, {})
        if worker_entry:
            live = str(
                worker_entry.get("stats", {}).get("user_display_name", "") or ""
            )
        out[p.ip] = live or (p.display_name or "")
    return out


@router.post(
    "/foreign_storage/retrieve/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="depositor pulls bytes back and decrypts to local disk",
    tags=["Foreign Storage"],
)
async def foreign_storage_retrieve(
    deposit_id: str, payload: dict
) -> dict:
    from sqlalchemy.orm import undefer

    from nexus.networking.storage_pump import build_storage_retrieve_open
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    password = str(payload.get("password") or "")
    save_to = str(payload.get("save_to_path") or "")
    if not (password and save_to):
        raise HTTPException(400, "password and save_to_path required")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        host_uuid = row.host_uuid
        chunk_count = int(row.chunk_count or 0)
        salt = bytes(row.salt or b"")
        sealed_manifest = bytes(row.encrypted_manifest or b"")

    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import derive_key, unseal_manifest

    # SECURITY: ALWAYS verify the password by unsealing the manifest.
    # The previous code would derive a key from any password and cache
    # it without checking — wrong passwords silently kicked off
    # downloads that decrypted to garbage, and worse, the wrong key
    # poisoned the in-RAM cache so subsequent Share View shipped a
    # bad key to the host.
    if not salt:
        raise HTTPException(409, "no salt stored for deposit")
    if not sealed_manifest:
        raise HTTPException(
            409,
            "no sealed manifest persisted; cannot verify password "
            "for this deposit",
        )
    derived = derive_key(password, salt)
    try:
        unseal_manifest(derived, sealed_manifest)
    except Exception:
        # Don't trust whatever may have been cached previously — a poisoned
        # entry would have let a re-attempt sail through. Drop and refuse.
        foreign_storage_keys.drop(deposit_id)
        raise HTTPException(401, "wrong password")

    # Password verified — only now cache the key.
    foreign_storage_keys.store(deposit_id, derived, file_path="")

    # Round 1: if the user picked a directory, append the manifest's
    # filename so the file lands as <dir>/<original_name>.
    from pathlib import Path as _P
    save_path = _P(save_to)
    if save_path.is_dir():
        try:
            from nexus.security.deposit_crypto import unseal_manifest
            key_bytes = foreign_storage_keys.get(deposit_id) or b""
            manifest = unseal_manifest(key_bytes, sealed_manifest) if (key_bytes and sealed_manifest) else {}
            fname = str(manifest.get("filename") or "").strip()
            if fname:
                # Strip any path components from the manifest filename.
                save_to = str(save_path / _P(fname).name)
        except Exception:
            pass

    entry = foreign_storage_keys.get_entry(deposit_id) or {}
    entry["save_to"] = save_to
    entry["delete_after_download"] = bool(payload.get("delete_after_download"))

    await _send_to_peer(
        host_uuid,
        build_storage_retrieve_open(deposit_id, 0, chunk_count - 1),
    )
    await record_audit_event(
        "storage.deposit_retrieve_requested",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok", "chunks_requested": chunk_count}


@router.post(
    "/foreign_storage/forward/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="depositor asks current host to forward the bundle to a new peer",
    tags=["Foreign Storage"],
)
async def foreign_storage_forward(deposit_id: str, payload: dict) -> dict:
    from nexus.networking.storage_pump import build_storage_eviction_response
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    new_target = str(payload.get("new_target_peer") or "")
    if not new_target:
        raise HTTPException(400, "new_target_peer required")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        host_uuid = row.host_uuid

    await _send_to_peer(
        host_uuid,
        build_storage_eviction_response(
            deposit_id, action="forward", target_uuid=new_target
        ),
    )
    await record_audit_event(
        "storage.deposit_forward_requested",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"new_target={new_target}",
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/delete/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="depositor proactively deletes the deposit",
    tags=["Foreign Storage"],
)
async def foreign_storage_delete(deposit_id: str) -> dict:
    from nexus.networking.storage_pump import (
        build_storage_delete_now,
        build_storage_offer_cancelled,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.security.crypto import sign_bytes
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        prior_status = row.status
        host_uuid = row.host_uuid
        row.status = "withdrawn"
        await db.commit()

    # P2: auto-mode rows have no host yet — broadcast cancel to all
    # candidates and skip the delete_now (no chunks were transferred).
    if prior_status == "offering_multi":
        candidates = STATE.foreign_storage_auto_candidates.pop(deposit_id, [])
        STATE.foreign_storage_auto_started_at.pop(deposit_id, None)
        foreign_storage_keys.drop(deposit_id)
        for cand in candidates:
            try:
                await _send_to_peer(
                    cand,
                    build_storage_offer_cancelled(
                        deposit_id, reason="cancelled_by_user"
                    ),
                )
            except Exception:
                continue
        await record_audit_event(
            "storage.auto_offer_cancelled_by_user",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            details=f"candidates={len(candidates)}",
        )
        return {"status": "ok", "candidates_notified": len(candidates)}

    sig = sign_bytes("foreign_storage_delete", deposit_id, b"")
    delivered = await _send_to_peer(
        host_uuid, build_storage_delete_now(deposit_id, sig)
    )
    await record_audit_event(
        "storage.deposit_decommissioned",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok", "host_notified": bool(delivered)}


@router.post(
    "/foreign_storage/decrypt_rescued/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Decrypt a locally auto-rescued (encrypted) deposit with its password",
    tags=["Foreign Storage"],
)
async def foreign_storage_decrypt_rescued(deposit_id: str, payload: dict) -> dict:
    """Decrypt the ciphertext bundle auto-rescue pulled to local disk.

    Auto-rescue saves a locked deposit's encrypted chunks to
    ``rescued_deposit_dir(deposit_id)`` and marks the row
    ``rescued_encrypted``. Here the user supplies the deposit password: we
    verify it by unsealing the manifest, decrypt every chunk to the output
    file, wipe the encrypted bundle, and mark the row ``completed``.
    """
    import asyncio
    import shutil as _sh
    from pathlib import Path as _P

    from sqlalchemy.orm import undefer

    from nexus.networking.storage_pump import (
        rescued_deposit_dir,
        rescued_root,
    )
    from nexus.security.deposit_crypto import (
        decrypt_chunk,
        derive_key,
        unseal_manifest,
    )
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    password = str(payload.get("password") or "")
    if not password:
        raise HTTPException(400, "password required")
    save_to = str(payload.get("save_to_path") or "").strip()

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status != "rescued_encrypted":
            raise HTTPException(
                409,
                f"deposit not awaiting decrypt (status={row.status})",
            )
        chunk_count = int(row.chunk_count or 0)
        salt = bytes(row.salt or b"")
        sealed = bytes(row.encrypted_manifest or b"")
        filename = row.filename or deposit_id

    if not salt:
        raise HTTPException(409, "no salt stored for deposit")
    if not sealed:
        raise HTTPException(
            409, "no sealed manifest persisted; cannot verify password"
        )
    key = derive_key(password, salt)
    try:
        unseal_manifest(key, sealed)
    except Exception:
        raise HTTPException(401, "wrong password")

    dpath = rescued_deposit_dir(deposit_id)
    if not dpath.exists():
        raise HTTPException(409, "rescued chunks not found on disk")

    out_path = _P(save_to) if save_to else (rescued_root() / _P(filename).name)
    if out_path.is_dir():
        out_path = out_path / _P(filename).name

    def _decrypt() -> None:
        with open(out_path, "wb") as out:
            for idx in range(chunk_count):
                cp = dpath / f"chunk_{idx:08d}.enc"
                if not cp.exists():
                    raise FileNotFoundError(f"missing chunk {idx}")
                out.write(decrypt_chunk(key, cp.read_bytes(), idx))

    try:
        await asyncio.to_thread(_decrypt)
    except Exception as exc:
        raise HTTPException(500, f"decrypt failed: {exc}")

    # Plaintext is on disk now — drop the encrypted bundle and close the row.
    await asyncio.to_thread(_sh.rmtree, str(dpath), True)
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "completed"
            await db.commit()
    await record_audit_event(
        "storage.rescued_decrypted",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"out={out_path}",
    )
    return {"status": "ok", "path": str(out_path)}


@router.post(
    "/foreign_storage/auto_rescue_config/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Set or clear a deposit's per-deposit auto-rescue override",
    tags=["Foreign Storage"],
)
async def foreign_storage_auto_rescue_config(deposit_id: str, payload: dict) -> dict:
    """Write a per-deposit auto-rescue override (or revert to node defaults).

    ``payload`` may carry ``enabled`` (bool), ``rclone_targets`` (list) and
    ``dir`` (str); any omitted field keeps falling back to the node default.
    ``use_default: true`` drops the override entirely for this deposit.
    """
    from nexus.core.config import (
        effective_auto_rescue,
        normalize_bool,
        normalize_list_field,
    )

    overrides = dict(LOCAL_SETTINGS.get("fs_auto_rescue_overrides") or {})
    if payload.get("use_default"):
        overrides.pop(deposit_id, None)
    else:
        entry: dict = {}
        if "enabled" in payload:
            entry["enabled"] = normalize_bool(payload.get("enabled"), True)
        if "mode" in payload:
            entry["mode"] = str(payload.get("mode") or "")
        if "trigger" in payload:
            entry["trigger"] = str(payload.get("trigger") or "")
        if "days" in payload:
            entry["days"] = payload.get("days")
        if "cloud_cred" in payload:
            entry["cloud_cred"] = str(payload.get("cloud_cred") or "")
        if "rclone_targets" in payload:
            entry["rclone_targets"] = normalize_list_field(
                payload.get("rclone_targets")
            )
        if "dir" in payload:
            entry["dir"] = str(payload.get("dir") or "")
        # Run the entry through the shared sanitizer (mode/trigger/days bounds).
        from nexus.core.config import _normalize_auto_rescue_overrides
        overrides[deposit_id] = _normalize_auto_rescue_overrides(
            {deposit_id: entry}
        ).get(deposit_id, {})
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = overrides
    await save_local_settings_to_db()
    return {"status": "ok", "auto_rescue": effective_auto_rescue(deposit_id)}


@router.post(
    "/foreign_storage/purge/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Drop a withdrawn deposit row from the depositor's local DB",
    tags=["Foreign Storage"],
)
async def foreign_storage_purge(deposit_id: str) -> dict:
    """Two-step delete companion: removes a depositor row that is already
    in ``withdrawn`` state. The host copy was wiped by the earlier
    ``foreign_storage_delete`` call; this endpoint just clears the local
    history entry."""
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status != "withdrawn":
            raise HTTPException(
                409, f"deposit not withdrawn (status={row.status})"
            )
        await db.delete(row)
        await db.commit()
    await record_audit_event(
        "storage.deposit_history_purged",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/host_download/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Host pulls plaintext from local chunks using the granted view key",
    tags=["Foreign Storage"],
)
async def foreign_storage_host_download(deposit_id: str, payload: dict) -> dict:
    """Host-side download for a deposit the depositor has shared.

    Requires an active view grant: the AES key is already cached in
    ``foreign_storage_keys`` (delivered on the grant frame). Iterates the
    host's local ``chunk_*.enc`` files, decrypts each, and writes
    plaintext to ``save_to_path``. If the path is a directory, the
    manifest's filename is appended.
    """
    import asyncio
    from pathlib import Path as _P
    from sqlalchemy.orm import undefer

    from nexus.networking.storage_pump import deposit_dir
    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import decrypt_chunk, unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    save_to = str(payload.get("save_to_path") or "").strip()
    if not save_to:
        raise HTTPException(400, "save_to_path required")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if not (row.host_view_granted_at and row.host_view_granted_at > 0):
            raise HTTPException(403, "no view grant active for this deposit")
        chunk_count = int(row.chunk_count or 0)
        depositor_uuid = row.depositor_uuid
        sealed_manifest = bytes(row.encrypted_manifest or b"")

    key = foreign_storage_keys.get(deposit_id)
    if key is None:
        raise HTTPException(403, "view key not cached (depositor may have revoked)")

    save_path = _P(save_to)
    if save_path.is_dir():
        try:
            manifest = unseal_manifest(key, sealed_manifest) if sealed_manifest else {}
            fname = str(manifest.get("filename") or "").strip()
            if fname:
                save_path = save_path / _P(fname).name
        except Exception:
            pass

    dpath = deposit_dir(deposit_id, depositor_uuid)

    def _decrypt_to_disk() -> None:
        with open(save_path, "wb") as out:
            for idx in range(chunk_count):
                chunk_path = dpath / f"chunk_{idx:08d}.enc"
                if not chunk_path.exists():
                    raise FileNotFoundError(f"missing chunk {idx}")
                blob = chunk_path.read_bytes()
                out.write(decrypt_chunk(key, blob, idx))

    try:
        await asyncio.to_thread(_decrypt_to_disk)
    except Exception as exc:
        raise HTTPException(500, f"decrypt/write failed: {exc}")

    await record_audit_event(
        "storage.host_download_completed",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok", "save_to_path": str(save_path)}


###############################################################################
# Session-key unlock / lock + listing
###############################################################################


@router.post(
    "/foreign_storage/unlock/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="derive + cache the deposit AES key for preview / retrieve",
    tags=["Foreign Storage"],
)
async def foreign_storage_unlock(deposit_id: str, payload: dict) -> dict:
    """Verify the password by unsealing the manifest, then cache the key.

    Returns 401 if the password does not produce a key that unseals the
    encrypted manifest. The manifest is persisted at deposit time, so we
    can authenticate the password without round-tripping to the host.
    """
    from sqlalchemy.orm import undefer

    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import derive_key, unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    password = str(payload.get("password") or "")
    if not password:
        raise HTTPException(400, "password required")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        salt = bytes(row.salt or b"")
        sealed_manifest = bytes(row.encrypted_manifest or b"")

    if not salt:
        raise HTTPException(409, "no salt stored for deposit")

    # SECURITY: refuse without a sealed manifest — caching the wrong key
    # silently would corrupt subsequent preview / download attempts and,
    # worse, ship a useless key to the host on Share View.
    if not sealed_manifest:
        raise HTTPException(
            409,
            "no sealed manifest persisted; cannot verify password "
            "for this deposit",
        )
    derived = derive_key(password, salt)
    try:
        unseal_manifest(derived, sealed_manifest)
    except Exception:
        raise HTTPException(401, "wrong password")

    foreign_storage_keys.store(deposit_id, derived)
    await record_audit_event(
        "storage.deposit_unlocked",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok"}


async def _fail_in_transit_and_wipe_host(
    deposit_id: str, reason: str
) -> None:
    """P8.8: depositor-side terminal failure path.

    Flips the depositor row to ``failed_in_transit``, drops any cached
    key, and asks the host to wipe its chunks via the standard
    ``storage_delete_now`` frame. Best-effort across the wire — if the
    host is offline the abandoned-chunk purge pass will catch up.
    """
    from nexus.networking.storage_pump import build_storage_delete_now
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.security.crypto import sign_bytes
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    host_uuid = ""
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            host_uuid = row.host_uuid or ""
            row.status = "failed_in_transit"
            row.pause_reason = reason
            await db.commit()

    foreign_storage_keys.drop(deposit_id)
    if host_uuid:
        try:
            sig = sign_bytes("foreign_storage_delete", deposit_id, b"")
            await _send_to_peer(
                host_uuid, build_storage_delete_now(deposit_id, sig)
            )
        except Exception:
            pass
    await record_audit_event(
        "storage.transit_failed",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        severity="warning",
        details=f"reason={reason}",
    )


@router.post(
    "/foreign_storage/resume/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="P8: re-derive key after restart, point at the source file, allow resume",
    tags=["Foreign Storage"],
)
async def foreign_storage_resume(deposit_id: str, payload: dict) -> dict:
    """P8: depositor restarted; key is gone from RAM. User re-supplies
    the password and the path to the original file. We verify password
    via manifest-unseal, sanity-check file size against the manifest,
    cache the key + path, and reset ``last_progress_at`` so the
    lifecycle retry pass fires the resume on the next tick.

    Does not actually send any chunks — that's the lifecycle pass's job
    once the key is in the cache.
    """
    from datetime import datetime, timezone
    from pathlib import Path
    from sqlalchemy.orm import undefer

    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import derive_key, unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    password = str(payload.get("password") or "")
    file_path = str(payload.get("file_path") or "")
    if not password or not file_path:
        raise HTTPException(400, "password and file_path required")

    fpath = Path(file_path)
    if not fpath.is_file():
        raise HTTPException(404, f"file not found: {file_path}")
    file_size_now = fpath.stat().st_size

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.status in {"stored", "withdrawn", "purged", "failed_in_transit"}:
            raise HTTPException(
                409, f"deposit not resumable (status={row.status})"
            )
        salt = bytes(row.salt or b"")
        sealed_manifest = bytes(row.encrypted_manifest or b"")
        expected_bytes = int(row.total_bytes or 0)

    if not salt or not sealed_manifest:
        raise HTTPException(409, "deposit has no salt/manifest; cannot resume")

    derived = derive_key(password, salt)
    try:
        manifest = unseal_manifest(derived, sealed_manifest)
    except Exception:
        raise HTTPException(401, "wrong password")

    manifest_size = int(manifest.get("size") or 0)
    if manifest_size and manifest_size != file_size_now:
        raise HTTPException(
            409,
            f"file size mismatch: manifest says {manifest_size}, "
            f"selected file is {file_size_now}. Wrong file?",
        )
    if expected_bytes and expected_bytes != file_size_now:
        # Defensive: row's total_bytes should agree with manifest. If
        # not, trust the manifest but warn loudly.
        _log.warning(
            "[storage:%s] resume size disagreement: row=%d manifest=%d file=%d",
            deposit_id, expected_bytes, manifest_size, file_size_now,
        )

    # P8.8: authoritative file-identity check. Size match is necessary but
    # not sufficient (truncate+pad would slip past it); compare the full
    # SHA-256 against what we sealed at deposit time. Drift → terminal
    # failed_in_transit and wipe the host's chunks. The user picks a new
    # deposit if they want to re-share.
    manifest_sha = str(manifest.get("sha256") or "")
    if manifest_sha:
        import hashlib as _hashlib
        _h = _hashlib.sha256()
        with fpath.open("rb") as _fh:
            for _block in iter(lambda: _fh.read(1024 * 1024), b""):
                _h.update(_block)
        live_sha = _h.hexdigest()
        if live_sha != manifest_sha:
            await _fail_in_transit_and_wipe_host(
                deposit_id, reason="file_changed"
            )
            raise HTTPException(
                409,
                "file has changed since deposit; the deposit is now "
                "failed_in_transit. Start a new deposit if you want to "
                "share this file again.",
            )

    foreign_storage_keys.store(deposit_id, derived, file_path=str(fpath))

    # Reset last_progress_at so the retry pass's backoff window opens
    # immediately — user just confirmed intent, no need to wait.
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.last_progress_at = datetime.now(timezone.utc).isoformat()
            # Reset retry_count too — the user manually re-confirmed,
            # so previous automatic attempts shouldn't count against them.
            row.retry_count = 0
            await db.commit()

    await record_audit_event(
        "storage.transit_resume_armed",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"file={fpath.name}",
    )
    return {"status": "ok", "deposit_id": deposit_id}


@router.post(
    "/foreign_storage/lock/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="drop the cached deposit key (zero out bytes)",
    tags=["Foreign Storage"],
)
async def foreign_storage_lock(deposit_id: str) -> dict:
    from nexus.runtime import foreign_storage_keys, preview_pump
    from nexus.telemetry.audit import record_audit_event

    dropped = foreign_storage_keys.drop(deposit_id)
    preview_pump.drop_deposit(deposit_id)
    if dropped:
        await record_audit_event(
            "storage.deposit_locked",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
        )
    return {"status": "ok", "was_unlocked": dropped}


@router.get(
    "/foreign_storage/unlocked",
    dependencies=[Depends(verify_local_auth)],
    summary="list deposits with a cached key (no key material)",
    tags=["Foreign Storage"],
)
async def foreign_storage_unlocked() -> dict:
    from nexus.runtime import foreign_storage_keys

    return {"unlocked": foreign_storage_keys.list_unlocked()}


@router.get(
    "/foreign_storage/manifest/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="return decrypted manifest fields for an unlocked deposit",
    tags=["Foreign Storage"],
)
async def foreign_storage_manifest(deposit_id: str) -> dict:
    """Unseal the deposit's encrypted manifest using the cached key."""
    import mimetypes

    from sqlalchemy.orm import undefer

    from nexus.networking.storage_pump import CHUNK_PLAINTEXT_BYTES
    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session

    key = foreign_storage_keys.get(deposit_id)
    if key is None:
        raise HTTPException(401, "deposit is locked")

    # Also accept the host role when the depositor has granted
    # viewing rights — the host caches the deposit AES key and the sealed
    # manifest is shipped on the grant frame, so the host can render the
    # same metadata as the depositor would.
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role.in_(["depositor", "host"]),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.role == "host" and not int(row.host_view_granted_at or 0):
            raise HTTPException(404, "deposit not found")
        sealed = bytes(row.encrypted_manifest or b"")
        chunk_count = int(row.chunk_count or 0)

    if not sealed:
        raise HTTPException(409, "no manifest stored for deposit")

    try:
        manifest = unseal_manifest(key, sealed)
    except Exception:
        raise HTTPException(401, "manifest decrypt failed")

    filename = str(manifest.get("filename") or "")
    size = int(manifest.get("size") or 0)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "filename": filename,
        "size": size,
        "mime": mime,
        "chunk_count": chunk_count,
        "chunk_size": CHUNK_PLAINTEXT_BYTES,
    }


def _parse_range_header(header: str, total: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=START-END`` Range header. Returns ``(start, end)``
    inclusive, or ``None`` if absent / malformed / unsatisfiable.
    """
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec:
        spec = spec.split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    lo_s, hi_s = spec.split("-", 1)
    lo_s, hi_s = lo_s.strip(), hi_s.strip()
    try:
        if lo_s == "":
            # Suffix range: last N bytes.
            n = int(hi_s)
            if n <= 0 or total == 0:
                return None
            start = max(0, total - n)
            end = total - 1
        else:
            start = int(lo_s)
            end = int(hi_s) if hi_s else total - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total:
        return None
    end = min(end, total - 1)
    return (start, end)


@router.get(
    "/foreign_storage/preview/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="stream-decrypt deposit bytes for in-browser preview",
    tags=["Foreign Storage"],
)
async def foreign_storage_preview(
    deposit_id: str, request: Request
) -> StreamingResponse:
    """Stream plaintext bytes for an unlocked deposit, with HTTP Range support."""
    import mimetypes

    from sqlalchemy.orm import undefer

    from nexus.networking.storage_pump import (
        CHUNK_PLAINTEXT_BYTES,
        build_storage_retrieve_open,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys, preview_pump
    from nexus.security.deposit_crypto import unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    key = foreign_storage_keys.get(deposit_id)
    if key is None:
        raise HTTPException(401, "deposit is locked")

    # Also accept the host role when the depositor has shared
    # viewing rights — the host streams from local ciphertext instead of
    # going through the depositor → host fetch loop.
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role.in_(["depositor", "host"]),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if row.role == "host" and not int(row.host_view_granted_at or 0):
            raise HTTPException(404, "deposit not found")
        role = row.role
        host_uuid = row.host_uuid
        depositor_uuid = row.depositor_uuid
        sealed = bytes(row.encrypted_manifest or b"")
        chunk_count = int(row.chunk_count or 0)
        total_db = int(row.total_bytes or 0)

    try:
        manifest = unseal_manifest(key, sealed) if sealed else {}
    except Exception:
        raise HTTPException(401, "manifest decrypt failed")

    filename = str(manifest.get("filename") or "")
    total = int(manifest.get("size") or total_db or 0)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunk_size = CHUNK_PLAINTEXT_BYTES

    range_header = request.headers.get("range") or ""
    rng = _parse_range_header(range_header, total) if range_header else None
    if range_header and rng is None:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}"},
        )

    start, end = rng if rng is not None else (0, max(0, total - 1))
    length = (end - start + 1) if total > 0 else 0
    first_chunk = start // chunk_size
    last_chunk = end // chunk_size if total > 0 else 0

    async def _request_open(host: str, idx: int) -> None:
        await _send_to_peer(
            host, build_storage_retrieve_open(deposit_id, idx, idx)
        )

    # Host-side preview reads ciphertext from local disk and
    # decrypts in-process. No round-trip required.
    async def _read_host_chunk(idx: int) -> bytes:
        from pathlib import Path

        from nexus.core import cache_dir, get_node_port
        from nexus.security.deposit_crypto import decrypt_chunk

        chunk_path = (
            cache_dir(get_node_port())
            / "foreign_storage"
            / depositor_uuid
            / deposit_id
            / f"chunk_{idx:08}.enc"
        )
        blob = await asyncio.to_thread(Path(chunk_path).read_bytes)
        return decrypt_chunk(key, blob, idx)

    async def _stream():
        try:
            for idx in range(first_chunk, last_chunk + 1):
                if idx >= chunk_count:
                    break
                if role == "host":
                    plaintext = await _read_host_chunk(idx)
                else:
                    plaintext = await preview_pump.fetch_plaintext(
                        deposit_id,
                        key,
                        host_uuid,
                        idx,
                        request_open=_request_open,
                    )
                lo = (start - idx * chunk_size) if idx == first_chunk else 0
                hi = (
                    (end - idx * chunk_size + 1)
                    if idx == last_chunk
                    else len(plaintext)
                )
                yield plaintext[max(0, lo) : max(0, hi)]
        except Exception as exc:
            await record_audit_event(
                "storage.preview_decrypt_failed",
                actor=LOCAL_SETTINGS.get("node_uuid", ""),
                task_id=deposit_id,
                severity="error",
                details=str(exc)[:200],
            )
            raise

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
    }
    if rng is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(length)
        status_code = 206
    else:
        if total > 0:
            headers["Content-Length"] = str(total)
        status_code = 200

    await record_audit_event(
        "storage.preview_served",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return StreamingResponse(
        _stream(), status_code=status_code, headers=headers, media_type=mime
    )


###############################################################################
# C4: Secrets vault — node-local encrypted store for task/service env secrets
###############################################################################


@router.get(
    "/secrets",
    dependencies=[Depends(verify_local_auth)],
    summary="C4: list secret names + metadata (never the values)",
    tags=["Secrets"],
)
async def local_secrets_list() -> dict:
    from nexus.runtime import secrets_vault

    return {"secrets": await secrets_vault.list_secrets()}


@router.post(
    "/secrets",
    dependencies=[Depends(verify_local_auth)],
    summary="C4: create or replace a secret (value encrypted at rest)",
    tags=["Secrets"],
)
async def local_secrets_set(payload: dict) -> dict:
    from nexus.runtime import secrets_vault

    name = str(payload.get("name") or "")
    value = payload.get("value")
    if value is None:
        raise HTTPException(400, "value required")
    try:
        await secrets_vault.set_secret(
            name, str(value), str(payload.get("description") or "")
        )
    except secrets_vault.SecretError as exc:
        raise HTTPException(400, str(exc))
    await write_audit_event(
        "secret.set", actor=get_node_identity(), details=f"name={name}"
    )
    return {"status": "ok", "name": name}


@router.delete(
    "/secrets/{name}",
    dependencies=[Depends(verify_local_auth)],
    summary="C4: delete a secret",
    tags=["Secrets"],
)
async def local_secrets_delete(name: str) -> dict:
    from nexus.runtime import secrets_vault

    if not await secrets_vault.delete_secret(name):
        raise HTTPException(404, "secret not found")
    await write_audit_event(
        "secret.deleted", actor=get_node_identity(), details=f"name={name}"
    )
    return {"status": "ok"}


###############################################################################
# Cloud-credential management + cloud-eviction response
###############################################################################


@router.get(
    "/foreign_storage/cloud_credentials",
    dependencies=[Depends(verify_local_auth)],
    summary="list saved cloud-provider credentials (no secrets)",
    tags=["Foreign Storage"],
)
async def foreign_storage_cloud_credentials_list() -> dict:
    from nexus.storage import CloudCredential, get_session

    async with get_session() as db:
        rows = (
            (await db.execute(select(CloudCredential))).scalars().all()
        )
    return {
        "credentials": [
            {
                "id": r.id,
                "provider": r.provider,
                "label": r.label or "",
                "default_folder": r.default_folder or "",
                "created_at": r.created_at or "",
                "last_used_at": r.last_used_at or "",
            }
            for r in rows
        ]
    }


@router.post(
    "/foreign_storage/cloud_credentials",
    dependencies=[Depends(verify_local_auth)],
    summary="persist a new cloud-provider credential (encrypted at rest)",
    tags=["Foreign Storage"],
)
async def foreign_storage_cloud_credentials_create(payload: dict) -> dict:
    from nexus.security.cred_crypto import wrap_credential_blob
    from nexus.storage import CloudCredential, get_session
    from nexus.storage.cloud import PROVIDERS
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import timestamp

    provider = str(payload.get("provider") or "")
    label = str(payload.get("label") or "")
    creds_json = str(payload.get("credential_json") or "")
    default_folder = str(payload.get("default_folder") or "")
    if not (provider and creds_json):
        raise HTTPException(400, "provider and credential_json required")
    provider_cls = PROVIDERS.get(provider)
    if provider_cls is None:
        raise HTTPException(400, f"unknown provider: {provider}")
    try:
        provider_cls.from_credential_json(creds_json.encode("utf-8"))
    except NotImplementedError:
        raise HTTPException(400, f"provider {provider} not yet supported")
    except Exception as exc:
        raise HTTPException(400, f"invalid credential json: {exc}")

    cred_id = uuid.uuid4().hex
    blob = wrap_credential_blob(creds_json.encode("utf-8"))

    async with get_session() as db:
        db.add(
            CloudCredential(
                id=cred_id,
                provider=provider,
                label=label,
                encrypted_blob=blob,
                default_folder=default_folder,
                created_at=timestamp(),
            )
        )
        await db.commit()

    await record_audit_event(
        "storage.cloud_credential_added",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=cred_id,
        details=f"provider={provider}",
    )
    return {"status": "ok", "id": cred_id}


@router.delete(
    "/foreign_storage/cloud_credentials/{cred_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="remove a saved cloud-provider credential",
    tags=["Foreign Storage"],
)
async def foreign_storage_cloud_credentials_delete(cred_id: str) -> dict:
    from nexus.storage import CloudCredential, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(CloudCredential).filter(CloudCredential.id == cred_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "credential not found")
        await db.execute(
            delete(CloudCredential).where(CloudCredential.id == cred_id)
        )
        await db.commit()

    await record_audit_event(
        "storage.cloud_credential_removed",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=cred_id,
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/evict_to_cloud/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="depositor accepts an eviction by streaming bundle to cloud",
    tags=["Foreign Storage"],
)
async def foreign_storage_evict_to_cloud(deposit_id: str, payload: dict) -> dict:
    """Send `storage_eviction_response{action: cloud}` with transit-wrapped creds."""
    from nexus.runtime.foreign_storage_cloud import (
        CloudEvictionError,
        request_cloud_eviction,
    )

    cred_id = str(payload.get("credential_id") or "")
    cloud_dest_override = str(payload.get("cloud_dest") or "")
    if not cred_id:
        raise HTTPException(400, "credential_id required")

    try:
        await request_cloud_eviction(deposit_id, cred_id, cloud_dest_override)
    except CloudEvictionError as exc:
        reason = str(exc)
        code = {
            "credential not found": 404,
            "deposit not found": 404,
            "no signing_key for host peer": 409,
            "could not reach host": 503,
        }.get(reason, 500)
        raise HTTPException(code, reason)
    return {"status": "ok"}


###############################################################################
# IP/copyright consent gate for cloud task-data sources
###############################################################################


@router.get(
    "/task_data_terms",
    dependencies=[Depends(verify_local_auth)],
    summary="cloud task-data IP/copyright terms + acceptance state",
    tags=["Task Lifecycle"],
)
async def local_task_data_terms_get() -> dict:
    from nexus.security.task_data_terms import (
        accepted_version,
        current_terms_text,
        current_version,
        is_current_accepted,
    )

    return {
        "version": current_version(),
        "text": current_terms_text(),
        "accepted_version": accepted_version(),
        "accepted": is_current_accepted(),
    }


@router.post(
    "/task_data_terms/accept",
    dependencies=[Depends(verify_local_auth)],
    summary="accept the cloud task-data IP/copyright terms",
    tags=["Task Lifecycle"],
)
async def local_task_data_terms_accept() -> dict:
    from nexus.security.task_data_terms import current_version

    version = current_version()
    LOCAL_SETTINGS["task_data_terms_accepted_version"] = version
    await save_local_settings_to_db()
    await write_audit_event(
        "task.data_terms_accepted",
        actor=get_node_identity(),
        details=f"version={version}",
    )
    return {"status": "ok", "version": version}


# ---------------------------------------------------------------------------
# Per-deposit host-view grants
# ---------------------------------------------------------------------------

@router.post(
    "/foreign_storage/grant_view/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="share viewing rights for a deposit with the host",
    tags=["Foreign Storage"],
)
async def foreign_storage_grant_view(deposit_id: str, payload: dict) -> dict:
    """Transit-wrap the deposit AES key and ship it to the host.

    Share View is permanent (no revoke), so we require a fresh password
    on every call — even when a key is cached locally — and verify it
    against the sealed manifest before transmitting. The sealed
    manifest piggy-backs on the same frame so the host can render
    preview metadata.
    """
    import os as _os

    from sqlalchemy.orm import undefer

    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.cred_crypto import (
        EVICTION_NONCE_BYTES,
        wrap_view_grant_for_transit,
    )
    from nexus.security.deposit_crypto import derive_key, unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.storage.repositories import get_peer_by_ip

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        host_uuid = row.host_uuid
        salt = bytes(row.salt or b"")
        sealed_manifest = bytes(row.encrypted_manifest or b"")

    # SECURITY: Share View is a permanent, irrevocable action. Always
    # require + verify a fresh password. Skipping verification when a
    # key is already cached lets any password (or none at all) sail
    # through if the user previously unlocked or downloaded — that was
    # the "any random word works" bug.
    password = str(payload.get("password") or "")
    if not password:
        raise HTTPException(401, "password required")
    if not salt:
        raise HTTPException(409, "no salt stored for deposit")
    if not sealed_manifest:
        raise HTTPException(
            409,
            "no sealed manifest persisted; cannot verify password "
            "for this deposit",
        )
    derived = derive_key(password, salt)
    try:
        unseal_manifest(derived, sealed_manifest)
    except Exception:
        raise HTTPException(401, "wrong password")
    deposit_key = derived

    host_peer = await get_peer_by_ip(host_uuid)
    host_signing_key = (host_peer.signing_key if host_peer else "") or ""
    if not host_signing_key:
        raise HTTPException(409, "no signing_key for host peer")

    grant_nonce = _os.urandom(EVICTION_NONCE_BYTES)
    wrapped = wrap_view_grant_for_transit(
        host_signing_key, grant_nonce, deposit_key
    )

    sent = await _send_to_peer(host_uuid, {
        "type": "storage_view_grant",
        "deposit_id": deposit_id,
        "grant_nonce_b64": base64.b64encode(grant_nonce).decode("ascii"),
        "deposit_key_blob_b64": base64.b64encode(wrapped).decode("ascii"),
        "sealed_manifest_b64": (
            base64.b64encode(sealed_manifest).decode("ascii")
            if sealed_manifest else ""
        ),
    })
    if not sent:
        raise HTTPException(503, "could not reach host")

    # Mark the row as "share pending host ack" so the UI can show a
    # transitional state instead of optimistically displaying "Shared".
    # ``host_view_granted_at`` is only stamped when the host echoes back
    # ``storage_view_grant_accepted``.
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one()
        row.host_view_granted_at = -1  # sentinel: pending ack
        await db.commit()

    await record_audit_event(
        "storage.view_grant_sent",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok"}


@router.post(
    "/foreign_storage/revoke_view/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="revoke host viewing rights for a deposit",
    tags=["Foreign Storage"],
)
async def foreign_storage_revoke_view(deposit_id: str) -> dict:
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        host_uuid = row.host_uuid
        row.host_view_granted_at = 0
        await db.commit()

    await _send_to_peer(host_uuid, {
        "type": "storage_view_revoke",
        "deposit_id": deposit_id,
    })
    await record_audit_event(
        "storage.view_grant_revoked",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
    )
    return {"status": "ok"}


@router.get(
    "/foreign_storage/granted_views",
    dependencies=[Depends(verify_local_auth)],
    summary="deposits currently shared with their host",
    tags=["Foreign Storage"],
)
async def foreign_storage_granted_views() -> dict:
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        rows = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.role == "depositor",
                    ForeignStorageDeposit.host_view_granted_at > 0,
                )
            )
        ).scalars().all()
    return {
        "granted": [
            {
                "deposit_id": r.deposit_id,
                "host_uuid": r.host_uuid,
                "granted_at": int(r.host_view_granted_at or 0),
            }
            for r in rows
        ]
    }


@router.post(
    "/foreign_storage/materialize_view/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Host-side: decrypt a view-granted deposit to plaintext on disk",
    tags=["Foreign Storage"],
)
async def foreign_storage_materialize_view(deposit_id: str) -> dict:
    """Once the depositor has granted view, the host can materialize the
    deposit to plaintext on disk by clicking Open in the UI.

    Lazy by design: nothing is written to disk until the host explicitly
    asks for it. Once written, the directory persists across depositor
    revoke — only the host can delete it (via the delete endpoint below).
    Idempotent: a second call returns the existing path without
    re-decrypting.
    """
    import os as _os
    from pathlib import Path

    from sqlalchemy.orm import undefer

    from nexus.core import cache_dir, get_node_port
    from nexus.runtime import foreign_storage_keys
    from nexus.security.deposit_crypto import decrypt_chunk, unseal_manifest
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit)
                .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                .filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        if not row.host_view_granted_at:
            raise HTTPException(403, "view not granted for this deposit")
        depositor_uuid = row.depositor_uuid or ""
        sealed_manifest = bytes(row.encrypted_manifest or b"")
        existing_dir = str(row.host_view_decrypted_dir or "")
        total_chunks = int(row.chunk_count or 0)

    if existing_dir:
        # Idempotent — already materialized once.
        p = Path(existing_dir)
        if p.exists():
            return {
                "status": "ok",
                "path": str(p),
                "already_materialized": True,
            }

    key = foreign_storage_keys.get(deposit_id)
    if key is None:
        raise HTTPException(409, "no view key cached (depositor revoked?)")

    if not sealed_manifest:
        raise HTTPException(409, "no sealed manifest persisted")

    try:
        manifest = unseal_manifest(key, sealed_manifest)
    except Exception:
        raise HTTPException(500, "manifest unseal failed")

    filename = str(manifest.get("filename") or f"{deposit_id}.bin")
    # Defensive: never let a manifest-supplied filename traverse out of
    # the materialize dir. Keep only the leaf component, drop slashes.
    filename = _os.path.basename(filename).replace("/", "_").replace("\\", "_")
    if not filename:
        filename = f"{deposit_id}.bin"

    chunks_dir = (
        cache_dir(get_node_port())
        / "foreign_storage"
        / depositor_uuid
        / deposit_id
    )
    if not chunks_dir.exists():
        raise HTTPException(409, "ciphertext directory missing")

    out_dir = (
        cache_dir(get_node_port())
        / "foreign_storage_shared"
        / depositor_uuid
        / deposit_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    def _do_decrypt() -> None:
        with open(out_path, "wb") as out_fh:
            for idx in range(total_chunks):
                chunk_path = chunks_dir / f"chunk_{idx:08d}.enc"
                if not chunk_path.exists():
                    raise FileNotFoundError(f"missing chunk {idx}")
                blob = chunk_path.read_bytes()
                out_fh.write(decrypt_chunk(key, blob, idx))

    try:
        await asyncio.to_thread(_do_decrypt)
    except Exception as exc:
        # Clean up partial write so the next attempt starts fresh.
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        raise HTTPException(500, f"decrypt failed: {exc}")

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one()
        row.host_view_decrypted_dir = str(out_dir)
        await db.commit()

    await record_audit_event(
        "storage.view_grant_materialized",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"path={out_dir}",
    )
    return {
        "status": "ok",
        "path": str(out_dir),
        "filename": filename,
        "already_materialized": False,
    }


@router.post(
    "/foreign_storage/open_shared_folder/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Host-side: open the decrypted-files folder in the OS file manager",
    tags=["Foreign Storage"],
)
async def foreign_storage_open_shared_folder(deposit_id: str) -> dict:
    """Best-effort OS open of the decrypted directory.

    The materialize endpoint above is what actually writes plaintext to
    disk — this just calls ``os.startfile`` (Windows) / ``xdg-open``
    (Linux) / ``open`` (macOS) so the host can browse the files. Fails
    gracefully if the dir doesn't exist (caller should materialize first).
    """
    import os as _os
    import subprocess
    import sys
    from pathlib import Path

    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        path = str(row.host_view_decrypted_dir or "")

    if not path or not Path(path).exists():
        raise HTTPException(409, "decrypted directory not present; materialize first")

    try:
        if sys.platform == "win32":
            _os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        raise HTTPException(500, f"could not open folder: {exc}")

    return {"status": "ok", "path": path}


@router.post(
    "/foreign_storage/delete_view_decrypted/{deposit_id}",
    dependencies=[Depends(verify_local_auth)],
    summary="Host-side: delete the plaintext copy of a view-granted deposit",
    tags=["Foreign Storage"],
)
async def foreign_storage_delete_view_decrypted(deposit_id: str) -> dict:
    """Remove the on-disk plaintext for a view-granted deposit.

    Ciphertext + cached AES key are unaffected — the host stays the
    custodian of the encrypted deposit for the depositor's TTL. Host can
    re-materialize later via :func:`foreign_storage_materialize_view`.
    """
    from pathlib import Path

    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "deposit not found")
        path = str(row.host_view_decrypted_dir or "")
        if path:
            try:
                p = Path(path)
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
        row.host_view_decrypted_dir = ""
        await db.commit()

    await record_audit_event(
        "storage.view_grant_disk_deleted",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"path={path}",
    )
    return {"status": "ok"}


__all__ = ["router"]
