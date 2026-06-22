"""FastAPI application factory and lifespan wiring.

Ported to parity with Phase-1/node_modified.py ``lifespan`` (lines 6008-6136)
and the CORS block (lines 6139-6161). Startup flow:

1. Record node port (needed by identity, cache_dir, audit actors).
2. Init SQLite + run idempotent schema migrations.
3. Load settings + seed identity mappings from persisted peer rows.
4. Install identity ⇄ storage persistence hook.
5. Install grid-key provider + CLI overrides.
6. Install runtime ⇄ networking hook.
7. Seed any ``--peers`` arg into the DB as trusted masters.
8. Re-enqueue queued tasks that survived a restart.
9. Start every background loop:
   - ``start_discovery`` (UDP 34567 listener),
   - ``gossip_broadcaster_loop``,
   - scheduler loops (``dag_scheduler_loop``, ``retry_scheduler_loop``),
   - ``zombie_sweeper``, ``observability_loop``,
   - ``master_manager_loop`` (worker-client supervisor),
   - ``relay_client_loop``.
10. Mount CORS middleware.

On shutdown: send a signed ``bye`` to every inbound + outbound peer WS,
a plain ``bye`` to the relay, then cancel every background task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from nexus.core import DEFAULT_HTTP_PORT

_log = logging.getLogger("nexus.app")


def create_app(args=None):
    """Return a configured FastAPI instance.

    *args* is the parsed ``argparse`` namespace from :mod:`nexus.cli`. When
    missing (e.g. tests), defaults from :mod:`nexus.core.constants` are used.
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    port = getattr(args, "port", DEFAULT_HTTP_PORT) if args else DEFAULT_HTTP_PORT
    cli_peers = str(getattr(args, "peers", "") or "")
    cli_relay_url = str(getattr(args, "relay", "") or "")
    cli_grid_key = str(getattr(args, "grid_key", "") or "")

    @asynccontextmanager
    async def lifespan(app):
        from sqlalchemy import select

        from nexus.core import LOCAL_SETTINGS
        from nexus.core.identity import (
            register_peer_uuid,
            resolve_uuid_to_ip,
            set_node_port,
            set_persist_hook,
        )
        from nexus.networking import (
            get_connected_master_peers,
            gossip_broadcaster_loop,
            set_grid_key_provider,
            start_discovery,
        )
        from nexus.networking.relay_client import (
            relay_client_loop,
            relay_pool_orchestrator,
            set_relay_cli_overrides,
        )
        from nexus.networking.worker_client import master_manager_loop
        from nexus.runtime import set_connected_masters_hook
        from nexus.scheduler import start_scheduler_loops
        from nexus.security.crypto import sign_bye
        from nexus.security.tokens import get_local_api_token
        from nexus.storage import (
            Peer,
            TaskRecord,
            get_session,
            init_db,
            load_local_settings_from_db,
            persist_resolved_ip,
            save_local_settings_to_db,
            seed_identity_mappings,
        )
        from nexus.tasks import enqueue_task
        from nexus.telemetry import observability_loop, zombie_sweeper

        # --- 1. Node port ------------------------------------------------
        set_node_port(port)

        # --- 1b. E5: apply a staged restore before the DB is opened ------
        from nexus.runtime.backup import apply_pending_restore
        _restore = apply_pending_restore(port)
        if _restore:
            import logging
            logging.getLogger("nexus.app").warning("[E5] restore: %s", _restore)

        # --- 2. Storage + settings --------------------------------------
        await init_db(port)
        await load_local_settings_from_db()
        await seed_identity_mappings()

        # Force LOCAL_API_TOKEN creation if not yet on disk
        get_local_api_token()

        # --- 3. Identity ⇄ storage hook ---------------------------------
        set_persist_hook(persist_resolved_ip)

        # --- 4. Node UUID ------------------------------------------------
        from nexus.core import NODE_UUID  # noqa: F401 — forces module load
        from nexus.core.identity import get_or_create_node_uuid

        node_uuid = get_or_create_node_uuid()
        if not LOCAL_SETTINGS.get("node_uuid"):
            LOCAL_SETTINGS["node_uuid"] = node_uuid
            await save_local_settings_to_db()
        _log.info("Node UUID: %s", node_uuid)

        # --- 5. Seed UUID↔IP mapping from persisted peers ---------------
        async with get_session() as db:
            all_peers = (await db.execute(select(Peer))).scalars().all()
            for p in all_peers:
                if str(p.ip).startswith("nexus_") and p.resolved_ip:
                    register_peer_uuid(p.ip, p.resolved_ip)

        # --- 6. Seed --peers CLI arg + re-enqueue surviving tasks -------
        async with get_session() as db:
            if cli_peers:
                for p in cli_peers.split(","):
                    p = p.strip()
                    if not p:
                        continue
                    ip = f"127.0.0.1:{p}" if ":" not in p else p
                    existing = (
                        await db.execute(select(Peer).filter(Peer.ip == ip))
                    ).scalar_one_or_none()
                    if not existing:
                        db.add(
                            Peer(
                                ip=ip,
                                status="trusted",
                                role="master",
                                their_auth_token=secrets.token_hex(16),
                                # Seed with a non-NULL placeholder so a missing
                                # X-Cluster-Key can't match this row via
                                # `IS NULL` semantics. Overwritten by the real
                                # token during the peer handshake.
                                my_auth_token=secrets.token_hex(16),
                            )
                        )
            for task in (
                (
                    await db.execute(
                        select(TaskRecord).filter(
                            TaskRecord.status.in_(["queued", "retrying"])
                        )
                    )
                )
                .scalars()
                .all()
            ):
                if task.status == "queued":
                    await enqueue_task(task.id)
            await db.commit()

        # --- 7. Grid key + relay CLI overrides --------------------------
        set_relay_cli_overrides(cli_relay_url, cli_grid_key)

        def _grid_key_getter() -> str:
            return (
                cli_grid_key
                or str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
            ).strip()

        set_grid_key_provider(_grid_key_getter)

        # --- 8. Runtime ⇄ networking hook -------------------------------
        set_connected_masters_hook(get_connected_master_peers)

        # --- 8b. First-run self-benchmark (non-blocking) -----------------
        if not float(LOCAL_SETTINGS.get("benchmark_score", 0.0) or 0.0):
            from nexus.scheduler.benchmark import run_benchmark

            async def _bench_once() -> None:
                try:
                    result = await asyncio.to_thread(run_benchmark)
                    LOCAL_SETTINGS["benchmark_score"] = float(result["score"])
                    LOCAL_SETTINGS["benchmark_at"] = str(result["ran_at"])
                    await save_local_settings_to_db()
                    _log.info(
                        "Self-benchmark: score=%.2f cpu=%.2f MFLOPS io=%.2f MB/s",
                        result["score"],
                        result["cpu_mflops"],
                        result["io_mb_s"],
                    )
                except Exception as exc:
                    _log.warning("Self-benchmark failed: %s", exc)

            asyncio.create_task(_bench_once(), name="nexus.scheduler.first_bench")

        # Install foreign-storage throttle so the receive_chunk
        # path can find it via STATE.foreign_storage_throttle.
        from nexus.networking.storage_throttle import install_storage_throttle
        install_storage_throttle()
        # Install workflow handler that routes non-chunk
        # storage_* frames (offer / response / eviction / retrieve / etc.).
        from nexus.runtime.foreign_storage_workflow import (
            install_workflow_handler,
        )
        install_workflow_handler()
        # D3: fan grid events out to user-configured outbound webhooks.
        from nexus.runtime.webhooks import install_webhook_dispatcher
        install_webhook_dispatcher()

        # --- 9. Background loops ----------------------------------------
        discovery_transport = await start_discovery()
        bg_tasks: list[asyncio.Task] = []
        bg_tasks.extend(await start_scheduler_loops())
        bg_tasks.append(
            asyncio.create_task(
                gossip_broadcaster_loop(port), name="nexus.networking.gossip"
            )
        )
        bg_tasks.append(
            asyncio.create_task(zombie_sweeper(), name="nexus.telemetry.zombie")
        )
        bg_tasks.append(
            asyncio.create_task(
                observability_loop(), name="nexus.telemetry.observability"
            )
        )
        bg_tasks.append(
            asyncio.create_task(
                master_manager_loop(), name="nexus.networking.master_manager"
            )
        )
        bg_tasks.append(
            asyncio.create_task(
                relay_client_loop(), name="nexus.networking.relay"
            )
        )
        # Secondary pool of WS connections to every group's
        # bound relays (besides the legacy primary). Lets us receive
        # Inbound from any relay a sender might use; will
        # also send outbound through the lowest-RTT one.
        bg_tasks.append(
            asyncio.create_task(
                relay_pool_orchestrator(),
                name="nexus.networking.relay_pool",
            )
        )

        # --- 9b. Local relay autostart ------------------------
        if LOCAL_SETTINGS.get("local_relay_enabled"):
            try:
                from nexus.runtime import local_relay

                local_relay.start(
                    int(
                        LOCAL_SETTINGS.get("local_relay_port")
                        or local_relay.DEFAULT_RELAY_PORT
                    ),
                    str(LOCAL_SETTINGS.get("relay_grid_key", "") or ""),
                    str(LOCAL_SETTINGS.get("local_relay_module", "") or "default"),
                )
            except Exception:
                _log.warning("local relay autostart failed", exc_info=True)

        # Restart any extra opt-in relay instances.
        for _inst in LOCAL_SETTINGS.get("local_relay_instances") or []:
            try:
                from nexus.runtime import local_relay

                local_relay.start_instance(
                    int(_inst.get("port")),
                    str(LOCAL_SETTINGS.get("relay_grid_key", "") or ""),
                    str(_inst.get("module") or "default"),
                )
            except Exception:
                _log.warning("extra relay instance autostart failed", exc_info=True)

        # --- 9c. Relay tunnel autostart + self-heal (/28) --------
        # Slow (cloudflared download + tunnel), so run it as a background
        # task; it re-binds groups onto the fresh tunnel URL.
        if LOCAL_SETTINGS.get("relay_tunnel_enabled"):
            try:
                from nexus.runtime import relay_selfheal

                bg_tasks.append(
                    asyncio.create_task(
                        relay_selfheal.maybe_autostart(),
                        name="nexus.runtime.relay_tunnel_autostart",
                    )
                )
            except Exception:
                _log.warning(
                    "relay tunnel autostart scheduling failed", exc_info=True
                )

        # --- 9d. Relay-latency probe loop -------------------
        # Sweeps every known relay every 60s + caches RTT for latency-
        # aware routing (W36.B/C) and the UI's latency column.
        try:
            from nexus.runtime import relay_latency

            bg_tasks.append(
                asyncio.create_task(
                    relay_latency.probe_loop(),
                    name="nexus.runtime.relay_latency.probe_loop",
                )
            )
        except Exception:
            _log.warning(
                "relay latency probe loop scheduling failed", exc_info=True
            )

        # --- 9e. Group frame-log prune ------------------------
        # Drops GroupFrameLog rows older than FRAME_LOG_RETENTION_HOURS
        # once an hour so the catch-up store stays bounded.
        async def _frame_log_prune_loop():
            from nexus.runtime.group_inbox import prune_frame_log

            while True:
                try:
                    await prune_frame_log()
                except Exception:
                    pass
                await asyncio.sleep(3600)

        try:
            bg_tasks.append(
                asyncio.create_task(
                    _frame_log_prune_loop(),
                    name="nexus.runtime.group_inbox.prune_loop",
                )
            )
        except Exception:
            _log.warning(
                "group frame-log prune loop scheduling failed", exc_info=True
            )

        try:
            yield
        finally:
            # --- Shutdown: signed bye to every peer --------------------
            from nexus.core import STATE

            bye_ts = time.time()
            peer_signing_keys: dict[str, str] = {}
            try:
                async with get_session() as db:
                    for p in (
                        (
                            await db.execute(
                                select(Peer).filter(Peer.status == "trusted")
                            )
                        )
                        .scalars()
                        .all()
                    ):
                        if p.signing_key:
                            peer_signing_keys[p.ip] = p.signing_key
                            rip = resolve_uuid_to_ip(p.ip)
                            if rip != p.ip:
                                peer_signing_keys[rip] = p.signing_key
            except Exception:
                pass

            def _signed_bye(peer_ip: str) -> dict:
                skey = peer_signing_keys.get(peer_ip, "")
                return {
                    "type": "bye",
                    "node_id": node_uuid,
                    "ts": bye_ts,
                    "sig": sign_bye(node_uuid, bye_ts, key=skey),
                }

            async with STATE.inbound_peer_ws_lock:
                inbound_snapshot = list(STATE.inbound_peer_ws.items())
            for ip, ws in inbound_snapshot:
                try:
                    await asyncio.wait_for(
                        ws.send_json(_signed_bye(ip)), timeout=1.0
                    )
                except Exception:
                    pass
            async with STATE.outbound_master_ws_lock:
                outbound_snapshot = list(STATE.outbound_master_ws.items())
            for ip, ws in outbound_snapshot:
                try:
                    await asyncio.wait_for(
                        ws.send_json(_signed_bye(ip)), timeout=1.0
                    )
                except Exception:
                    pass
            try:
                if STATE.relay_ws is not None:
                    await asyncio.wait_for(
                        STATE.relay_ws.send(json.dumps({"type": "bye"})),
                        timeout=1.0,
                    )
            except Exception:
                pass

            # Cancel every background task
            for task in bg_tasks:
                task.cancel()
            for task in bg_tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if discovery_transport is not None:
                try:
                    discovery_transport.close()
                except Exception:
                    pass

            # /27: stop the in-process relay + auto-tunnel.
            try:
                from nexus.runtime import local_relay, relay_tunnel

                relay_tunnel.stop()
                local_relay.stop()
            except Exception:
                pass

            set_persist_hook(None)
            set_grid_key_provider(None)
            set_connected_masters_hook(None)
            set_relay_cli_overrides("", "")

    from nexus import __version__ as _app_version

    app = FastAPI(title="NexusGrid", version=_app_version, lifespan=lifespan)

    # --- CORS ---------------------------------------------------------
    # Phase-1 parity: restrict origins to loopback + local-IP:port unless
    # ``NEXUS_CORS_ORIGINS`` explicitly overrides.
    from nexus.utils.net import get_local_ip

    cors_env = os.getenv("NEXUS_CORS_ORIGINS", "")
    if cors_env:
        cors_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        cors_origins = [
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://{get_local_ip()}:{port}",
        ]
    cors_credentials = True
    if "*" in cors_origins or cors_origins == ["*"]:
        cors_credentials = False
        _log.warning(
            "[SECURITY] CORS wildcard with credentials is insecure. "
            "Credentials disabled."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Security F-010: global request-body ceiling so an unauthenticated peer
    # can't OOM the node with a huge body before a handler rejects it. Cap is
    # the existing top upload bound; WS frames are bounded separately.
    from nexus.security.body_limit import BodySizeLimitMiddleware
    from nexus.security.limits import get_max_result_bytes
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=get_max_result_bytes)

    from nexus.api import register_routers
    from nexus.ui import mount_ui

    register_routers(app)
    mount_ui(app)

    return app
