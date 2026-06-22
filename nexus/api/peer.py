"""``/peer/*`` P2P protocol routes.

Ported from Phase-1/node_modified.py:

* ``/peer/cache_query`` — lines 6167-6175
* ``/peer/request_join`` — lines 6235-6287
* ``/peer/callback_remove`` — lines 6424-6444
* ``/peer/callback_accept`` — lines 6447-6490
* ``/peer/callback_reject_dual`` — lines 6493-6512
* ``/peer/callback_rotate_token`` — lines 6515-6535
* ``/peer/relay_heartbeat`` — lines 6765-6777
* ``/peer/pop_task`` — lines 6780-7002
* ``/peer/accept_offer/{task_id}`` — lines 7005-7097
* ``/peer/decline_offer/{task_id}`` — lines 7100-7131
* ``/peer/submit_result/{task_id}`` — lines 7134-7258
* ``/peer/disrupt_task/{task_id}`` — lines 7261-7292

All routes are thin — validation + delegation to the already-extracted
business layer (``nexus.tasks``, ``nexus.scheduler``, ``nexus.runtime``,
``nexus.security``, ``nexus.networking.peer_protocol``, ``nexus.telemetry``,
``nexus.storage``). Anything non-trivial belongs *there*, not here.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import secrets
import time
import uuid
import zipfile
from collections import defaultdict

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import undefer

from nexus.core import (
    LOCAL_SETTINGS,
    STATE,
    TERMINAL_STATES,
    cache_dir,
    get_node_identity,
    get_node_port,
    get_or_create_node_uuid,
    register_peer_uuid,
    resolve_ip_to_uuid,
    resolve_uuid_to_ip,
)
from nexus.networking.connection_manager import ws_manager
from nexus.networking.peer_protocol import (
    check_join_rate_limit,
    verify_callback_hmac,
    verify_join_hmac,
)
from nexus.runtime import (
    refresh_worker_task_leases,
    task_required_caps,
)
from nexus.scheduler import (
    read_task_manifest,
    select_task_for_worker,
)
from nexus.scheduler.manifest import _manifest_cache
from nexus.scheduler.reliability import record_worker_outcome
from nexus.security import (
    enforce_actual_size,
    enforce_content_length,
    get_max_result_bytes,
    sign_bytes,
    verify_signature,
    verify_trusted_peer,
)
from nexus.storage import Peer, TaskRecord, get_session
from nexus.storage.models import DirectMessage
from nexus.tasks import (
    add_task_timeline_event,
    enqueue_task,
    extract_task_metadata,
    mark_task_interrupted,
    set_task_lease,
    set_task_status,
    try_schedule_retry,
    upsert_remote_shadow_task,
)
from nexus.telemetry import incr_metric, task_log_append, write_audit_event
from nexus.ui.broadcaster import broadcast_ui_update
from nexus.utils import client_host, safe_extractall, timestamp

_log = logging.getLogger("nexus.api.peer")

router = APIRouter(prefix="/peer", tags=["Peer Protocol"])


# ---------------------------------------------------------------------------
# /peer/cache_query
# ---------------------------------------------------------------------------

@router.get(
    "/cache_query",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Query cached task payload by URI hash",
)
async def api_peer_cache_query(uri_hash: str) -> FileResponse:
    """Return the zipped bundle stored under ``<cache>/<uri_hash>.zip``."""
    cache_path = os.path.join(str(cache_dir(get_node_port())), f"{uri_hash}.zip")
    if os.path.exists(cache_path):
        return FileResponse(
            cache_path, media_type="application/zip", filename=f"{uri_hash}.zip"
        )
    raise HTTPException(404, detail="Cache miss")


# ---------------------------------------------------------------------------
# /peer/request_join  (incoming join requests from other peers)
# ---------------------------------------------------------------------------

@router.post(
    "/request_join",
    summary="Receive an incoming join request from a peer",
)
async def peer_request_join(request: Request, data: dict) -> dict:
    """Record an incoming join request; the local user accepts/rejects later."""
    # Rate limit by client IP
    if not check_join_rate_limit(client_host(request)):
        raise HTTPException(429, "Too many join requests. Try again later.")

    # Verify HMAC if grid key is configured
    if not verify_join_hmac(data):
        raise HTTPException(403, "Invalid or missing join_hmac")

    addr = data.get("requester_address")
    remote_name = str(data.get("display_name", "") or "").strip()[:50]
    remote_uuid = data.get("node_uuid", "")
    remote_fp = str(data.get("cert_fingerprint", "") or "").strip().lower() or None
    # Peer advertised their relay-pool URL set. Store on
    # their Peer row so peer_http_post can pick a shared relay when
    # routing to them. Defensive parsing: non-list / missing → empty.
    raw_remote_urls = data.get("relay_urls") or []
    if not isinstance(raw_remote_urls, list):
        raw_remote_urls = []
    remote_relay_urls = sorted({
        str(u).strip() for u in raw_remote_urls
        if isinstance(u, str) and u
    })
    import json as _json
    remote_relay_urls_blob = _json.dumps(remote_relay_urls)
    if remote_uuid and addr:
        register_peer_uuid(remote_uuid, addr)

    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == addr))
        ).scalar_one_or_none()

        # Fallback: check if we know them by their UUID
        if not peer and remote_uuid:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == remote_uuid))
            ).scalar_one_or_none()

        # Additional fallback for the dual-reverse case: a row originally
        # created by the local user typing an IP:port keeps that IP:port as
        # ``Peer.ip``, but the remote peer's ``my_address`` may now resolve
        # differently (UUID-based identity, LAN IP changed, NAT shifted).
        # Match on ``resolved_ip`` so the existing trusted row gets promoted
        # to ``trusted_pending_in`` instead of a duplicate pending row being
        # inserted next to it (the Accept button never appears in that case
        # because the dup may collide on a constraint or render second).
        if not peer and addr:
            peer = (
                await db.execute(
                    select(Peer).filter(Peer.resolved_ip == addr)
                )
            ).scalar_one_or_none()
        if not peer and remote_uuid:
            peer = (
                await db.execute(
                    select(Peer).filter(Peer.resolved_ip == remote_uuid)
                )
            ).scalar_one_or_none()

        if not peer:
            # Prefer the UUID as the primary identifier to unify LAN + Relay profiles
            primary_id = remote_uuid or addr
            new_peer = Peer(
                ip=primary_id,
                status="pending_in",
                role="master",
                display_name=remote_name,
                cert_fingerprint=remote_fp,
                peer_relay_urls=remote_relay_urls_blob,
            )
            if remote_uuid and addr and primary_id != addr:
                new_peer.resolved_ip = addr
            db.add(new_peer)
        elif peer.status == "trusted" and peer.role in ("worker", "master"):
            peer.status = "trusted_pending_in"
            if addr and not peer.resolved_ip:
                peer.resolved_ip = addr
            if remote_name:
                peer.display_name = remote_name
            if remote_fp:
                peer.cert_fingerprint = remote_fp
            peer.peer_relay_urls = remote_relay_urls_blob
        else:
            if remote_name:
                peer.display_name = remote_name
            if remote_fp:
                peer.cert_fingerprint = remote_fp
            peer.peer_relay_urls = remote_relay_urls_blob
        await db.commit()
    await broadcast_ui_update({"type": "state_changed"})
    from nexus.security.tls import get_local_fingerprint
    try:
        my_fp = get_local_fingerprint()
    except Exception:
        my_fp = ""
    # Advertise our own relay-pool URL set back so the
    # initiator can pick a shared relay routing to us.
    try:
        from nexus.networking.relay_client import my_relay_pool_urls

        my_relay_urls = await my_relay_pool_urls()
    except Exception:
        my_relay_urls = []
    return {
        "status": "received",
        "cert_fingerprint": my_fp,
        "relay_urls": my_relay_urls,
    }


# ---------------------------------------------------------------------------
# /peer/callback_remove  (remote peer revoked us)
# ---------------------------------------------------------------------------

@router.post(
    "/callback_remove",
    summary="Notification that a peer has revoked the connection",
)
async def peer_callback_remove(request: Request, data: dict) -> dict:
    if not check_join_rate_limit(client_host(request)):
        raise HTTPException(429, "Too many callback attempts. Try again later.")
    # Callback endpoints carry no per-peer auth token (the relationship may
    # not even be ``trusted`` yet), so the grid-key HMAC is the only thing
    # that proves this is a real peer rather than an unauthenticated caller
    # spoofing a revocation / token-leak request.
    if not verify_callback_hmac(data):
        raise HTTPException(403, "Invalid or missing callback_hmac")
    addr = data.get("responder_address")
    remote_uuid = data.get("node_uuid", "")
    if remote_uuid and addr:
        register_peer_uuid(remote_uuid, addr)
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == addr))
        ).scalar_one_or_none()
        if not peer and remote_uuid:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == remote_uuid))
            ).scalar_one_or_none()
        if peer:
            await db.execute(delete(Peer).where(Peer.ip == peer.ip))
            await db.commit()
    await broadcast_ui_update({"type": "state_changed"})
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /peer/callback_accept  (remote peer accepted our join; exchange tokens)
# ---------------------------------------------------------------------------

@router.post(
    "/callback_accept",
    summary="Receive acceptance and exchange auth tokens",
)
async def peer_callback_accept(request: Request, data: dict) -> dict:
    if not check_join_rate_limit(client_host(request)):
        raise HTTPException(429, "Too many callback attempts. Try again later.")
    # Without this HMAC check, any unauthenticated caller who knows a peer
    # identifier in our DB can mint a usable ``auth_token`` + ``signing_key``
    # (returned in the response) and impersonate that peer on every
    # ``/peer/*`` route gated by ``verify_trusted_peer``.
    if not verify_callback_hmac(data):
        raise HTTPException(403, "Invalid or missing callback_hmac")
    addr = data.get("responder_address")
    their_token = data.get("auth_token")
    their_signing_key = data.get("signing_key", "")
    remote_uuid = data.get("node_uuid", "")
    remote_fp = str(data.get("cert_fingerprint", "") or "").strip().lower() or None
    if remote_uuid and addr:
        register_peer_uuid(remote_uuid, addr)
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == addr))
        ).scalar_one_or_none()
        if not peer and remote_uuid:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == remote_uuid))
            ).scalar_one_or_none()
        if peer:
            peer.their_auth_token = their_token
            peer.my_auth_token = peer.my_auth_token or str(uuid.uuid4())
            # Negotiate a shared signing key: prefer the one sent by the
            # acceptor, generate our own if none provided. Both sides end up
            # with the same key.
            if their_signing_key:
                peer.signing_key = their_signing_key
            elif not peer.signing_key:
                peer.signing_key = secrets.token_hex(32)
            if remote_fp:
                peer.cert_fingerprint = remote_fp
            if peer.status in ("trusted", "trusted_pending_out") and peer.role in (
                "master",
                "worker",
                "dual",
            ):
                peer.role = "dual"
                peer.status = "trusted"
            else:
                peer.status = "trusted"
                peer.role = "worker"
            await db.commit()
            await broadcast_ui_update({"type": "state_changed"})
            from nexus.security.tls import get_local_fingerprint
            try:
                my_fp = get_local_fingerprint()
            except Exception:
                my_fp = ""
            return {
                "status": "ok",
                "auth_token": peer.my_auth_token,
                "signing_key": peer.signing_key,
                "cert_fingerprint": my_fp,
            }
    raise HTTPException(404)


# ---------------------------------------------------------------------------
# /peer/callback_reject_dual  (remote declined dual upgrade)
# ---------------------------------------------------------------------------

@router.post(
    "/callback_reject_dual",
    summary="Notification that dual-connect upgrade was rejected",
)
async def peer_callback_reject_dual(request: Request, data: dict) -> dict:
    if not check_join_rate_limit(client_host(request)):
        raise HTTPException(429, "Too many callback attempts. Try again later.")
    if not verify_callback_hmac(data):
        raise HTTPException(403, "Invalid or missing callback_hmac")
    addr = data.get("responder_address")
    remote_uuid = data.get("node_uuid", "")
    if remote_uuid and addr:
        register_peer_uuid(remote_uuid, addr)
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == addr))
        ).scalar_one_or_none()
        if not peer and remote_uuid:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == remote_uuid))
            ).scalar_one_or_none()
        if peer and peer.status == "trusted_pending_out":
            peer.status = "trusted"
            await db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /peer/callback_rotate_token  (remote rotated their token; update ours)
# ---------------------------------------------------------------------------

@router.post(
    "/callback_rotate_token",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Receive a rotated auth token from a trusted peer",
)
async def peer_callback_rotate_token(data: dict) -> dict:
    responder_address = data.get("responder_address")
    new_auth_token = str(data.get("new_auth_token", "")).strip()
    if not responder_address or not new_auth_token:
        raise HTTPException(400, detail="Invalid rotation payload.")
    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == responder_address))
        ).scalar_one_or_none()
        if not peer:
            raise HTTPException(404, detail="Peer not found for rotation.")
        peer.their_auth_token = new_auth_token
        await db.commit()
    await write_audit_event(
        "token_rotated_remote",
        actor=responder_address,
        details="Updated their_auth_token.",
    )
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /peer/relay_heartbeat  (heartbeat from a relay-connected worker)
# ---------------------------------------------------------------------------

@router.post(
    "/relay_heartbeat",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Receive heartbeat from a relay-connected worker",
    tags=["Task Lifecycle"],
)
async def api_relay_heartbeat(
    request: Request, worker_id: str = Depends(verify_trusted_peer)
) -> dict:
    data = await request.json()
    stats = data.get("stats", {})
    stats["connection_type"] = "relay"
    async with STATE.worker_state_lock:
        STATE.active_workers[worker_id] = {"stats": stats, "last_seen": time.time()}
    await refresh_worker_task_leases(worker_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /peer/storage_inbox (HTTP fallback for storage_* frames when no WS)
# ---------------------------------------------------------------------------

@router.post(
    "/storage_inbox",
    dependencies=[Depends(verify_trusted_peer)],
    summary="receive a storage_* frame from a trusted peer over HTTP",
)
async def peer_storage_inbox(
    payload: dict, peer_id: str = Depends(verify_trusted_peer)
) -> dict:
    """Route a storage frame through the foreign-storage workflow.

    Mirrors the WS dispatch path in :mod:`nexus.networking.tunnel` so deposits
    work between trusted peers even when neither side has an active worker
    websocket open (common for pure pairing relationships that haven't
    elected ``dual`` mode).
    """
    ftype = str(payload.get("type") or "")
    if not ftype.startswith("storage_"):
        raise HTTPException(400, "only storage_* frames accepted here")
    from nexus.runtime.foreign_storage_workflow import workflow_handler

    await workflow_handler(peer_id, payload)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /peer/foreign_storage_capacity (trusted peers query free GB)
# ---------------------------------------------------------------------------

@router.get(
    "/foreign_storage_capacity",
    dependencies=[Depends(verify_trusted_peer)],
    summary="report this node's foreign-storage capacity to a trusted peer",
)
async def peer_foreign_storage_capacity(
    worker_id: str = Depends(verify_trusted_peer),
) -> dict:
    from nexus.runtime.foreign_storage_quota import (
        disk_free_gb,
        effective_free_gb,
        is_effectively_accepting,
        used_gb,
    )

    accepting = is_effectively_accepting()
    return {
        "accepting": accepting,
        "pledge_gb": float(LOCAL_SETTINGS.get("storage_max_total_gb", 5) or 5),
        "used_gb": round(used_gb(), 3),
        "free_gb": round(effective_free_gb() if accepting else 0.0, 3),
        "disk_free_gb": round(disk_free_gb(), 3),
    }


# ---------------------------------------------------------------------------
# /peer/pop_task  (worker pulls next queued task)
# ---------------------------------------------------------------------------

@router.get(
    "/pop_task",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Worker pulls the next queued task for execution",
    tags=["Task Lifecycle"],
)
async def api_pop_task(worker_id: str = Depends(verify_trusted_peer)) -> Response:
    """Select the best queued task for *worker_id* and return it.

    Behaviour matches Phase-1 exactly:
    * direct dispatch unless consent mode is enabled on either side;
    * task bundle is a zip containing ``payload.zip`` and optionally
      ``checkpoint.zip``, signed per-peer with the shared signing key.
    """
    master_consent = bool(LOCAL_SETTINGS.get("require_worker_consent", False))

    async with STATE.task_assign_lock:
        async with get_session() as db:
            queued_tasks = (
                (
                    await db.execute(
                        select(TaskRecord)
                        .filter(TaskRecord.status == "queued")
                        .options(
                            undefer(TaskRecord.payload),
                            undefer(TaskRecord.checkpoint_payload),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not queued_tasks:
                return Response(status_code=204)

            # Skip tasks already offered to THIS worker; better-fit supersession
            # is done below.
            async with STATE.pending_offers_lock:
                my_offered_ids = {
                    tid
                    for tid, o in STATE.pending_task_offers.items()
                    if o["worker_id"] == worker_id
                }
            queued_tasks = [t for t in queued_tasks if t.id not in my_offered_ids]
            if not queued_tasks:
                return Response(
                    status_code=204, headers={"X-Dispatch-Wait": "offered"}
                )

            # Filter out (task, worker) pairs that hit the consent-strike limit.
            max_strikes = int(LOCAL_SETTINGS.get("consent_max_strikes", 3) or 3)
            if max_strikes > 0:
                queued_tasks = [
                    t
                    for t in queued_tasks
                    if STATE.consent_strikes.get((t.id, worker_id), 0) < max_strikes
                ]
                if not queued_tasks:
                    return Response(
                        status_code=204,
                        headers={"X-Dispatch-Wait": "strikes_exhausted"},
                    )

            # Pre-warm manifest cache while payloads are still undeferred.
            for t in queued_tasks:
                if t.id not in _manifest_cache:
                    read_task_manifest(t.payload, cache_key=t.id)

            worker_info = STATE.active_workers.get(worker_id, {})
            worker_wants_consent = bool(
                worker_info.get("stats", {}).get("require_consent", False)
            )
            worker_consent_timeout = int(
                worker_info.get("stats", {}).get("consent_timeout_sec", 10)
            )
            consent_mode = master_consent or worker_wants_consent
            consent_timeout = (
                worker_consent_timeout
                if worker_wants_consent
                else int(LOCAL_SETTINGS.get("consent_timeout_sec", 10))
            )

            processing_by_master: dict[str, int] = defaultdict(int)
            # DAG #2 anti-affinity: which workers are already running a step of
            # each workflow (parent_id), so "one step per node" can skip them.
            workflow_busy: dict[str, set[str]] = defaultdict(set)
            for proc in (
                (
                    await db.execute(
                        select(TaskRecord).filter(TaskRecord.status == "processing")
                    )
                )
                .scalars()
                .all()
            ):
                processing_by_master[
                    extract_task_metadata(proc).get("requested_by") or "unknown"
                ] += 1
                if proc.parent_id and proc.worker:
                    workflow_busy[proc.parent_id].add(proc.worker)

            # Build the group → eligible-worker pool for any task
            # scoped to one or more groups, so group-scoped tasks only offer
            # to members holding task:run.
            group_ids_needed: set[str] = set()
            for t in queued_tasks:
                for g in (extract_task_metadata(t).get("target_groups") or []):
                    group_ids_needed.add(g)
            group_pool = None
            if group_ids_needed:
                from nexus.runtime.group_compute import build_group_worker_pool
                group_pool = await build_group_worker_pool(group_ids_needed)

            task = select_task_for_worker(
                worker_id, queued_tasks, STATE.active_workers,
                processing_by_master, group_pool, dict(workflow_busy),
            )
            if not task:
                return Response(
                    status_code=204, headers={"X-Dispatch-Wait": "capacity"}
                )

            # Supersede any pending offer that had this task held for another worker
            async with STATE.pending_offers_lock:
                existing_offer = STATE.pending_task_offers.pop(task.id, None)
            if existing_offer:
                old_worker = existing_offer["worker_id"]
                task.logs = (task.logs or "") + (
                    f"[{timestamp()}] [DISPATCH] Offer to {old_worker} "
                    f"superseded — better fit found: {worker_id}.\n"
                )
                add_task_timeline_event(
                    task, "offer_superseded", f"{old_worker} -> {worker_id}"
                )
                _log.info(
                    "[CONSENT] Offer for %s redirected from %s to %s",
                    task.id, old_worker, worker_id,
                )

            # GPU fallback logging: non-GPU task dispatched to a GPU worker because
            # there are no free non-GPU workers right now.
            manifest = _manifest_cache.get(task.id, {})
            req_caps = task_required_caps(task)
            w_stats = worker_info.get("stats", {})
            w_caps = w_stats.get("capabilities", {}) if isinstance(
                w_stats.get("capabilities"), dict
            ) else {}
            if not req_caps["require_gpu"] and bool(w_caps.get("gpu", False)):
                has_non_gpu_workers = any(
                    not bool(
                        (
                            info.get("stats", {}).get("capabilities", {})
                            if isinstance(info.get("stats", {}).get("capabilities"), dict)
                            else {}
                        ).get("gpu", False)
                    )
                    for cid, info in STATE.active_workers.items()
                    if time.time() - float(info.get("last_seen", 0) or 0) <= 12
                )
                if not has_non_gpu_workers:
                    task.logs = (task.logs or "") + (
                        f"[{timestamp()}] [DISPATCH] No non-GPU workers available. "
                        f"Using GPU worker {worker_id} for non-GPU task.\n"
                    )
                    add_task_timeline_event(task, "gpu_fallback", worker_id)

            if consent_mode:
                consent_source = "worker" if worker_wants_consent else "master"
                offer_summary = {
                    "task_id": task.id,
                    "runtime": manifest.get("runtime", "docker"),
                    "image": manifest.get("image", ""),
                    "entrypoint": manifest.get("entrypoint", ""),
                    "ram_limit_mb": int(manifest.get("ram_limit_mb", 512) or 512),
                    "cpu_limit_pct": int(manifest.get("cpu_limit_pct", 100) or 100),
                    "require_gpu": req_caps["require_gpu"],
                    "consent_timeout_sec": consent_timeout,
                }
                async with STATE.pending_offers_lock:
                    STATE.pending_task_offers[task.id] = {
                        "worker_id": worker_id,
                        "offered_at": time.time(),
                        "timeout": consent_timeout,
                    }
                task.logs = (task.logs or "") + (
                    f"[{timestamp()}] [CONSENT] Task offered to {worker_id} "
                    f"(requested by {consent_source}). "
                    f"Awaiting acceptance (timeout: {consent_timeout}s).\n"
                )
                add_task_timeline_event(task, "task_offered", worker_id)
                _log.info(
                    "[CONSENT] Sent offer for task %s to worker %s",
                    task.id, worker_id,
                )
                await db.commit()
                return Response(
                    content=json.dumps(offer_summary).encode(),
                    media_type="application/json",
                    status_code=202,
                    headers={"X-Task-ID": task.id, "X-Dispatch-Mode": "consent"},
                )

            # Direct assignment (no consent)
            if not set_task_status(task, "processing", f"Lease assigned to {worker_id}."):
                return Response(
                    status_code=204, headers={"X-Dispatch-Wait": "state_conflict"}
                )
            task.worker = worker_id
            set_task_lease(task, worker_id)
            task.logs = (task.logs or "") + (
                f"[{timestamp()}] [NETWORK] Pulled securely by {worker_id}.\n"
            )
            add_task_timeline_event(task, "lease_assigned", worker_id)
            incr_metric("tasks_dispatched")
            await write_audit_event(
                "task_dispatched",
                actor=get_node_identity(),
                task_id=task.id,
                details=f"worker={worker_id}",
            )
            await db.commit()
            payload, task_env, checkpoint = (
                task.payload, task.env_vars, task.checkpoint_payload,
            )

    return await _build_task_bundle_response(task.id, worker_id, payload, task_env, checkpoint)


# ---------------------------------------------------------------------------
# /peer/accept_offer/{task_id}
# ---------------------------------------------------------------------------

@router.post(
    "/accept_offer/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Worker accepts a consent task offer",
    tags=["Task Lifecycle"],
)
async def api_accept_offer(
    task_id: str, worker_id: str = Depends(verify_trusted_peer)
) -> Response:
    """Master side of the consent flow: record acceptance + return the bundle."""
    async with STATE.pending_offers_lock:
        offer = STATE.pending_task_offers.pop(task_id, None)
    if not offer:
        raise HTTPException(
            404,
            detail="No pending offer for this task (expired or already accepted).",
        )
    if offer["worker_id"] != worker_id:
        async with STATE.pending_offers_lock:
            STATE.pending_task_offers[task_id] = offer
        raise HTTPException(403, detail="This offer was made to a different worker.")

    async with STATE.task_assign_lock:
        async with get_session() as db:
            task = (
                await db.execute(
                    select(TaskRecord)
                    .filter(TaskRecord.id == task_id)
                    .options(
                        undefer(TaskRecord.payload),
                        undefer(TaskRecord.checkpoint_payload),
                    )
                )
            ).scalar_one_or_none()
            if not task or task.status != "queued":
                return Response(
                    status_code=204, headers={"X-Dispatch-Wait": "state_conflict"}
                )

            if not set_task_status(task, "processing", f"Offer accepted by {worker_id}."):
                return Response(
                    status_code=204, headers={"X-Dispatch-Wait": "state_conflict"}
                )
            task.worker = worker_id
            set_task_lease(task, worker_id)
            task.logs = (task.logs or "") + (
                f"[{timestamp()}] [CONSENT] Offer accepted by {worker_id}. Task assigned.\n"
            )
            add_task_timeline_event(task, "offer_accepted", worker_id)
            _log.info("[CONSENT] Worker %s accepted offer for task %s", worker_id, task_id)
            incr_metric("tasks_dispatched")
            await write_audit_event(
                "task_dispatched",
                actor=get_node_identity(),
                task_id=task.id,
                details=f"worker={worker_id} (consent)",
            )
            await db.commit()
            payload, task_env, checkpoint = (
                task.payload, task.env_vars, task.checkpoint_payload,
            )

    return await _build_task_bundle_response(task.id, worker_id, payload, task_env, checkpoint)


# ---------------------------------------------------------------------------
# /peer/decline_offer/{task_id}
# ---------------------------------------------------------------------------

@router.post(
    "/decline_offer/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Worker declines a consent task offer",
    tags=["Task Lifecycle"],
)
async def api_decline_offer(
    task_id: str, worker_id: str = Depends(verify_trusted_peer)
) -> dict:
    async with STATE.pending_offers_lock:
        offer = STATE.pending_task_offers.pop(task_id, None)
    if not offer:
        return {"status": "ok", "detail": "Offer already expired or accepted."}
    STATE.consent_strikes[(task_id, worker_id)] = (
        STATE.consent_strikes.get((task_id, worker_id), 0) + 1
    )
    max_strikes = int(LOCAL_SETTINGS.get("consent_max_strikes", 3) or 3)
    strikes = STATE.consent_strikes[(task_id, worker_id)]
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if task:
            strike_note = f" (strike {strikes}/{max_strikes})" if max_strikes > 0 else ""
            task.logs = (task.logs or "") + (
                f"[{timestamp()}] [CONSENT] Offer declined by {worker_id}{strike_note}. "
                "Task re-entering pool.\n"
            )
            add_task_timeline_event(task, "offer_declined", worker_id)
            _log.info(
                "[CONSENT] Worker %s declined offer for task %s%s",
                worker_id, task_id, strike_note,
            )
            await db.commit()
    incr_metric("task_offers_declined")
    await write_audit_event("offer_declined", actor=worker_id, task_id=task_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /peer/submit_result/{task_id}
# ---------------------------------------------------------------------------

@router.post(
    "/submit_result/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Worker submits execution result for a task",
    tags=["Task Lifecycle"],
)
async def api_submit_result(
    task_id: str,
    request: Request,
    status: str = Form(...),
    logs: str = Form(""),
    result_sig: str = Form(""),
    elapsed_secs: int = Form(0),
    worker_pubkey: str = Form(""),
    worker_proof: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    max_bytes = get_max_result_bytes()
    enforce_content_length(request, max_bytes, label="Result payload")
    file_bytes = await file.read()
    enforce_actual_size(file_bytes, max_bytes, label="Result payload")
    status = str(status or "").lower().strip()
    if status not in {"success", "failed", "fatal_error", "preempted"}:
        raise HTTPException(400, detail="Invalid status.")

    # Look up the submitter's signing key to verify the result signature
    cluster_key = request.headers.get("X-Cluster-Key") or ""
    _peer_skey = ""
    if cluster_key:
        async with get_session() as db:
            submitter = (
                await db.execute(
                    select(Peer).filter(
                        Peer.my_auth_token == cluster_key,
                        Peer.status == "trusted",
                    )
                )
            ).scalar_one_or_none()
        _peer_skey = (submitter.signing_key or "") if submitter else ""

    if not verify_signature(
        result_sig, "result", task_id, file_bytes, status, key=_peer_skey
    ):
        await write_audit_event(
            "result_signature_rejected",
            actor="remote-worker",
            task_id=task_id,
            severity="warning",
            details="Invalid result signature.",
        )
        raise HTTPException(403, detail="Result signature invalid.")

    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if not task:
            return {"message": "Accepted."}

        if task.id in STATE.disrupted_master_tasks:
            task.logs = (task.logs or "") + (
                f"{logs}\n[{timestamp()}] [MASTER] Late payload ignored (disrupted).\n"
            )
            await db.commit()
            return {"message": "Ignored."}
        if str(task.status or "").lower() in TERMINAL_STATES:
            return {"message": "Ignored."}

        old_worker = task.worker
        if status == "success":
            safe_task_dir = os.path.join(
                "completed_tasks",
                task_id.replace("..", "").replace("/", "_").replace("\\", "_"),
            )
            os.makedirs(safe_task_dir, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                safe_extractall(z, safe_task_dir)
            set_task_status(task, "completed", "Remote worker returned successful payload.")
            task.logs = (task.logs or "") + (
                f"{logs}\n[{timestamp()}] [NETWORK] Payload returned. "
                f"Status: {status.upper()}.\n"
            )
            incr_metric("tasks_completed")
            record_worker_outcome(old_worker, ok=True)
            # As the consumer, sign + distribute a usage receipt
            # crediting the worker (tamper-proof, replaces self-reported stats).
            try:
                from nexus.runtime.usage_receipts import issue_compute_receipt
                await issue_compute_receipt(
                    task, elapsed_secs, worker_pubkey, worker_proof
                )
            except Exception:
                _log.debug("usage receipt issue failed", exc_info=True)
            await write_audit_event(
                "task_completed",
                actor=get_node_identity(),
                task_id=task.id,
                details=f"worker={old_worker or 'unknown'}",
            )
        elif status == "preempted":
            task.checkpoint_payload = file_bytes
            task.worker = None
            if try_schedule_retry(
                task,
                "Worker preempted task. State saved to Checkpoint.",
                old_worker,
            ):
                incr_metric("tasks_preempted")
                task.logs = (task.logs or "") + (
                    f"{logs}\n[{timestamp()}] [MASTER] Task preempted & checkpointed. "
                    "Retry scheduled.\n"
                )
            else:
                set_task_status(task, "failed", "Preempted but retry budget exhausted.")
                task.logs = (task.logs or "") + (
                    f"{logs}\n[{timestamp()}] [MASTER] Preempted with no retries left.\n"
                )
                incr_metric("tasks_failed")
        else:
            task.worker = None
            record_worker_outcome(old_worker, ok=False)
            retry_applied = try_schedule_retry(
                task, f"Worker returned {status}.", old_worker
            )
            task.logs = (task.logs or "") + (
                f"{logs}\n[{timestamp()}] [NETWORK] Payload returned. "
                f"Status: {status.upper()}.\n"
            )
            if retry_applied:
                task.logs = (task.logs or "") + (
                    f"[{timestamp()}] [MASTER] Retry policy engaged.\n"
                )
            else:
                set_task_status(
                    task,
                    "failed",
                    f"Worker returned {status}; retry budget exhausted.",
                )
                incr_metric("tasks_failed")
                await write_audit_event(
                    "task_failed",
                    actor=get_node_identity(),
                    task_id=task.id,
                    severity="warning",
                    details=f"worker={old_worker or 'unknown'} status={status}",
                )

        task.worker = None if task.status != "processing" else task.worker
        await db.commit()
        STATE.disrupted_master_tasks.discard(task_id)
        if task.status == "queued":
            await enqueue_task(task.id)
            await ws_manager.broadcast_ping()
    return {"message": "Accepted."}


# ---------------------------------------------------------------------------
# /peer/task_log_chunk/{task_id}  (Batch D1: live log forwarding)
# ---------------------------------------------------------------------------

@router.post(
    "/task_log_chunk/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Worker streams an incremental log chunk for a running task",
    tags=["Task Lifecycle"],
)
async def api_task_log_chunk(
    task_id: str,
    chunk: str = Form(""),
) -> dict:
    """Append a worker-side log chunk to the master's rolling tail buffer.

    The chunk is best-effort live output while the task is running. The
    final, signed log still arrives via ``/peer/submit_result``; this
    endpoint just lets the UI tail the output without waiting for the
    task to finish.
    """
    if chunk:
        # 64 KiB ceiling per request — large enough for verbose builds,
        # small enough to keep memory usage bounded if a malicious peer
        # spams.
        if len(chunk) > 65536:
            chunk = chunk[-65536:]
        await task_log_append(task_id, chunk)
    return {"ok": True}


# ---------------------------------------------------------------------------
# /peer/disrupt_task/{task_id}
# ---------------------------------------------------------------------------

@router.post(
    "/disrupt_task/{task_id}",
    summary="Master sends disrupt signal to a worker running a task",
    tags=["Task Lifecycle"],
)
async def api_peer_disrupt_task(
    task_id: str, master_ip: str = Depends(verify_trusted_peer)
) -> dict:
    """Disrupt a task if and only if the requester is the originating master."""
    async with get_session() as db:
        task_record = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
        ).scalar_one_or_none()
        if task_record:
            meta = extract_task_metadata(task_record)
            task_owner = meta.get("requested_by", "")
            owner_resolved = resolve_uuid_to_ip(task_owner) if task_owner else ""
            requester_resolved = resolve_uuid_to_ip(master_ip)
            if (
                task_owner
                and task_owner != master_ip
                and owner_resolved != requester_resolved
            ):
                raise HTTPException(
                    403, "Only the originating master can disrupt this task"
                )

    interrupted = await mark_task_interrupted(task_id)
    worker_id = get_node_identity()
    disruption_log = (
        f"[{timestamp()}] [MASTER] Disruption requested by {master_ip}.\n"
        f"[{timestamp()}] [WORKER] Stop signal sent on {worker_id} for task {task_id}.\n"
    )
    await upsert_remote_shadow_task(
        master_ip, task_id, "failed", disruption_log, worker_id
    )
    await write_audit_event(
        "remote_disrupt_signal",
        actor=master_ip,
        task_id=task_id,
        details=f"interrupted={interrupted}",
    )
    return {"status": "ok", "interrupted": interrupted}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _build_task_bundle_response(
    task_id: str,
    worker_id: str,
    payload: bytes,
    task_env: str,
    checkpoint: bytes | None,
) -> Response:
    """Construct the signed zip bundle returned by ``pop_task``/``accept_offer``."""
    # --- INTEGRITY CHECK: verify original payload signature before dispatch ---
    try:
        env_dict = json.loads(task_env) if task_env else {}
    except (json.JSONDecodeError, TypeError):
        env_dict = {}

    manifest = _manifest_cache.get(task_id) or read_task_manifest(
        payload, cache_key=task_id
    )

    async with get_session() as db:
        peer = (
            await db.execute(select(Peer).filter(Peer.ip == worker_id))
        ).scalar_one_or_none()
    peer_skey = peer.signing_key if peer and peer.signing_key else ""

    # Pre-resolve service dependencies so the worker can open
    # dep tunnels and inject env vars before container.run.
    deps = manifest.get("depends_on") or []
    if (
        str(manifest.get("runtime", "")).lower() == "service"
        and isinstance(deps, list)
        and deps
    ):
        await _resolve_and_grant_deps(task_id, worker_id, deps, env_dict)

    # Transit-wrap referenced cloud credentials for this worker.
    await _attach_task_data_creds(manifest, peer_skey, env_dict, task_id, worker_id)

    task_env = json.dumps(env_dict)

    original_sig = env_dict.get("NEXUS_META_PAYLOAD_SIG", "")
    if original_sig and not verify_signature(
        original_sig, "task_bundle", task_id, payload
    ):
        _log.error(
            "Payload integrity check FAILED for task %s — possible tampering.",
            task_id,
        )
        async with get_session() as db:
            t = (
                await db.execute(select(TaskRecord).filter(TaskRecord.id == task_id))
            ).scalar_one_or_none()
            if t:
                t.status = "fatal_error"
                t.logs = (t.logs or "") + (
                    f"\n[{timestamp()}] [SECURITY] Payload integrity compromised "
                    "— dispatch blocked.\n"
                )
                await db.commit()
        raise HTTPException(500, detail="Payload integrity compromised — dispatch blocked.")

    out_io = io.BytesIO()
    with zipfile.ZipFile(out_io, "w") as out_z:
        out_z.writestr("payload.zip", payload)
        if checkpoint:
            out_z.writestr("checkpoint.zip", checkpoint)

    task_sig = sign_bytes("task_bundle", task_id, payload, key=peer_skey)
    return Response(
        content=out_io.getvalue(),
        media_type="application/zip",
        headers={"X-Task-ID": task_id, "X-Task-Env": task_env, "X-Task-Sig": task_sig},
    )


# ---------------------------------------------------------------------------
# Cloud-credential transit at dispatch
# ---------------------------------------------------------------------------

async def _attach_task_data_creds(
    manifest: dict,
    peer_skey: str,
    env_dict: dict,
    task_id: str,
    worker_id: str,
) -> None:
    """Inject ``NEXUS_TASK_DATA_CREDS`` into ``env_dict`` if needed.

    For each unique ``credential_id`` referenced by the manifest's
    ``data_sources`` / ``workspace_source``, we look up the at-rest
    blob, unwrap it, and transit-wrap with the worker peer's
    ``signing_key`` under a fresh 16-byte nonce. The worker decrypts
    in RAM and zeroizes after fetching.
    """
    sources: list[dict] = []
    raw_data = manifest.get("data_sources") or []
    if isinstance(raw_data, list):
        sources.extend(s for s in raw_data if isinstance(s, dict))
    ws = manifest.get("workspace_source")
    if isinstance(ws, dict):
        sources.append(ws)
    if not sources:
        return

    cred_ids: list[str] = []
    for s in sources:
        cid = str(s.get("credential_id") or "").strip()
        if cid and cid not in cred_ids:
            cred_ids.append(cid)
    if not cred_ids:
        return

    if not peer_skey:
        raise HTTPException(
            500, "missing peer signing key for cloud credential transit"
        )

    from nexus.security.cred_crypto import (
        unwrap_credential_blob,
        wrap_task_data_for_transit,
    )
    from nexus.storage import CloudCredential

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(CloudCredential)
                    .options(undefer(CloudCredential.encrypted_blob))
                    .filter(CloudCredential.id.in_(cred_ids))
                )
            )
            .scalars()
            .all()
        )
    rows_by_id = {row.id: row for row in rows}

    nonce = secrets.token_bytes(16)
    creds_out: dict[str, str] = {}
    for cid in cred_ids:
        row = rows_by_id.get(cid)
        if row is None:
            raise HTTPException(
                400, f"task {task_id}: cloud credential {cid!r} not found"
            )
        plaintext = bytearray(unwrap_credential_blob(bytes(row.encrypted_blob)))
        try:
            wrapped = wrap_task_data_for_transit(peer_skey, nonce, bytes(plaintext))
        finally:
            for i in range(len(plaintext)):
                plaintext[i] = 0
        creds_out[cid] = base64.b64encode(wrapped).decode("ascii")

    env_dict["NEXUS_TASK_DATA_CREDS"] = json.dumps(
        {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "credentials": creds_out,
        },
        separators=(",", ":"),
    )

    await write_audit_event(
        "task.data_source_transmitted",
        actor=worker_id,
        task_id=task_id,
        details=f"creds={len(creds_out)}",
    )


# ---------------------------------------------------------------------------
# Dep resolution + grant push at dispatch time
# ---------------------------------------------------------------------------

async def _lookup_dep_primary(dep_service_id: str) -> tuple[str, int] | None:
    """Return ``(primary_worker_id, container_port)`` for *dep_service_id*.

    Prefers the live ``service_records`` entry (kept current by promotion);
    falls back to the dispatched task's ``worker`` + manifest's first
    ``expose_ports`` entry. Returns ``None`` if the dep isn't running yet.
    """
    rec = STATE.service_records.get(dep_service_id)
    if rec:
        primary = str(rec.get("worker_id") or "")
        ports = rec.get("expose_ports") or []
        if primary and ports:
            return primary, int(ports[0])

    async with get_session() as db:
        task = (
            await db.execute(
                select(TaskRecord)
                .options(undefer(TaskRecord.payload))
                .filter(TaskRecord.id == dep_service_id)
            )
        ).scalar_one_or_none()
    if not task or not task.worker:
        return None
    manifest = read_task_manifest(task.payload, cache_key=task.id)
    ports = list(manifest.get("expose_ports") or [])
    if not ports:
        return None
    return str(task.worker), int(ports[0])


async def _resolve_and_grant_deps(
    consumer_task_id: str,
    consumer_worker_id: str,
    deps: list,
    env_dict: dict,
) -> None:
    """Inject ``NEXUS_DEP_<ALIAS>_*`` env vars and push grants for each dep.

    For each ``{service_id, alias}`` entry: look up the dep's primary, write
    the address into *env_dict*, push a ``service_dep_grant`` frame to the
    primary so it accepts tunnels from *consumer_worker_id*, and record the
    consumer in :data:`STATE.service_dependents` for failover propagation.
    """
    from nexus.networking.tunnel import _send_to_peer

    consumer_uuid = resolve_ip_to_uuid(consumer_worker_id) or ""

    for entry in deps:
        if not isinstance(entry, dict):
            continue
        dep_id = str(entry.get("service_id") or "").strip()
        alias = str(entry.get("alias") or "").strip().upper()
        if not dep_id or not alias:
            continue
        resolved = await _lookup_dep_primary(dep_id)
        if resolved is None:
            _log.warning(
                "[dep:%s] consumer=%s alias=%s: dep not running yet, skipping",
                dep_id,
                consumer_task_id,
                alias,
            )
            continue
        primary, port = resolved
        env_dict[f"NEXUS_DEP_{alias}_PRIMARY"] = primary
        env_dict[f"NEXUS_DEP_{alias}_PORT"] = str(port)

        peers = [consumer_worker_id]
        if consumer_uuid and consumer_uuid != consumer_worker_id:
            peers.append(consumer_uuid)
        try:
            await _send_to_peer(
                primary,
                {
                    "type": "service_dep_grant",
                    "task_id": dep_id,
                    "peers": peers,
                },
            )
        except Exception as exc:
            _log.debug("[dep:%s] grant push to %s failed: %s", dep_id, primary, exc)

        STATE.service_dependents.setdefault(dep_id, set()).add(consumer_task_id)


# ---------------------------------------------------------------------------
# /peer/snapshot_upload/{task_id} (primary -> master)
# ---------------------------------------------------------------------------

@router.post(
    "/snapshot_upload/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Primary worker uploads a service-replica snapshot to its master",
    tags=["Task Lifecycle"],
)
async def api_snapshot_upload(
    task_id: str,
    data: dict,
    worker_id: str = Depends(verify_trusted_peer),
) -> dict:
    import base64
    import hashlib

    b64 = str(data.get("b64", "") or "")
    if not b64:
        raise HTTPException(400, detail="snapshot body missing")
    try:
        zip_bytes = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, detail="snapshot body not base64")
    if len(zip_bytes) > get_max_result_bytes():
        raise HTTPException(413, detail="snapshot exceeds max_result_bytes")

    expected_sha = str(data.get("sha256", "") or "").lower()
    if expected_sha:
        actual_sha = hashlib.sha256(zip_bytes).hexdigest()
        if actual_sha != expected_sha:
            await write_audit_event(
                "service_snapshot_invalid",
                actor=worker_id,
                task_id=task_id,
                details=f"upload sha256 mismatch: expected {expected_sha}, got {actual_sha}",
            )
            raise HTTPException(400, detail="snapshot sha256 mismatch")

    record = STATE.service_records.get(task_id)
    if record is None:
        raise HTTPException(404, detail=f"unknown service task {task_id}")

    async with get_session() as db:
        task = (
            await db.execute(
                select(TaskRecord)
                .options(undefer(TaskRecord.payload))
                .filter(TaskRecord.id == task_id)
            )
        ).scalar_one_or_none()
        if task is not None:
            task.checkpoint_payload = zip_bytes
            await db.commit()

    standby_ips = list(record.get("standbys") or [])
    if not standby_ips and task is not None:
        # Lazy standby allocation: first snapshot triggers placement.
        manifest = read_task_manifest(task.payload, cache_key=task.id)
        replicas = int(manifest.get("replicas", 1) or 1)
        strategy = str(manifest.get("replica_strategy", "none") or "none").lower()
        if strategy == "snapshot" and replicas > 1:
            standby_ips = await _allocate_snapshot_standbys(
                task, manifest, primary=worker_id, count=replicas - 1
            )
            async with STATE.service_lock:
                rec = STATE.service_records.get(task_id)
                if rec is not None:
                    rec["standbys"] = list(standby_ips)
                    rec["replica_strategy"] = strategy
                    rec["replicas"] = replicas

    async with STATE.service_lock:
        rec = STATE.service_records.get(task_id)
        if rec is not None:
            rec["last_snapshot_at"] = time.time()
            rec["last_snapshot_bytes"] = len(zip_bytes)

    distribute_results: dict[str, bool] = {}
    if standby_ips:
        from nexus.runtime.service_replication import distribute_snapshot

        distribute_results = await distribute_snapshot(
            task_id, standby_ips, zip_bytes
        )

    return {
        "status": "ok",
        "bytes": len(zip_bytes),
        "standbys": distribute_results,
    }


async def _allocate_snapshot_standbys(
    task: "TaskRecord",
    manifest: dict,
    *,
    primary: str,
    count: int,
) -> list[str]:
    """Pick *count* standbys for a snapshot service and tell them to prepare."""
    from nexus.networking.tunnel import _send_to_peer
    from nexus.scheduler import select_top_n_workers
    from nexus.tasks.metadata import extract_task_metadata

    # Keep snapshot standbys inside the task's group scope.
    target_groups = extract_task_metadata(task).get("target_groups") or []
    group_pool = None
    if target_groups:
        from nexus.runtime.group_compute import build_group_worker_pool
        group_pool = await build_group_worker_pool(set(target_groups))

    chosen = select_top_n_workers(
        task, count, STATE.active_workers, exclude={primary},
        group_pool=group_pool,
    )
    if not chosen:
        _log.warning(
            "[snapshot:%s] no standby candidates (primary=%s, want=%d)",
            task.id,
            primary,
            count,
        )
        return []

    frame = {
        "type": "service_prepare_standby",
        "task_id": task.id,
        "manifest": dict(manifest),
    }
    delivered: list[str] = []
    for worker_id in chosen:
        try:
            ok = await _send_to_peer(worker_id, frame)
        except Exception:
            ok = False
        if ok:
            delivered.append(worker_id)
        else:
            _log.warning(
                "[snapshot:%s] could not reach standby %s", task.id, worker_id
            )
    _log.info(
        "[snapshot:%s] allocated standbys: %s (of %s)",
        task.id,
        delivered,
        chosen,
    )
    return delivered


# ---------------------------------------------------------------------------
# /peer/snapshot_load/{task_id} (master -> standby)
# ---------------------------------------------------------------------------

@router.post(
    "/snapshot_load/{task_id}",
    dependencies=[Depends(verify_trusted_peer)],
    summary="Standby worker stages a snapshot for a service replica",
    tags=["Task Lifecycle"],
)
async def api_snapshot_load(
    task_id: str,
    data: dict,
    worker_id: str = Depends(verify_trusted_peer),
) -> dict:
    import base64
    import hashlib

    from nexus.runtime.service_replication import load_snapshot

    b64 = str(data.get("b64", "") or "")
    if not b64:
        raise HTTPException(400, detail="snapshot body missing")
    try:
        zip_bytes = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, detail="snapshot body not base64")
    if len(zip_bytes) > get_max_result_bytes():
        raise HTTPException(413, detail="snapshot exceeds max_result_bytes")

    expected_sha = str(data.get("sha256", "") or "").lower()
    if expected_sha:
        actual_sha = hashlib.sha256(zip_bytes).hexdigest()
        if actual_sha != expected_sha:
            await write_audit_event(
                "service_snapshot_invalid",
                actor=worker_id,
                task_id=task_id,
                details=f"load sha256 mismatch: expected {expected_sha}, got {actual_sha}",
            )
            raise HTTPException(400, detail="snapshot sha256 mismatch")

    if task_id not in STATE.service_standbys:
        raise HTTPException(409, detail=f"not a standby for {task_id}")

    path = await load_snapshot(task_id, zip_bytes)
    return {"status": "ok", "bytes": len(zip_bytes), "path": str(path)}


# ---------------------------------------------------------------------------
# 1:1 direct messages
# ---------------------------------------------------------------------------


def _my_enc_pubkey() -> str:
    """This node's X25519 public key (hex) for sealing DMs to it."""
    from nexus.security.group_ecies import derive_x25519_pubkey_hex
    from nexus.security.group_keys import get_local_group_privkey

    return derive_x25519_pubkey_hex(get_local_group_privkey())


