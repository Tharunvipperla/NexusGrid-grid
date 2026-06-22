"""Long-lived Worker→Master WebSocket loop + relay fallback + supervisor.

Ported from Phase-1/node_modified.py:

* ``worker_client_process`` — lines 3791-4382 (direct WS mode)
* ``_relay_worker_loop`` — lines 4384-4818 (relay-tunnel fallback)
* ``master_manager_loop`` — lines 4821-4850 (per-master supervisor)

One :func:`worker_client_process` coroutine runs per trusted master. It:

1. Opens ``ws://<master>/peer/ws`` with the negotiated token.
2. Streams heartbeats every ~2s with live CPU/RAM/GPU stats.
3. Polls ``/peer/pop_task`` and dispatches each task through the runtime
   executor, uploading signed results back via ``/peer/submit_result``.
4. Honours consent-mode task offers via the worker-side offer queue.
5. Falls back to :func:`_relay_worker_loop` when direct HTTP+WS fails but
   the master is reachable on the relay.

``master_manager_loop`` is a supervisor — it reads the ``peers`` table
every 5s and spawns (or respawns) one worker coroutine per trusted master.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import random
import tempfile
import time
import zipfile

import httpx
import psutil
import websockets
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.core.identity import (
    fmt_peer,
    get_node_identity,
    resolve_ip_to_uuid,
    resolve_uuid_to_ip,
)
from nexus.networking import log_forwarder
from nexus.networking.relay_client import relay_http_request
from nexus.networking.websocket_client import open_worker_websocket
from nexus.runtime import (
    can_pull_task_from_master,
    clear_local_task,
    execute_bundle_with_watchdog,
    get_dispatch_capacity_mb,
    get_local_worker_snapshot,
    local_capabilities,
    mark_local_task_result,
    mark_local_task_running,
)
from nexus.security.crypto import sign_bytes, verify_bye, verify_signature
from nexus.storage import Peer, get_session
from nexus.tasks import upsert_remote_shadow_task
from nexus.telemetry import presence
from nexus.telemetry.audit import write_audit_event
from nexus.telemetry.hardware import sample_net_bandwidth
from nexus.telemetry.presence import is_peer_offline
from nexus.ui.broadcaster import broadcast_ui_update
from nexus.utils.text import mask_ips_in_log, safe_extractall
from nexus.utils.time import timestamp

_log = logging.getLogger("nexus.networking.worker_client")


async def _fetch_data_sources(
    workspace_dir: str,
    dynamic_env: dict,
    peer_signing_key: str,
    task_id: str,
    master_ip: str,
) -> None:
    """Pull cloud data_sources / workspace_source into the workspace.

    Reads ``task.json`` from ``workspace_dir`` to find any cloud sources,
    unwraps ``NEXUS_TASK_DATA_CREDS`` into a per-credential ``bytearray``,
    instantiates each provider via the cloud registry, and downloads the
    folder under the requested ``mount_path``. Audits per source. Always
    zeroizes credential bytes in ``finally``.

    Raises if any source fails — the caller MUST not run the bundle.
    """
    task_json_path = os.path.join(workspace_dir, "task.json")
    if not os.path.isfile(task_json_path):
        return
    try:
        with open(task_json_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return

    sources: list[dict] = []
    raw_data = manifest.get("data_sources") or []
    if isinstance(raw_data, list):
        sources.extend(s for s in raw_data if isinstance(s, dict))
    ws = manifest.get("workspace_source")
    if isinstance(ws, dict):
        sources.append({**ws, "mount_path": ""})
    if not sources:
        return

    creds_raw = dynamic_env.get("NEXUS_TASK_DATA_CREDS", "")
    if not creds_raw:
        raise RuntimeError(
            f"task {task_id}: data_sources present but no NEXUS_TASK_DATA_CREDS"
        )

    from pathlib import Path as _Path

    from nexus.networking.storage_throttle import get_storage_throttle
    from nexus.security.cred_crypto import unwrap_task_data_from_transit
    from nexus.storage.cloud import PROVIDERS

    payload = json.loads(creds_raw)
    nonce = base64.b64decode(payload.get("nonce", ""))
    wrapped = payload.get("credentials", {}) or {}

    cred_plaintexts: dict[str, bytearray] = {}
    try:
        for cid, b64_blob in wrapped.items():
            blob = base64.b64decode(b64_blob)
            cred_plaintexts[cid] = bytearray(
                unwrap_task_data_from_transit(peer_signing_key, nonce, blob)
            )

        throttle = get_storage_throttle()
        for s in sources:
            cid = str(s.get("credential_id") or "").strip()
            ptype = str(s.get("type") or "").strip()
            folder_id = str(s.get("folder_id") or "").strip()
            mount_path = str(s.get("mount_path") or "").strip().lstrip("/")

            if cid not in cred_plaintexts:
                raise RuntimeError(
                    f"task {task_id}: no transit credential for {cid!r}"
                )
            provider_cls = PROVIDERS.get(ptype)
            if provider_cls is None:
                raise RuntimeError(f"task {task_id}: unknown provider {ptype!r}")

            cred_bytes = bytes(cred_plaintexts[cid])
            try:
                provider = provider_cls.from_credential_json(cred_bytes)
                target = _Path(workspace_dir) / mount_path if mount_path else _Path(workspace_dir)
                file_count, byte_count = await provider.download_folder(
                    folder_id, target, throttle.acquire
                )
            except Exception as exc:
                await write_audit_event(
                    "task.data_source_fetch_failed",
                    actor=master_ip,
                    task_id=task_id,
                    details=f"provider={ptype} folder={folder_id} err={exc}",
                    severity="warning",
                )
                raise
            await write_audit_event(
                "task.data_source_fetched",
                actor=master_ip,
                task_id=task_id,
                details=f"provider={ptype} files={file_count} bytes={byte_count}",
            )
    finally:
        for buf in cred_plaintexts.values():
            for i in range(len(buf)):
                buf[i] = 0
        cred_plaintexts.clear()


def _idle_hb_fields() -> dict:
    """Return the small dict of idle-state fields to merge into a heartbeat."""
    from nexus.runtime.idle_detect import (
        is_idle,
        is_node_online_effective,
        seconds_since_input,
    )

    secs = seconds_since_input()
    return {
        "idle": is_idle(),
        "idle_seconds": float(secs) if secs is not None else None,
        "online_effective": is_node_online_effective(),
    }


# ---------------------------------------------------------------------------
# Per-master HTTP scheme cache — masters serve TLS by default but can be
# launched with --no-tls. Trust is via signing-key + cert fingerprint pinning,
# so verify=False matches the existing peer_http.py posture.
# ---------------------------------------------------------------------------

_master_schemes: dict[str, str] = {}


async def _peer_request(
    client: httpx.AsyncClient,
    method: str,
    master_ip: str,
    resolved_ip: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    """Issue an HTTP(S) request to a peer's ``/peer/*`` endpoint.

    Tries HTTPS first, falls back to HTTP, caches the working scheme per
    master so subsequent calls skip the probe.
    """
    cached = _master_schemes.get(master_ip)
    order = [cached, "https", "http"] if cached else ["https", "http"]
    seen: set[str] = set()
    last_err: BaseException | None = None
    for scheme in order:
        if not scheme or scheme in seen:
            continue
        seen.add(scheme)
        try:
            res = await client.request(
                method, f"{scheme}://{resolved_ip}{path}", **kwargs
            )
            _master_schemes[master_ip] = scheme
            return res
        except httpx.RequestError as exc:
            last_err = exc
            continue
    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Direct-WS worker loop
# ---------------------------------------------------------------------------

async def worker_client_process(
    master_ip: str,
    token: str,
    peer_signing_key: str = "",
) -> None:
    """Long-lived worker loop for one master. Exits only on auth failure."""
    if not token:
        return

    headers = {
        "X-Cluster-Key": str(token),
        "X-Node-Address": get_node_identity(),
    }

    backoff = 5
    max_backoff = 120
    idle_backoff = 600  # quiet poll interval once master looks dead
    consecutive_failures = 0
    quiet_threshold = 5
    quiet_notice_sent = False

    while True:
        # Batch C: if the operator blocked this master, stop connecting.
        if master_ip in set(LOCAL_SETTINGS.get("blocked_peer_uuids") or []):
            await asyncio.sleep(30)
            continue
        # Presence short-circuit: skip reconnecting to a peer we already know is offline.
        if is_peer_offline(master_ip):
            await asyncio.sleep(10)
            continue

        resolved_ip = resolve_uuid_to_ip(master_ip)
        if resolved_ip == master_ip and str(master_ip).startswith("nexus_"):
            if consecutive_failures < quiet_threshold:
                print(
                    f"[*] Worker waiting for peer discovery to resolve "
                    f"{fmt_peer(master_ip)}..."
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        ws_url = f"ws://{resolved_ip}/peer/ws"
        try:
            if consecutive_failures < quiet_threshold:
                print(f"[*] Worker attempting to connect to Master: {ws_url}...")
            async with open_worker_websocket(ws_url, headers) as ws:
                print(f"[+] Worker successfully connected to WebSocket: {ws_url}")
                backoff = 5
                consecutive_failures = 0
                quiet_notice_sent = False

                async def send_heartbeats() -> None:
                    while True:
                        try:
                            fr, dispatch_cap = get_dispatch_capacity_mb()
                            worker_state = await get_local_worker_snapshot()
                            caps = local_capabilities()
                            gpu_s = caps.get("gpu_stats", {}) if caps.get("gpu") else {}
                            hb_stats = {
                                "cpu": psutil.cpu_percent(interval=None),
                                "ram": psutil.virtual_memory().percent,
                                "free_ram": fr,
                                "dispatch_ram_cap_mb": dispatch_cap,
                                "status": worker_state["status"],
                                "active_task": worker_state["active_task"],
                                "serving_master": worker_state["serving_master"],
                                "active_tasks": worker_state["active_tasks"],
                                "serving_masters": worker_state["serving_masters"],
                                "connected_masters": worker_state["connected_masters"],
                                "connected_master_count": worker_state[
                                    "connected_master_count"
                                ],
                                "active_task_count": worker_state["active_task_count"],
                                "last_update": worker_state["last_update"],
                                "last_result_status": worker_state["last_result_status"],
                                "last_result_at": worker_state["last_result_at"],
                                "last_result_master": worker_state["last_result_master"],
                                "capabilities": caps,
                                "node_identity": headers["X-Node-Address"],
                                "user_display_name": str(
                                    LOCAL_SETTINGS.get("user_display_name", "") or ""
                                ),
                                "require_consent": bool(
                                    LOCAL_SETTINGS.get("require_worker_consent", False)
                                ),
                                "consent_timeout_sec": int(
                                    LOCAL_SETTINGS.get("consent_timeout_sec", 10)
                                ),
                                "net_io": sample_net_bandwidth(),
                                "process_ram_mb": round(
                                    psutil.Process().memory_info().rss / (1024 * 1024),
                                    1,
                                ),
                                "bench": float(
                                    LOCAL_SETTINGS.get("benchmark_score", 0.0) or 0.0
                                ),
                                "bench_at": str(
                                    LOCAL_SETTINGS.get("benchmark_at", "") or ""
                                ),
                                **_idle_hb_fields(),
                            }
                            if gpu_s:
                                hb_stats["gpu_util"] = gpu_s.get("gpu_util", 0)
                                hb_stats["gpu_mem_used_mb"] = gpu_s.get(
                                    "gpu_mem_used_mb", 0
                                )
                                hb_stats["gpu_mem_free_mb"] = gpu_s.get(
                                    "gpu_mem_free_mb", 0
                                )
                                hb_stats["gpu_mem_total_mb"] = gpu_s.get(
                                    "gpu_mem_total_mb", 0
                                )
                                hb_stats["gpu_name"] = gpu_s.get("gpu_name", "")
                                hb_stats["dispatch_gpu_cap_mb"] = gpu_s.get(
                                    "dispatch_gpu_cap_mb", 0
                                )
                            await ws.send(
                                json.dumps({"type": "heartbeat", "stats": hb_stats})
                            )
                            await asyncio.sleep(2)
                        except Exception as hb_err:
                            print(
                                f"[!] Heartbeat disconnected from "
                                f"{fmt_peer(master_ip)}: {hb_err}"
                            )
                            break

                hb_task = asyncio.create_task(send_heartbeats())

                # Inbound dispatcher: a single recv() loop that routes WS
                # frames from this master. Bye still ends the loop; tunnel
                # frames flow into nexus.networking.tunnel handlers.
                disconnect_event = asyncio.Event()

                async def recv_dispatch() -> None:
                    from nexus.networking.tunnel import (
                        handle_worker_tunnel_close,
                        handle_worker_tunnel_data,
                        handle_worker_tunnel_open,
                    )

                    while True:
                        try:
                            raw_msg = await ws.recv()
                        except Exception as exc:
                            _log.debug("ws recv from %s ended: %s", master_ip, exc)
                            disconnect_event.set()
                            return
                        try:
                            msg = json.loads(raw_msg)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        m_type = msg.get("type")
                        if m_type == "bye":
                            bye_node = msg.get("node_id", "")
                            bye_ts = msg.get("ts", 0)
                            bye_sig = msg.get("sig", "")
                            if verify_bye(
                                bye_node,
                                bye_ts,
                                bye_sig,
                                key=peer_signing_key,
                            ):
                                presence.mark_peer_offline(master_ip, source="ws")
                                print(
                                    f"[*] {fmt_peer(master_ip)} sent bye — "
                                    "disconnecting cleanly."
                                )
                                disconnect_event.set()
                                return
                            else:
                                _log.warning(
                                    "Ignoring bye with invalid signature from %s",
                                    master_ip,
                                )
                        elif m_type == "tunnel_open":
                            await handle_worker_tunnel_open(master_ip, msg)
                        elif m_type == "tunnel_data":
                            await handle_worker_tunnel_data(msg)
                        elif m_type == "tunnel_close":
                            await handle_worker_tunnel_close(msg)
                        elif m_type in ("svc_open", "svc_data", "svc_close"):
                            from nexus.runtime.service_tunnel import (
                                dispatch_service_frame,
                            )

                            await dispatch_service_frame(master_ip, msg)
                        elif m_type == "tunnel_udp_send":
                            from nexus.networking.tunnel import (
                                handle_worker_tunnel_udp_send,
                            )

                            await handle_worker_tunnel_udp_send(master_ip, msg)
                        elif m_type == "tunnel_udp_recv":
                            from nexus.networking.tunnel import (
                                handle_master_tunnel_udp_recv,
                            )

                            await handle_master_tunnel_udp_recv(msg)
                        elif isinstance(m_type, str) and m_type.startswith(
                            "storage_"
                        ):
                            from nexus.networking.storage_pump import (
                                dispatch_storage_frame,
                            )

                            await dispatch_storage_frame(master_ip, msg)
                        elif m_type == "service_stop":
                            from nexus.runtime.service_runner import stop_service

                            tid = str(msg.get("task_id", ""))
                            reason = str(msg.get("reason", "manual"))
                            if tid:
                                await stop_service(tid, reason=reason)
                        elif m_type == "service_prepare_standby":
                            from nexus.runtime.service_replication import (
                                prepare_standby,
                            )

                            tid = str(msg.get("task_id", ""))
                            mf = msg.get("manifest") or {}
                            if tid and isinstance(mf, dict):
                                asyncio.create_task(
                                    prepare_standby(tid, mf, master_ip)
                                )
                        elif m_type == "service_promote_with_snapshot":
                            from nexus.runtime.service_replication import (
                                promote_standby,
                            )

                            tid = str(msg.get("task_id", ""))
                            if tid:
                                asyncio.create_task(promote_standby(tid))
                        elif m_type == "service_image_refresh":
                            from nexus.runtime.service_replication import (
                                refresh_standby_image,
                            )

                            tid = str(msg.get("task_id", ""))
                            image = str(msg.get("image", ""))
                            if tid and image:
                                asyncio.create_task(
                                    refresh_standby_image(tid, image)
                                )
                        elif m_type == "service_dep_grant":
                            tid = str(msg.get("task_id", ""))
                            peers = msg.get("peers") or []
                            if tid and isinstance(peers, list):
                                grants = STATE.service_dep_grants.setdefault(
                                    tid, set()
                                )
                                for p in peers:
                                    if isinstance(p, str) and p:
                                        grants.add(p)
                        elif m_type == "service_dep_changed":
                            from nexus.networking.tunnel import (
                                ensure_dependency_tunnel,
                            )

                            tid = str(msg.get("task_id", ""))
                            primary = str(msg.get("primary", ""))
                            try:
                                port = int(msg.get("port") or 0)
                            except (TypeError, ValueError):
                                port = 0
                            if tid and primary and port:
                                asyncio.create_task(
                                    ensure_dependency_tunnel(tid, primary, port)
                                )

                inbound_task = asyncio.create_task(recv_dispatch())

                async with STATE.outbound_master_ws_lock:
                    STATE.outbound_master_ws[master_ip] = ws
                presence.mark_peer_online(master_ip, source="ws")

                async with httpx.AsyncClient(
                    headers=headers, verify=False
                ) as http_client:
                    while True:
                        try:
                            if (
                                LOCAL_SETTINGS["mode"] == "user"
                                and (
                                    psutil.virtual_memory().available // (1024 * 1024)
                                )
                                < 600
                            ):
                                await asyncio.sleep(5)
                                continue

                            if not await can_pull_task_from_master(master_ip):
                                await asyncio.sleep(2)
                                continue

                            async with STATE.worker_pull_lock:
                                if not await can_pull_task_from_master(master_ip):
                                    await asyncio.sleep(2)
                                    continue
                                res = await _peer_request(
                                    http_client,
                                    "GET",
                                    master_ip,
                                    resolved_ip,
                                    "/peer/pop_task",
                                    timeout=3.0,
                                )

                            if (
                                res.status_code == 202
                                and res.headers.get("X-Dispatch-Mode") == "consent"
                            ):
                                offer_data = res.json()
                                offer_task_id = offer_data.get("task_id", "unknown")
                                offer_timeout = offer_data.get(
                                    "consent_timeout_sec", 10
                                )
                                print(
                                    f"[CONSENT] Received task offer {offer_task_id} "
                                    f"from {fmt_peer(master_ip)} — awaiting your approval in the UI..."
                                )

                                decision_event = asyncio.Event()
                                offer_record = {
                                    "master_ip": master_ip,
                                    "offer_data": offer_data,
                                    "received_at": time.time(),
                                    "timeout": offer_timeout,
                                    "decision": None,
                                    "decision_event": decision_event,
                                }
                                async with STATE.worker_pending_offers_lock:
                                    STATE.worker_pending_offers[offer_task_id] = (
                                        offer_record
                                    )

                                await broadcast_ui_update(
                                    {
                                        "type": "task_offer",
                                        "task_id": offer_task_id,
                                        "master_ip": master_ip,
                                        "runtime": offer_data.get("runtime", "docker"),
                                        "image": offer_data.get("image", ""),
                                        "entrypoint": offer_data.get("entrypoint", ""),
                                        "ram_limit_mb": offer_data.get(
                                            "ram_limit_mb", 512
                                        ),
                                        "cpu_limit_pct": offer_data.get(
                                            "cpu_limit_pct", 100
                                        ),
                                        "require_gpu": offer_data.get(
                                            "require_gpu", False
                                        ),
                                        "timeout": offer_timeout,
                                    }
                                )

                                try:
                                    await asyncio.wait_for(
                                        decision_event.wait(), timeout=offer_timeout
                                    )
                                except asyncio.TimeoutError:
                                    offer_record["decision"] = "decline"

                                decision = offer_record["decision"] or "decline"
                                async with STATE.worker_pending_offers_lock:
                                    STATE.worker_pending_offers.pop(offer_task_id, None)

                                if decision == "accept":
                                    print(
                                        f"[CONSENT] You accepted task "
                                        f"{offer_task_id} from {fmt_peer(master_ip)}"
                                    )
                                    accept_res = await _peer_request(
                                        http_client,
                                        "POST",
                                        master_ip,
                                        resolved_ip,
                                        f"/peer/accept_offer/{offer_task_id}",
                                        timeout=5.0,
                                    )
                                    if accept_res.status_code == 200:
                                        res = accept_res
                                    else:
                                        print(
                                            f"[!] Accept failed for {offer_task_id}: "
                                            f"{accept_res.status_code}"
                                        )
                                        await broadcast_ui_update(
                                            {
                                                "type": "task_offer_resolved",
                                                "task_id": offer_task_id,
                                                "result": "accept_failed",
                                            }
                                        )
                                        await asyncio.sleep(1)
                                        continue
                                else:
                                    reason = (
                                        "timed out"
                                        if decision == "decline"
                                        and not decision_event.is_set()
                                        else "declined by user"
                                    )
                                    print(f"[CONSENT] Task {offer_task_id} {reason}")
                                    try:
                                        await _peer_request(
                                            http_client,
                                            "POST",
                                            master_ip,
                                            resolved_ip,
                                            f"/peer/decline_offer/{offer_task_id}",
                                            timeout=3.0,
                                        )
                                    except Exception:
                                        pass
                                    await broadcast_ui_update(
                                        {
                                            "type": "task_offer_resolved",
                                            "task_id": offer_task_id,
                                            "result": reason,
                                        }
                                    )
                                    await asyncio.sleep(2)
                                    continue

                            if res.status_code == 200:
                                print(f"[*] Task Pulled from {fmt_peer(master_ip)}! Executing...")
                                t_id = res.headers.get("X-Task-ID", "unknown")
                                task_env_str = res.headers.get("X-Task-Env", "{}")
                                task_sig = res.headers.get("X-Task-Sig", "")
                                try:
                                    dynamic_env = json.loads(task_env_str)
                                except Exception:
                                    dynamic_env = {}

                                try:
                                    with zipfile.ZipFile(
                                        io.BytesIO(res.content)
                                    ) as combined_z:
                                        inner_payload_bytes = combined_z.read(
                                            "payload.zip"
                                        )
                                except Exception as e:
                                    print(
                                        f"[!] Malformed zip payload from "
                                        f"{fmt_peer(master_ip)}: {e}"
                                    )
                                    continue

                                if not verify_signature(
                                    task_sig,
                                    "task_bundle",
                                    t_id,
                                    inner_payload_bytes,
                                    key=peer_signing_key,
                                ):
                                    print(
                                        f"[!] Security signature mismatch on task "
                                        f"{t_id}; rejecting payload."
                                    )
                                    await upsert_remote_shadow_task(
                                        master_ip,
                                        t_id,
                                        "failed",
                                        f"[{timestamp()}] [SECURITY] Task "
                                        "signature invalid. Payload rejected.\n",
                                        get_node_identity(),
                                    )
                                    await asyncio.sleep(1)
                                    continue

                                await mark_local_task_running(master_ip, t_id)
                                worker_id = get_node_identity()
                                _run_start = time.time # pool compute-secs
                                start_log = mask_ips_in_log(
                                    f"[{timestamp()}] [NETWORK] Accepted remote "
                                    f"task from {fmt_peer(master_ip)}.\n"
                                    f"[{timestamp()}] [WORKER] Executing locally "
                                    f"as {worker_id}.\n"
                                )
                                await upsert_remote_shadow_task(
                                    master_ip,
                                    t_id,
                                    "processing",
                                    start_log,
                                    worker_id,
                                )
                                try:
                                    with tempfile.TemporaryDirectory() as tmp:
                                        with zipfile.ZipFile(
                                            io.BytesIO(inner_payload_bytes)
                                        ) as pz:
                                            safe_extractall(pz, tmp)
                                        with zipfile.ZipFile(
                                            io.BytesIO(res.content)
                                        ) as combined_z:
                                            if (
                                                "checkpoint.zip"
                                                in combined_z.namelist()
                                            ):
                                                with zipfile.ZipFile(
                                                    io.BytesIO(
                                                        combined_z.read(
                                                            "checkpoint.zip"
                                                        )
                                                    )
                                                ) as cz:
                                                    safe_extractall(cz, tmp)

                                        await _fetch_data_sources(
                                            tmp,
                                            dynamic_env,
                                            peer_signing_key,
                                            t_id,
                                            master_ip,
                                        )

                                        log_forwarder.register_target(
                                            t_id, master_ip, token
                                        )
                                        try:
                                            status_meta, z_path = (
                                                await execute_bundle_with_watchdog(
                                                    tmp, t_id, dynamic_env, master_ip
                                                )
                                            )
                                        finally:
                                            log_forwarder.unregister_target(t_id)

                                        with open(z_path, "rb") as f:
                                            result_bytes = f.read()
                                        result_sig = sign_bytes(
                                            "result",
                                            t_id,
                                            result_bytes,
                                            status_meta["status"],
                                            key=peer_signing_key,
                                        )

                                        # Prove our group pubkey so a
                                        # 1:1 peer task can credit us (signed
                                        # once, reused across retries so it
                                        # matches the elapsed we send).
                                        from nexus.security.group_keys import (
                                            get_local_group_privkey,
                                            get_local_group_pubkey,
                                        )
                                        from nexus.security.usage_receipt import (
                                            sign_worker_proof,
                                        )
                                        _elapsed = int(time.time() - _run_start)
                                        _wpub = get_local_group_pubkey()
                                        _wproof = sign_worker_proof(
                                            t_id, _wpub, _elapsed,
                                            get_local_group_privkey(),
                                        )

                                        orphan_policy = dynamic_env.get(
                                            "NEXUS_ORPHAN_POLICY", "retry"
                                        )
                                        max_upload_attempts = (
                                            12 if orphan_policy == "retry" else 1
                                        )
                                        upload_success = False

                                        for attempt in range(max_upload_attempts):
                                            try:
                                                upload_res = await _peer_request(
                                                    http_client,
                                                    "POST",
                                                    master_ip,
                                                    resolved_ip,
                                                    f"/peer/submit_result/{t_id}",
                                                    data={
                                                        "status": status_meta["status"],
                                                        "logs": status_meta["output"],
                                                        "result_sig": result_sig,
                                                        "elapsed_secs": str(_elapsed),
                                                        "worker_pubkey": _wpub,
                                                        "worker_proof": _wproof,
                                                    },
                                                    files={
                                                        "file": (
                                                            "result.zip",
                                                            result_bytes,
                                                            "application/zip",
                                                        )
                                                    },
                                                    timeout=15.0,
                                                )
                                                if upload_res.status_code == 200:
                                                    upload_success = True
                                                    break
                                            except Exception:
                                                if attempt < max_upload_attempts - 1:
                                                    print(
                                                        f"[!] Master offline. "
                                                        f"Retrying upload for {t_id} "
                                                        f"in 5s... "
                                                        f"({attempt + 1}/"
                                                        f"{max_upload_attempts})"
                                                    )
                                                    await asyncio.sleep(5)
                                                else:
                                                    print(
                                                        f"[!] Master permanently "
                                                        f"offline. Dropping result "
                                                        f"for {t_id}."
                                                    )

                                        os.remove(z_path)

                                        if upload_success:
                                            execution_result_status = (
                                                "success"
                                                if status_meta["status"] == "success"
                                                else (
                                                    "preempted"
                                                    if status_meta["status"]
                                                    == "preempted"
                                                    else "failed"
                                                )
                                            )
                                            await mark_local_task_result(
                                                master_ip, execution_result_status
                                            )
                                            final_status = (
                                                "completed"
                                                if status_meta["status"] == "success"
                                                else (
                                                    "preempted"
                                                    if status_meta["status"]
                                                    == "preempted"
                                                    else "failed"
                                                )
                                            )
                                            final_logs = mask_ips_in_log(
                                                f"{start_log}{status_meta['output']}"
                                                f"\n[{timestamp()}] [NETWORK] "
                                                f"Remote result returned to "
                                                f"{fmt_peer(master_ip)} with status "
                                                f"{status_meta['status'].upper()}.\n"
                                            )
                                            await upsert_remote_shadow_task(
                                                master_ip,
                                                t_id,
                                                final_status,
                                                final_logs,
                                                worker_id,
                                            )
                                        else:
                                            await mark_local_task_result(
                                                master_ip, "failed"
                                            )
                                            await upsert_remote_shadow_task(
                                                master_ip,
                                                t_id,
                                                "failed",
                                                f"{start_log}{status_meta['output']}"
                                                f"\n[{timestamp()}] [NETWORK] Master "
                                                f"{fmt_peer(master_ip)} disconnected. Result "
                                                "dropped based on orphan policy.\n",
                                                worker_id,
                                            )

                                finally:
                                    await clear_local_task(master_ip, t_id)
                            elif (
                                res.status_code == 204
                                and res.headers.get("X-Dispatch-Wait") == "capacity"
                            ):
                                await asyncio.sleep(3)
                            else:
                                # No work and no capacity wait — yield briefly.
                                # The recv_dispatch task handles bye/tunnel
                                # frames in parallel.
                                await asyncio.sleep(2)
                                if disconnect_event.is_set():
                                    break
                        except httpx.RequestError as req_err:
                            print(
                                f"[!] HTTP Network Error polling tasks from "
                                f"{fmt_peer(master_ip)}: {req_err}"
                            )
                            await asyncio.sleep(2)
        except Exception as e:
            err_str = repr(e)
            async with STATE.outbound_master_ws_lock:
                STATE.outbound_master_ws.pop(master_ip, None)
            if "403" in err_str or "Forbidden" in err_str:
                print(
                    f"[~] Worker auth pending for {fmt_peer(master_ip)} (token not "
                    "yet synced). Will reconnect when peer accepts."
                )
                return  # master_manager_loop will re-spawn on next tick
            else:
                consecutive_failures += 1
                if consecutive_failures <= quiet_threshold:
                    print(
                        f"[!] Worker failed to connect to {fmt_peer(master_ip)}: "
                        f"{err_str}. Retrying in {int(backoff)}s..."
                    )
                elif not quiet_notice_sent:
                    print(
                        f"[~] {fmt_peer(master_ip)} unreachable after "
                        f"{quiet_threshold} attempts. Marking offline — will "
                        "reconnect when the node comes back online."
                    )
                    quiet_notice_sent = True
                if consecutive_failures >= 2:
                    presence.mark_peer_offline(master_ip, source="timeout")
                    peer_uuid = resolve_ip_to_uuid(master_ip)
                    if peer_uuid and peer_uuid != master_ip:
                        presence.mark_peer_offline(peer_uuid, source="timeout")

            # Relay fallback: if master is reachable via relay, switch modes.
            master_uuid = resolve_ip_to_uuid(master_ip)
            master_on_relay = STATE.relay_connected and (
                master_uuid in STATE.relay_peers or master_uuid != master_ip
            )
            if (
                master_on_relay
                and "403" not in err_str
                and "Forbidden" not in err_str
            ):
                if not LOCAL_SETTINGS.get("accept_cross_region_tasks", True):
                    print(
                        f"[~] Master {fmt_peer(master_ip)} is on relay but "
                        "accept_cross_region_tasks is disabled. Skipping relay mode."
                    )
                else:
                    print(
                        f"[*] Master {fmt_peer(master_ip)} reachable via relay — switching "
                        "to relay worker mode..."
                    )
                    try:
                        await _relay_worker_loop(master_ip, token, peer_signing_key)
                    except Exception as relay_err:
                        print(
                            f"[!] Relay worker loop for {fmt_peer(master_ip)} ended: "
                            f"{relay_err}"
                        )

                    master_still = master_uuid in STATE.relay_peers
                    if not master_still:
                        print(
                            f"[~] Master {fmt_peer(master_ip)} offline. Waiting for relay "
                            "peer list update..."
                        )
                        while STATE.relay_connected:
                            STATE.relay_peer_changed.clear()
                            try:
                                await asyncio.wait_for(
                                    STATE.relay_peer_changed.wait(), timeout=30
                                )
                            except asyncio.TimeoutError:
                                pass
                            if master_uuid in STATE.relay_peers:
                                print(f"[+] Master {fmt_peer(master_ip)} is back on relay!")
                                break
                            master_host = (
                                master_ip.split(":")[0]
                                if ":" in master_ip
                                else master_ip
                            )
                            beacon_hit = False
                            for peer_id, peer_data in list(
                                STATE.discovered_peers.items()
                            ):
                                peer_ts = (
                                    peer_data[0]
                                    if isinstance(peer_data, tuple)
                                    else peer_data
                                )
                                real_ip = (
                                    peer_data[5]
                                    if isinstance(peer_data, tuple)
                                    and len(peer_data) > 5
                                    else ""
                                )
                                if (
                                    real_ip
                                    and master_host in real_ip
                                    and (time.time() - peer_ts) < 10
                                ):
                                    beacon_hit = True
                                    print(
                                        f"[+] Master {fmt_peer(master_ip)} detected on LAN!"
                                    )
                                    break
                            if beacon_hit:
                                break
                    backoff = 5
                    continue

        finally:
            # Fast-recovery path: a fresh beacon beats the backoff timer.
            master_host = (
                master_ip.split(":")[0] if ":" in master_ip else master_ip
            )
            beacon_hit = False
            for peer_id, peer_data in list(STATE.discovered_peers.items()):
                peer_ts = (
                    peer_data[0] if isinstance(peer_data, tuple) else peer_data
                )
                real_ip = (
                    peer_data[5]
                    if isinstance(peer_data, tuple) and len(peer_data) > 5
                    else ""
                )
                if (
                    real_ip
                    and master_host in real_ip
                    and (time.time() - peer_ts) < 10
                ):
                    beacon_hit = True
                    break
            if beacon_hit:
                backoff = 5
                consecutive_failures = 0
                quiet_notice_sent = False
                await asyncio.sleep(2)
            else:
                effective = (
                    idle_backoff
                    if consecutive_failures > quiet_threshold
                    else backoff
                )
                jitter = random.uniform(0, min(effective * 0.3, 10))
                await asyncio.sleep(effective + jitter)
                backoff = min(max_backoff, backoff * 2)


# ---------------------------------------------------------------------------
# Relay-tunnel worker loop
# ---------------------------------------------------------------------------

async def _relay_worker_loop(
    master_ip: str,
    token: str,
    peer_signing_key: str = "",
) -> None:
    """Worker loop that uses the relay server as the transport."""
    node_id = get_node_identity()
    master_relay_uuid = resolve_ip_to_uuid(master_ip)
    print(f"[RELAY-WORKER] Starting relay worker mode for {fmt_peer(master_ip)}")

    def _master_on_relay() -> bool:
        return master_relay_uuid in STATE.relay_peers

    async def send_relay_heartbeats() -> None:
        hb_backoff = 3
        hb_consecutive_failures = 0
        while True:
            try:
                if not STATE.relay_connected or not _master_on_relay():
                    break
                fr, dispatch_cap = get_dispatch_capacity_mb()
                worker_state = await get_local_worker_snapshot()
                caps = local_capabilities()
                gpu_s = caps.get("gpu_stats", {}) if caps.get("gpu") else {}
                hb_stats = {
                    "cpu": psutil.cpu_percent(interval=None),
                    "ram": psutil.virtual_memory().percent,
                    "free_ram": fr,
                    "dispatch_ram_cap_mb": dispatch_cap,
                    "status": worker_state["status"],
                    "active_task": worker_state["active_task"],
                    "serving_master": worker_state["serving_master"],
                    "active_tasks": worker_state["active_tasks"],
                    "serving_masters": worker_state["serving_masters"],
                    "connected_masters": worker_state["connected_masters"],
                    "connected_master_count": worker_state["connected_master_count"],
                    "active_task_count": worker_state["active_task_count"],
                    "last_update": worker_state["last_update"],
                    "last_result_status": worker_state["last_result_status"],
                    "last_result_at": worker_state["last_result_at"],
                    "last_result_master": worker_state["last_result_master"],
                    "capabilities": caps,
                    "node_identity": node_id,
                    "user_display_name": str(
                        LOCAL_SETTINGS.get("user_display_name", "") or ""
                    ),
                    "require_consent": bool(
                        LOCAL_SETTINGS.get("require_worker_consent", False)
                    ),
                    "consent_timeout_sec": int(
                        LOCAL_SETTINGS.get("consent_timeout_sec", 10)
                    ),
                    "net_io": sample_net_bandwidth(),
                    "bench": float(
                        LOCAL_SETTINGS.get("benchmark_score", 0.0) or 0.0
                    ),
                    "bench_at": str(
                        LOCAL_SETTINGS.get("benchmark_at", "") or ""
                    ),
                    **_idle_hb_fields(),
                }
                if gpu_s:
                    hb_stats["gpu_util"] = gpu_s.get("gpu_util", 0)
                    hb_stats["gpu_mem_used_mb"] = gpu_s.get("gpu_mem_used_mb", 0)
                    hb_stats["gpu_mem_free_mb"] = gpu_s.get("gpu_mem_free_mb", 0)
                    hb_stats["gpu_mem_total_mb"] = gpu_s.get("gpu_mem_total_mb", 0)
                    hb_stats["gpu_name"] = gpu_s.get("gpu_name", "")
                    hb_stats["dispatch_gpu_cap_mb"] = gpu_s.get(
                        "dispatch_gpu_cap_mb", 0
                    )
                result = await relay_http_request(
                    master_relay_uuid,
                    "POST",
                    "/peer/relay_heartbeat",
                    {"stats": hb_stats},
                    timeout=10.0,
                )
                if result.get("status") != 200:
                    hb_consecutive_failures += 1
                    hb_backoff = min(hb_backoff * 2, 30)
                    _log.warning(
                        "[RELAY-WORKER] Heartbeat to %s failed (%d/5): %s",
                        master_ip, hb_consecutive_failures, result,
                    )
                    if hb_consecutive_failures >= 5:
                        _log.warning(
                            "[RELAY-WORKER] Too many heartbeat failures to %s — "
                            "stopping heartbeats",
                            master_ip,
                        )
                        break
                else:
                    hb_consecutive_failures = 0
                    hb_backoff = 3
                await asyncio.sleep(hb_backoff)
            except Exception as e:
                hb_consecutive_failures += 1
                hb_backoff = min(hb_backoff * 2, 30)
                _log.warning(
                    "[RELAY-WORKER] Heartbeat error (%d/5): %s",
                    hb_consecutive_failures, e,
                )
                if hb_consecutive_failures >= 5:
                    break
                await asyncio.sleep(hb_backoff)

    hb_task = asyncio.create_task(send_relay_heartbeats())
    peer_miss_count = 0

    try:
        while STATE.relay_connected:
            try:
                if not LOCAL_SETTINGS.get("accept_cross_region_tasks", True):
                    print(
                        f"[RELAY-WORKER] accept_cross_region_tasks disabled — "
                        f"exiting relay mode for {fmt_peer(master_ip)}"
                    )
                    return

                if not _master_on_relay():
                    peer_miss_count += 1
                    if peer_miss_count >= 3:
                        print(
                            f"[RELAY-WORKER] Master {fmt_peer(master_ip)} no longer on relay "
                            "— exiting"
                        )
                        return
                    await asyncio.sleep(5)
                    continue
                else:
                    peer_miss_count = 0

                if hb_task.done():
                    print(
                        f"[RELAY-WORKER] Heartbeat task ended for {fmt_peer(master_ip)} — "
                        "exiting relay mode"
                    )
                    return

                from nexus.runtime.idle_detect import is_node_online_effective

                if not is_node_online_effective():
                    await asyncio.sleep(2)
                    continue

                if (
                    LOCAL_SETTINGS["mode"] == "user"
                    and (psutil.virtual_memory().available // (1024 * 1024)) < 600
                ):
                    await asyncio.sleep(5)
                    continue

                if not await can_pull_task_from_master(master_ip):
                    await asyncio.sleep(2)
                    continue

                result = await relay_http_request(
                    master_relay_uuid, "GET", "/peer/pop_task", timeout=10.0
                )
                res_status = result.get("status", 204)
                res_body = result.get("body", {})
                res_headers = result.get("headers", {})
                dispatch_mode = ""
                if isinstance(res_headers, dict):
                    dispatch_mode = str(
                        res_headers.get("X-Dispatch-Mode")
                        or res_headers.get("x-dispatch-mode")
                        or ""
                    ).lower()

                if res_status == 202 and (
                    dispatch_mode == "consent"
                    or (
                        isinstance(res_body, dict)
                        and "consent_timeout_sec" in res_body
                    )
                ):
                    offer_data = res_body if isinstance(res_body, dict) else {}
                    offer_task_id = offer_data.get("task_id", "unknown")
                    offer_timeout = int(
                        offer_data.get("consent_timeout_sec", 10) or 10
                    )
                    print(
                        f"[CONSENT] Relay offer {offer_task_id} from {fmt_peer(master_ip)} - "
                        "awaiting approval in UI."
                    )

                    decision_event = asyncio.Event()
                    offer_record = {
                        "master_ip": master_ip,
                        "offer_data": offer_data,
                        "received_at": time.time(),
                        "timeout": offer_timeout,
                        "decision": None,
                        "decision_event": decision_event,
                    }
                    async with STATE.worker_pending_offers_lock:
                        STATE.worker_pending_offers[offer_task_id] = offer_record

                    await broadcast_ui_update(
                        {
                            "type": "task_offer",
                            "task_id": offer_task_id,
                            "master_ip": master_ip,
                            "runtime": offer_data.get("runtime", "docker"),
                            "image": offer_data.get("image", ""),
                            "entrypoint": offer_data.get("entrypoint", ""),
                            "ram_limit_mb": offer_data.get("ram_limit_mb", 512),
                            "cpu_limit_pct": offer_data.get("cpu_limit_pct", 100),
                            "require_gpu": offer_data.get("require_gpu", False),
                            "timeout": offer_timeout,
                        }
                    )

                    try:
                        await asyncio.wait_for(
                            decision_event.wait(), timeout=offer_timeout
                        )
                    except asyncio.TimeoutError:
                        offer_record["decision"] = "decline"

                    decision = offer_record["decision"] or "decline"
                    async with STATE.worker_pending_offers_lock:
                        STATE.worker_pending_offers.pop(offer_task_id, None)

                    if decision == "accept":
                        print(
                            f"[CONSENT] You accepted relay task {offer_task_id} "
                            f"from {fmt_peer(master_ip)}"
                        )
                        accept_res = await relay_http_request(
                            master_relay_uuid,
                            "POST",
                            f"/peer/accept_offer/{offer_task_id}",
                            timeout=15.0,
                        )
                        if (
                            accept_res.get("status") == 200
                            and isinstance(accept_res.get("body"), dict)
                        ):
                            await broadcast_ui_update(
                                {
                                    "type": "task_offer_resolved",
                                    "task_id": offer_task_id,
                                    "result": "accepted",
                                }
                            )
                            res_status = 200
                            res_body = accept_res.get("body", {})
                        else:
                            print(
                                f"[!] Relay accept failed for {offer_task_id}: "
                                f"{accept_res}"
                            )
                            await broadcast_ui_update(
                                {
                                    "type": "task_offer_resolved",
                                    "task_id": offer_task_id,
                                    "result": "accept_failed",
                                }
                            )
                            await asyncio.sleep(1)
                            continue
                    else:
                        reason = (
                            "timed out"
                            if decision == "decline"
                            and not decision_event.is_set()
                            else "declined by user"
                        )
                        print(f"[CONSENT] Relay task {offer_task_id} {reason}")
                        try:
                            await relay_http_request(
                                master_relay_uuid,
                                "POST",
                                f"/peer/decline_offer/{offer_task_id}",
                                timeout=8.0,
                            )
                        except Exception:
                            pass
                        await broadcast_ui_update(
                            {
                                "type": "task_offer_resolved",
                                "task_id": offer_task_id,
                                "result": reason,
                            }
                        )
                        await asyncio.sleep(2)
                        continue

                if (
                    res_status == 200
                    and isinstance(res_body, dict)
                    and res_body.get("task_id")
                ):
                    t_id = res_body["task_id"]
                    print(
                        f"[RELAY-WORKER] Received task {t_id} from {fmt_peer(master_ip)} "
                        "via relay"
                    )
                    worker_id = node_id

                    try:
                        env_str = res_body.get("env", "{}")
                        zip_b64 = res_body.get("zip_b64", "")
                        task_sig = str(res_body.get("task_sig", "") or "")
                        zip_data = base64.b64decode(zip_b64) if zip_b64 else b""

                        if task_sig and not verify_signature(
                            task_sig,
                            "task_bundle",
                            t_id,
                            zip_data,
                            key=peer_signing_key,
                        ):
                            print(
                                f"[!] Security signature mismatch on relay task "
                                f"{t_id}; rejecting payload."
                            )
                            await upsert_remote_shadow_task(
                                master_ip,
                                t_id,
                                "failed",
                                f"[{timestamp()}] [SECURITY] Relay task signature "
                                "invalid. Payload rejected.\n",
                                worker_id,
                            )
                            await asyncio.sleep(1)
                            continue
                        if not task_sig:
                            _log.warning(
                                "[SECURITY] Relay task %s arrived without "
                                "signature; allowing for compatibility.",
                                t_id,
                            )

                        await mark_local_task_running(master_ip, t_id)
                        _run_start = time.time # pool compute-secs
                        start_log = mask_ips_in_log(
                            f"[{timestamp()}] [NETWORK] Accepted remote task from "
                            f"{fmt_peer(master_ip)} via relay.\n"
                            f"[{timestamp()}] [WORKER] Executing locally as "
                            f"{worker_id}.\n"
                        )
                        await upsert_remote_shadow_task(
                            master_ip, t_id, "processing", start_log, worker_id
                        )

                        try:
                            dynamic_env = (
                                json.loads(env_str)
                                if isinstance(env_str, str)
                                else env_str
                            )
                        except Exception:
                            dynamic_env = {}

                        with tempfile.TemporaryDirectory() as tmp:
                            if zip_data:
                                with zipfile.ZipFile(io.BytesIO(zip_data)) as pz:
                                    safe_extractall(pz, tmp)

                            await _fetch_data_sources(
                                tmp,
                                dynamic_env,
                                peer_signing_key,
                                t_id,
                                master_ip,
                            )

                            log_forwarder.register_target(t_id, master_ip, token)
                            try:
                                status_meta, z_path = (
                                    await execute_bundle_with_watchdog(
                                        tmp, t_id, dynamic_env, master_ip
                                    )
                                )
                            finally:
                                log_forwarder.unregister_target(t_id)

                            with open(z_path, "rb") as f:
                                result_bytes = f.read()
                            result_zip_b64 = base64.b64encode(result_bytes).decode()
                            result_sig = sign_bytes(
                                "result",
                                t_id,
                                result_bytes,
                                status_meta["status"],
                                key=peer_signing_key,
                            )
                            os.remove(z_path)

                        execution_result_status = (
                            "success"
                            if status_meta["status"] == "success"
                            else (
                                "preempted"
                                if status_meta["status"] == "preempted"
                                else "failed"
                            )
                        )
                        await mark_local_task_result(master_ip, execution_result_status)

                        final_status = (
                            "completed"
                            if status_meta["status"] == "success"
                            else (
                                "preempted"
                                if status_meta["status"] == "preempted"
                                else "failed"
                            )
                        )
                        final_logs = mask_ips_in_log(
                            f"{start_log}{status_meta['output']}"
                        )

                        from nexus.security.group_keys import (
                            get_local_group_privkey,
                            get_local_group_pubkey,
                        )
                        from nexus.security.usage_receipt import sign_worker_proof
                        _elapsed = int(time.time() - _run_start)
                        _wpub = get_local_group_pubkey()
                        _wproof = sign_worker_proof(
                            t_id, _wpub, _elapsed, get_local_group_privkey()
                        )
                        submit_result = await relay_http_request(
                            master_relay_uuid,
                            "POST",
                            f"/peer/submit_result/{t_id}",
                            {
                                "status": status_meta["status"],
                                "logs": final_logs,
                                "result_zip_b64": result_zip_b64,
                                "result_sig": result_sig,
                                "elapsed_secs": _elapsed,
                                "worker_pubkey": _wpub,
                                "worker_proof": _wproof,
                            },
                            timeout=30.0,
                        )

                        if submit_result.get("status") == 200:
                            print(
                                f"[RELAY-WORKER] Result for {t_id} submitted to "
                                f"{fmt_peer(master_ip)} via relay"
                            )
                            await upsert_remote_shadow_task(
                                master_ip,
                                t_id,
                                final_status,
                                f"{final_logs}\n[{timestamp()}] [NETWORK] "
                                f"Result returned to {fmt_peer(master_ip)} via relay.\n",
                                worker_id,
                            )
                        else:
                            print(
                                f"[!] Relay result submission failed for {t_id}: "
                                f"{submit_result}"
                            )
                            await mark_local_task_result(master_ip, "failed")
                            await upsert_remote_shadow_task(
                                master_ip,
                                t_id,
                                "failed",
                                f"{final_logs}\n[{timestamp()}] [NETWORK] Relay "
                                "result submission failed.\n",
                                worker_id,
                            )

                    finally:
                        await clear_local_task(master_ip, t_id)

                elif res_status == 204:
                    await asyncio.sleep(3)
                else:
                    await asyncio.sleep(2)

            except Exception as e:
                _log.warning("[RELAY-WORKER] Task poll error: %s", e)
                await asyncio.sleep(3)

            # Probe for direct WS reachability — exit relay mode to retry if so.
            try:
                async with open_worker_websocket(
                    f"ws://{master_ip}/peer/ws", headers
                ) as test_ws:
                    await test_ws.close()
                print(
                    f"[RELAY-WORKER] Direct WS to {fmt_peer(master_ip)} is now reachable — "
                    "exiting relay mode"
                )
                return
            except Exception:
                pass
    finally:
        hb_task.cancel()
        print(f"[RELAY-WORKER] Exiting relay worker mode for {fmt_peer(master_ip)}")


# ---------------------------------------------------------------------------
# Master supervisor
# ---------------------------------------------------------------------------

async def master_manager_loop() -> None:
    """Supervisor: spawn one ``worker_client_process`` task per trusted master.

    gated on :func:`is_peer_link_allowed` (only ``node_online``)
    instead of :func:`is_node_online_effective` (which also folds in the
    idle gate). Storage / view-grant / control frames need the WS open
    even while the user is actively typing; the per-iteration polling loop
    inside ``worker_client_process`` still respects the idle gate so we
    don't pull *compute* tasks while in use.
    """
    from nexus.runtime.idle_detect import is_peer_link_allowed

    active_loops: dict[str, asyncio.Task] = {}
    while True:
        if not is_peer_link_allowed():
            await asyncio.sleep(2)
            continue

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
        master_auths = {
            m.ip: (m.their_auth_token, m.signing_key or "")
            for m in masters
            if m.their_auth_token
        }

        for ip, (tok, skey) in master_auths.items():
            if ip in active_loops:
                continue
            if is_peer_offline(ip):
                continue
            active_loops[ip] = asyncio.create_task(
                worker_client_process(ip, tok, skey)
            )

        # Clean up crashed or exited loops so they resurrect on the next tick.
        for ip in list(active_loops.keys()):
            if ip not in master_auths or active_loops[ip].done():
                if not active_loops[ip].done():
                    active_loops[ip].cancel()
                del active_loops[ip]

        await asyncio.sleep(5)


async def start_worker_client(*_args, **_kwargs):
    """Back-compat stub kept for the shim — real driver is :func:`master_manager_loop`."""
    return None


__all__ = [
    "worker_client_process",
    "_relay_worker_loop",
    "master_manager_loop",
    "start_worker_client",
]