@router.post("/enc_pubkey", summary="Return this node's DM encryption pubkey")
async def peer_enc_pubkey() -> dict:
    """Serve our X25519 pubkey so a peer/co-member can seal DMs to us.

    Public by design — it's just a public key, and DMs are E2E-sealed to
    it. No trusted-peer gate so group co-members (not paired) can fetch it.
    """
    return {"enc_pubkey": _my_enc_pubkey()}


async def _fetch_and_store_peer_group_pubkey(from_uuid: str) -> str:
    """Best-effort: learn a known peer's group pubkey from its profile (resolved
    via our own UUID→addr map, so a spoofed UUID resolves to the *real* peer) and
    cache it on the Peer row. Returns '' if unreachable. Security F-005/F-007."""
    from nexus.api.local import _resolve_dm_target
    from nexus.networking.peer_http import peer_http_post

    addr = await _resolve_dm_target(from_uuid)
    if not addr or addr == from_uuid:
        return ""
    try:
        res = await peer_http_post(addr, "/peer/profile", {}, timeout=4.0)
    except Exception:
        return ""
    if res.get("status") != 200:
        return ""
    pub = str((res.get("body") or {}).get("pubkey") or "")
    if pub:
        ip = resolve_uuid_to_ip(from_uuid)
        async with get_session() as s:
            row = (await s.execute(select(Peer).where(
                (Peer.ip == from_uuid) | (Peer.ip == ip) | (Peer.resolved_ip == ip)
            ).limit(1))).scalar_one_or_none()
            if row is not None:
                row.peer_group_pubkey = pub
                await s.commit()
    return pub


async def _bound_sender_group_pubkey(from_uuid: str) -> str:
    """The group pubkey bound to *from_uuid*: a co-member's ``GroupMember.pubkey``
    or a paired peer's recorded ``Peer.peer_group_pubkey`` (learned from its
    profile on first contact). Returns '' for an unknown sender — trust binds to
    the crypto identity, never the gossiped UUID. Security F-007."""
    from nexus.storage.models import GroupMember

    async with get_session() as s:
        pk = (await s.execute(
            select(GroupMember.pubkey).where(GroupMember.node_id == from_uuid).limit(1)
        )).scalar_one_or_none()
        if pk:
            return pk
        ip = resolve_uuid_to_ip(from_uuid)
        row = (await s.execute(select(Peer).where(
            (Peer.ip == from_uuid) | (Peer.ip == ip) | (Peer.resolved_ip == ip)
        ).limit(1))).scalar_one_or_none()
        if row is not None and (row.peer_group_pubkey or ""):
            return row.peer_group_pubkey
        known_peer = row is not None or (ip and ip != from_uuid)
    if known_peer:
        return await _fetch_and_store_peer_group_pubkey(from_uuid)
    return ""


async def apply_inbound_dm(body: dict) -> dict:
    """Store an inbound direct message (dedupe on ``msg_id``).

    Shared by the HTTP route and the relay-routed dispatch. The sender's
    node UUID rides in ``from_uuid`` — that's the conversation key on this
    (receiving) side. a sealed ``enc`` envelope is opened
    with our local key; a plaintext ``body`` is the legacy/old-peer path.

    Security F-007: the sender signs the message; we verify that signature
    against the group pubkey bound to ``from_uuid`` (co-member or paired peer),
    so a forged DM claiming a contact's gossiped UUID is rejected.
    """
    import base64

    from nexus.runtime import event_bus
    from nexus.utils.time import iso_now

    msg_id = str(body.get("msg_id") or "").strip()
    from_uuid = str(body.get("from_uuid") or "").strip()
    enc = body.get("enc")
    attach_data = str(body.get("attach_data") or "")
    if enc:
        try:
            from nexus.security.group_ecies import ecies_open
            from nexus.security.group_keys import get_local_group_privkey

            priv = get_local_group_privkey()
            text = ecies_open(base64.b64decode(enc), priv).decode("utf-8")
            if body.get("enc_attach"):
                attach_data = ecies_open(
                    base64.b64decode(body["enc_attach"]), priv
                ).decode("utf-8")
        except Exception:
            return {"ok": False, "reason": "decrypt failed"}
    else:
        text = str(body.get("body") or "")
    sent_at = str(body.get("sent_at") or "")
    if not msg_id or not from_uuid or (not text and not attach_data):
        return {"ok": False, "reason": "missing msg_id/from_uuid/content"}
    # Authorize by the CRYPTO identity, not the gossiped UUID (F-007). Resolve the
    # group pubkey bound to from_uuid (co-member or paired peer); a sender we have
    # no binding for is an unknown stranger.
    from nexus.security.usage_receipt import (
        STMT_DM,
        dm_statement_payload,
        verify_statement,
    )
    sender_pub = await _bound_sender_group_pubkey(from_uuid)
    if not sender_pub:
        return {"ok": False, "reason": "sender not a co-member or peer"}
    # The sender must prove it holds that key by signing this message.
    sig = str(body.get("sig") or "")
    if not verify_statement(
        STMT_DM, dm_statement_payload(msg_id, from_uuid, sent_at, text), sig, sender_pub
    ):
        return {"ok": False, "reason": "unverified sender"}
    async with get_session() as session:
        if await session.get(DirectMessage, msg_id) is not None:
            return {"ok": True, "deduped": True}
        session.add(DirectMessage(
            msg_id=msg_id,
            peer_uuid=from_uuid,
            direction="in",
            sender_name=str(body.get("from_name") or ""),
            body=text,
            sent_at=str(body.get("sent_at") or iso_now()),
            received_at=iso_now(),
            reply_to=str(body.get("reply_to") or ""),
            reply_snippet=str(body.get("reply_snippet") or ""),
            reply_sender=str(body.get("reply_sender") or ""),
            attach_kind=str(body.get("attach_kind") or ("inline" if attach_data else "")),
            attach_name=str(body.get("attach_name") or ""),
            attach_mime=str(body.get("attach_mime") or ""),
            attach_size=int(body.get("attach_size") or 0),
            attach_data=attach_data,
        ))
        await session.commit()
    await event_bus.publish({"type": "peer.message", "peer_uuid": from_uuid})
    # A >5MB DM attachment isn't inline — pull it from the sender.
    if str(body.get("attach_kind") or "") == "foreign":
        import asyncio
        asyncio.create_task(_pull_foreign_dm(msg_id, from_uuid))
    return {"ok": True}


async def _pull_foreign_dm(msg_id: str, sender_uuid: str) -> None:
    """Fetch a sender-hosted DM attachment, unseal it (ECIES to our key), and
    cache it locally so the download/preview endpoints can serve it."""
    from nexus.networking.peer_http import peer_http_post
    from nexus.runtime.chat_attachments import has_blob, store_blob
    from nexus.security.group_ecies import ecies_open
    from nexus.security.group_keys import get_local_group_privkey

    if has_blob(msg_id):
        return
    from nexus.api.local import _resolve_dm_target
    addr = await _resolve_dm_target(sender_uuid)
    if not addr or addr == sender_uuid:
        return
    try:
        res = await peer_http_post(
            addr, "/peer/attachment_pull",
            {"msg_id": msg_id, "from_uuid": get_or_create_node_uuid()},
            timeout=120.0,
        )
    except Exception:
        _log.debug("foreign DM pull failed for %s", msg_id, exc_info=True)
        return
    if int(res.get("status") or 0) != 200:
        return
    sealed_b64 = (res.get("body") or {}).get("sealed_b64") or ""
    if not sealed_b64:
        return
    try:
        raw = ecies_open(base64.b64decode(sealed_b64), get_local_group_privkey())
    except Exception:
        _log.debug("foreign DM unseal failed for %s", msg_id, exc_info=True)
        return
    store_blob(msg_id, raw)
    from nexus.runtime import event_bus
    await event_bus.publish({"type": "peer.message", "peer_uuid": sender_uuid})


@router.post(
    "/attachment_pull",
    summary="Serve a sender-hosted (>5MB) DM attachment, sealed to the requester.",
)
async def peer_attachment_pull(request: Request) -> dict:
    """Return a foreign DM attachment's bytes sealed (ECIES) to the requester's
    DM encryption key. ``from_uuid`` is the requester — the recipient of the
    original outgoing DM, which is how we find the hosted row and their key."""
    body = await request.json()
    msg_id = str(body.get("msg_id") or "").strip()
    from_uuid = str(body.get("from_uuid") or "").strip()
    if not msg_id or not from_uuid:
        raise HTTPException(status_code=400, detail="missing msg_id/from_uuid")
    async with get_session() as session:
        m = await session.get(DirectMessage, msg_id)
    if m is None or m.direction != "out" or m.peer_uuid != from_uuid:
        raise HTTPException(status_code=404, detail="attachment not hosted here")
    from nexus.runtime.chat_attachments import load_blob
    raw = load_blob(msg_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="attachment not hosted here")
    from nexus.api.local import _get_or_fetch_peer_enc_pub, _resolve_dm_target
    addr = await _resolve_dm_target(from_uuid)
    enc_pub = await _get_or_fetch_peer_enc_pub(addr) if addr else ""
    if not enc_pub:
        raise HTTPException(status_code=409, detail="no recipient key")
    from nexus.security.group_ecies import ecies_seal
    sealed = ecies_seal(raw, enc_pub)
    return {"sealed_b64": base64.b64encode(sealed).decode("ascii")}


@router.post("/usage_receipt", summary="Receive a counterparty-signed usage receipt")
async def peer_usage_receipt(request: Request) -> dict:
    """Store a usage receipt pushed by the consumer. The inner consumer
    signature is verified in ``store_and_apply`` — an unsigned/forged receipt
    is dropped, so this endpoint needs no extra trust gate."""
    body = await request.json()
    from nexus.runtime.usage_receipts import store_and_apply
    applied = await store_and_apply(body.get("receipt") or {}, str(body.get("sig") or ""))
    return {"ok": True, "applied": applied}


@router.get("/usage_receipts", summary="List usage receipts between the caller and us")
async def peer_usage_receipts(other: str = "") -> dict:
    """Return verified receipts exchanged between *other* (the caller's group
    pubkey) and this node, so a friend can audit who-helped-whom. Receipts are
    signed facts, so serving them leaks nothing forgeable."""
    from nexus.core.identity import get_node_identity  # noqa: F401
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage.models import UsageReceipt

    me = get_local_group_pubkey()
    other = (other or "").strip()
    if not other:
        return {"receipts": []}
    pair = {me, other}
    async with get_session() as session:
        rows = (
            await session.execute(
                select(UsageReceipt).where(
                    UsageReceipt.provider_pubkey.in_(pair)
                    & UsageReceipt.consumer_pubkey.in_(pair)
                )
            )
        ).scalars().all()
    out = [
        {
            "provider_pubkey": r.provider_pubkey,
            "consumer_pubkey": r.consumer_pubkey,
            "kind": r.kind, "amount": int(r.amount or 0), "ts": r.ts,
        }
        for r in rows if r.provider_pubkey in pair and r.consumer_pubkey in pair
    ]
    return {"receipts": out}


@router.post("/profile", summary="Serve this node's public profile")
async def peer_profile(request: Request) -> dict:
    """Return this node's advertised profile — about-me, hosted
    services (display-only labels), and receipt-derived global pool usage.
    The usage numbers are counterparty-signed, so they can't be inflated.
    Visible to connected peers / group co-members (who is the only one able to
    reach this endpoint); the data is advertisement, nothing private."""
    from nexus.core.config import LOCAL_SETTINGS, public_services
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.runtime.usage_receipts import global_usage_summary

    return {
        "display_name": str(LOCAL_SETTINGS.get("user_display_name", "") or ""),
        "about_me": str(LOCAL_SETTINGS.get("about_me", "") or ""),
        "hosted_services": public_services(LOCAL_SETTINGS.get("hosted_services")),
        "pubkey": get_local_group_pubkey(),
        "node_uuid": str(LOCAL_SETTINGS.get("node_uuid", "") or ""),
        "global_usage": await global_usage_summary(),
    }


@router.post("/service_request", summary="Receive a service-access request")
async def peer_service_request(request: Request) -> dict:
    """A connected peer asks to use one of our advertised services.
    The request is signed by the consumer and only accepted from a node we're
    connected to; free services auto-approve, permission ones queue for the
    host, paid are refused."""
    from nexus.runtime.service_grants import handle_service_request
    return await handle_service_request(await request.json())


@router.post("/service_grant_update", summary="Receive a service-grant status update")
async def peer_service_grant_update(request: Request) -> dict:
    """The provider tells us a grant we hold changed
    (approved/denied/revoked). Verified against the grant's provider key."""
    from nexus.runtime.service_grants import apply_grant_update
    return await apply_grant_update(await request.json())


@router.post("/service_db_credentials", summary="Serve DBaaS credentials to an approved consumer")
async def peer_service_db_credentials(request: Request) -> dict:
    """A consumer holding an approved DB-kind service grant fetches its
    per-consumer connection. Verified by the consumer's signed statement; the
    host provisions the database+login lazily on first fetch."""
    from nexus.runtime.service_grants import handle_db_credentials
    return await handle_db_credentials(await request.json())


@router.post("/dm", summary="Receive a 1:1 direct message")
async def peer_dm(request: Request) -> dict:
    """Inbound DM (direct-HTTP path). No trusted-peer gate — group
    co-members aren't paired; ``apply_inbound_dm`` authorizes by
    shared-group membership (or trusted-peer), and the body is E2E sealed.
    """
    body = await request.json()
    return await apply_inbound_dm(body)


__all__ = ["router"]
