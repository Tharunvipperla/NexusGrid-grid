"""``/local/relay/*`` : control the in-process local relay.

Lets a founder run the generic Nexus relay inside this node so groups
can be bound to ``ws://<this-node>:<port>`` without deploying a separate
relay server. Node-level (not group-scoped): one relay per node.
"""

from __future__ import annotations

import socket

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import (
    local_relay,
    relay_latency,
    relay_pause,
    relay_selfheal,
    relay_tunnel,
)
from nexus.security import verify_local_auth
from nexus.storage import save_local_settings_to_db

# Follow-up — restrict to unprivileged ports (>=1024 so we don't
# need root on Linux; <=65535 by definition). 1024–65535 is also the
# range we surface in the UI hint.
MIN_RELAY_PORT = 1024
MAX_RELAY_PORT = 65535

router = APIRouter(
    prefix="/local/relay",
    tags=["Local Relay"],
    dependencies=[Depends(verify_local_auth)],
)


class StartRelayBody(BaseModel):
    port: int = Field(
        default=local_relay.DEFAULT_RELAY_PORT,
        ge=MIN_RELAY_PORT,
        le=MAX_RELAY_PORT,
    )
    # Which relay implementation to run — "default" (bundled) or a
    # host-trusted nexus_relays/<module>.py plugin.
    module: str = "default"


@router.get("/status", summary="Local relay status")
async def relay_status() -> dict:
    return local_relay.status()


@router.get("/modules", summary="Relay implementations this node can run")
async def relay_modules() -> dict:
    """The bundled ``default`` relay plus any host-trusted
    ``nexus_relays/*.py`` plugins, each with its code fingerprint."""
    return {"modules": local_relay.available_relay_modules()}


class InstanceBody(BaseModel):
    port: int = Field(..., ge=MIN_RELAY_PORT, le=MAX_RELAY_PORT)
    # Each extra instance runs a distinct relay module.
    module: str = "default"


@router.get("/modules/{name}/source", summary="Export a relay module's source")
async def relay_module_source(name: str) -> dict:
    """The source of a relay module so it can be shared/inspected."""
    src = local_relay.get_module_source(name)
    if not src:
        raise HTTPException(status_code=404, detail="no such relay module")
    return src


class ImportModuleBody(BaseModel):
    name: str
    source: str


@router.post("/modules", summary="Import relay code as a host-trusted plugin")
async def relay_module_import(body: ImportModuleBody) -> dict:
    """Save shared relay code as a nexus_relays/<name>.py plugin so it
    can be run (start) and bound. Saving does NOT run it; running foreign relay
    code is the operator's explicit, separate action."""
    try:
        return local_relay.import_module_source(body.name, body.source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/modules/{name}", summary="Delete an imported relay plugin")
async def relay_module_delete(name: str) -> dict:
    res = local_relay.delete_module(name)
    if not res.get("ok"):
        code = 404 if res.get("error") == "not_found" else 409
        raise HTTPException(status_code=code, detail=res.get("error") or "cannot delete")
    return res


@router.get("/instances", summary="Extra relay instances running on this node")
async def relay_instances() -> dict:
    return {"instances": local_relay.list_instances()}


@router.post("/instances", summary="Start an extra relay instance")
async def relay_instance_start(body: InstanceBody) -> dict:
    """Run an additional relay (own + a group's) on its own port.
    Each running relay must be a distinct module."""
    grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    try:
        st = local_relay.start_instance(body.port, grid_key, body.module)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    insts = list(LOCAL_SETTINGS.get("local_relay_instances") or [])
    if not any(int(i.get("port", 0)) == body.port for i in insts):
        insts.append({"port": body.port, "module": body.module})
        LOCAL_SETTINGS["local_relay_instances"] = insts
        await save_local_settings_to_db()
    return st


@router.delete("/instances/{port}", summary="Stop an extra relay instance")
async def relay_instance_stop(port: int) -> dict:
    res = local_relay.stop_instance(port)
    insts = [i for i in (LOCAL_SETTINGS.get("local_relay_instances") or [])
             if int(i.get("port", 0)) != int(port)]
    LOCAL_SETTINGS["local_relay_instances"] = insts
    await save_local_settings_to_db()
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "not found")
    return res


# ---- sandboxed (out-of-process) relay execution ---------------


@router.get("/sandbox/runners", summary="Sandbox runners available for foreign relay code")
async def relay_sandbox_runners() -> dict:
    """The same runner registry (docker/podman/raw + plugins); a
    foreign/imported relay runs as a separate sandboxed process rather than
    in-process."""
    from nexus.runtime.replica_runner import available_runners
    return {"runners": available_runners()}


class SandboxRelayBody(BaseModel):
    module: str
    port: int = Field(..., ge=MIN_RELAY_PORT, le=MAX_RELAY_PORT)
    runner: str = "raw"
    image: str = ""
    allow_outbound: bool = False
    agreed: bool = False


@router.post("/sandbox", summary="Run a relay module as a sandboxed process")
async def relay_sandbox_start(body: SandboxRelayBody) -> dict:
    """Run foreign/imported relay code out-of-process in a chosen sandbox. The
    UI must show the consent panel and pass ``agreed=True``."""
    from nexus.runtime import relay_sandbox
    grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    res = await relay_sandbox.run_sandboxed_relay(
        body.module, body.port, body.runner, grid_key, body.agreed,
        body.image, body.allow_outbound,
    )
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "cannot run")
    return res


@router.get("/sandbox", summary="Sandboxed relays running on this node")
async def relay_sandbox_list() -> dict:
    from nexus.runtime import relay_sandbox
    return relay_sandbox.list_sandboxed_relays()


@router.delete("/sandbox/{sandbox_id}", summary="Stop a sandboxed relay")
async def relay_sandbox_stop(sandbox_id: str) -> dict:
    from nexus.runtime import relay_sandbox
    res = await relay_sandbox.stop_sandboxed_relay(sandbox_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error") or "not found")
    return res


@router.get("/precheck", summary="Check if a port can be used for the local relay")
async def relay_precheck(
    port: int = Query(default=local_relay.DEFAULT_RELAY_PORT),
) -> dict:
    """Tell the UI ahead of time whether ``port`` will accept ``bind``.

    Returns ``{"available": bool, "reason": str, "min": int, "max": int}``.
    A 200 OK either way — the ``available`` flag is the signal; the UI
    surfaces ``reason`` to the user. ``min``/``max`` are echoed so the
    UI's range hint stays in sync with the server's enforcement.
    """
    payload = {"min": MIN_RELAY_PORT, "max": MAX_RELAY_PORT}
    if port < MIN_RELAY_PORT or port > MAX_RELAY_PORT:
        return {
            **payload,
            "available": False,
            "reason": f"port {port} out of range ({MIN_RELAY_PORT}–{MAX_RELAY_PORT})",
        }
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        return {**payload, "available": False, "reason": str(exc)}
    finally:
        probe.close()
    return {**payload, "available": True, "reason": ""}


@router.post("/start", summary="Start the in-process relay")
async def relay_start(body: StartRelayBody) -> dict:
    """Start the relay and remember the intent so it auto-starts on the
    next node boot."""
    grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    try:
        st = local_relay.start(body.port, grid_key, body.module)
    except ValueError as exc:
        # Unknown relay plugin name.
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        # Port already in use — most common cause is a second Nexus node
        # on the same machine. Surface a clear 409 instead of the UI
        # falsely reporting "started".
        raise HTTPException(status_code=409, detail=str(exc))
    LOCAL_SETTINGS["local_relay_enabled"] = True
    LOCAL_SETTINGS["local_relay_port"] = body.port
    LOCAL_SETTINGS["local_relay_module"] = body.module
    await save_local_settings_to_db()
    return st


@router.post("/stop", summary="Stop the in-process relay")
async def relay_stop() -> dict:
    st = local_relay.stop()
    LOCAL_SETTINGS["local_relay_enabled"] = False
    await save_local_settings_to_db()
    return st


# ---- public auto-tunnel ---------------------------------------


@router.get("/tunnel/status", summary="Auto-tunnel status")
async def tunnel_status() -> dict:
    return {**relay_tunnel.status(), "self_heal": relay_selfheal.status()}


@router.post("/tunnel/start", summary="Expose the local relay via a tunnel")
async def tunnel_start() -> dict:
    """Open a Cloudflare quick tunnel to the local relay.

    Starts the in-process relay first if needed, then self-heals every
    group bound to a stale tunnel URL onto the fresh one. The first call
    downloads ``cloudflared`` and can take ~30 s.
    """
    try:
        return await relay_selfheal.start_tunnel_and_reconcile()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"tunnel failed: {exc}")


@router.post("/tunnel/stop", summary="Close the auto-tunnel")
async def tunnel_stop() -> dict:
    st = relay_tunnel.stop()
    LOCAL_SETTINGS["relay_tunnel_enabled"] = False
    await save_local_settings_to_db()
    return st


# ---- latency snapshot ---------------------------------------


@router.get("/latency", summary="Current relay-latency cache snapshot")
async def relay_latency_snapshot() -> dict:
    """Return ``{url: {rtt_ms, last_probed_at}}`` for every probed relay.

    Source of truth for UI latency columns and (/C) the pool's
    lowest-RTT selector. ``rtt_ms`` is ``null`` when the last probe
    failed.
    """
    return {"relays": relay_latency.snapshot()}


# ---- pause / resume with delayed cloudflared kill -----------


@router.get("/pause/status", summary="Pause-state snapshot")
async def relay_pause_status() -> dict:
    return relay_pause.status()


@router.post("/pause", summary="Pause the local relay")
async def relay_pause_endpoint() -> dict:
    """Stop the local relay; keep cloudflared alive for the grace window.

    Resume within the window → instant restart with the same tunnel URL.
    Resume after the window → fresh tunnel URL acquired (cached binary,
    no re-download); self-heal broadcasts the new URL.
    """
    return await relay_pause.pause()


@router.post("/resume", summary="Resume the local relay")
async def relay_resume_endpoint() -> dict:
    return await relay_pause.resume()


# ---- set pasted relay URL as the node's primary --------------


class SetRelayUrlBody(BaseModel):
    relay_url: str = Field(..., min_length=4)


@router.post("/set_url", summary="Set a pasted relay URL as this node's primary")
async def set_relay_url(body: SetRelayUrlBody) -> dict:
    """Bind a user-pasted relay URL as this node's primary subscription.

    gives the Mint-Invite / Pair-Invite "Paste relay URL" UI a
    backend without going through the full settings POST. Updates
    ``LOCAL_SETTINGS['relay_server_url']`` and persists; the pool
    orchestrator picks up the change on its next tick.
    """
    url = (body.relay_url or "").strip()
    if not (url.startswith("ws://") or url.startswith("wss://")):
        raise HTTPException(
            status_code=400,
            detail="relay URL must start with ws:// or wss://",
        )
    LOCAL_SETTINGS["relay_server_url"] = url
    LOCAL_SETTINGS["relay_enabled"] = True
    await save_local_settings_to_db()
    return {"relay_server_url": url, "relay_enabled": True}


# ---- follow-up: per-URL bound-group counts ---------------------


@router.get(
    "/bindings_summary",
    summary="Per-relay-URL bound-group counts (active bindings only)",
)
async def relay_bindings_summary() -> dict:
    """Return ``{by_url: {<relay_url>: <count>}}`` for the Diagnostics
    Relays panel. Counts only ``status='active'`` GroupRelayBinding rows.
    """
    from sqlalchemy import func, select
    from nexus.storage import get_session
    from nexus.storage.models import GroupRelayBinding

    by_url: dict[str, int] = {}
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    GroupRelayBinding.relay_url,
                    func.count(GroupRelayBinding.group_id),
                )
                .where(GroupRelayBinding.status == "active")
                .group_by(GroupRelayBinding.relay_url)
            )
        ).fetchall()
    for url, count in rows:
        if url:
            by_url[url] = int(count or 0)
    return {"by_url": by_url}


# ---- first-launch onboarding -------------------------------


@router.get(
    "/onboarding/status",
    summary="Decide whether the UI should show the relay-configuration prompt",
)
async def relay_onboarding_status() -> dict:
    """Return relay state plus a single fact: is this node currently
    unconfigured?

    follow-up: the persistent ``relay_onboarding_dismissed``
    flag used to suppress the modal forever after one Skip click —
    that turned a transient "not now" into a permanent "never again,"
    which left users stuck with no obvious way to re-open it. We drop
    that gating: ``should_prompt`` is now derived purely from current
    relay state. The client maintains its own per-session "skip this
    boot" hint via ``sessionStorage`` so closing the modal doesn't
    spam it for that tab.
    """
    primary_url = str(LOCAL_SETTINGS.get("relay_server_url", "") or "")
    status = local_relay.status()
    running = bool(status.get("running"))
    has_relay = bool(primary_url) or running
    return {
        "should_prompt": not has_relay,
        "primary_url": primary_url,
        "local_running": running,
        "local_status": status,
    }


@router.post(
    "/onboarding/dismiss",
    summary="(Legacy) acknowledged-the-prompt no-op",
)
async def relay_onboarding_dismiss() -> dict:
    """Kept as a 200 no-op so older clients calling this on Start /
    Paste / Skip don't 404. The flag is no longer load-bearing — see
    :func:`relay_onboarding_status` for the rationale."""
    return {"dismissed": True}


# ---- telemetry export + purge --------------------------------


class TelemetryPurgeBody(BaseModel):
    before: str = Field(
        ...,
        description="ISO8601 timestamp; every bucket whose start is "
        "strictly before this is deleted.",
    )


class TelemetryRetentionBody(BaseModel):
    days: int = Field(..., ge=0, le=3650)


@router.get(
    "/telemetry/retention",
    summary="Read the current telemetry retention setting (days)",
)
async def relay_telemetry_retention_get() -> dict:
    from nexus.runtime.relay_telemetry_rollup import DEFAULT_RETENTION_DAYS
    raw = LOCAL_SETTINGS.get(
        "relay_telemetry_retention_days", DEFAULT_RETENTION_DAYS
    )
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    return {"days": max(0, days)}


@router.post(
    "/telemetry/retention",
    summary="Set how many days of telemetry to keep (0 = unlimited)",
)
async def relay_telemetry_retention_set(body: TelemetryRetentionBody) -> dict:
    LOCAL_SETTINGS["relay_telemetry_retention_days"] = int(body.days)
    await save_local_settings_to_db()
    return {"days": int(body.days)}


@router.get(
    "/telemetry/export",
    summary="Stream relay telemetry buckets in the requested range",
)
async def relay_telemetry_export(
    since: str = Query("", description="ISO8601 (inclusive lower bound); empty = no lower bound"),
    until: str = Query("", description="ISO8601 (exclusive upper bound); empty = no upper bound"),
    format: str = Query("json", pattern="^(json|csv)$"),
) -> Response:
    """Stream every persisted bucket whose ``bucket_start`` falls in
    ``[since, until)`` as either JSON or CSV. Generator-based so a
    multi-year archive doesn't materialize in RAM.
    """
    from sqlalchemy import select
    from nexus.storage import get_session
    from nexus.storage.models import RelayTelemetryBucket

    async def _stream():
        if format == "csv":
            yield (
                b"relay_url,bucket_kind,bucket_start,"
                b"frame_count,state_changes,last_state\n"
            )
        else:
            yield b'{"buckets":[\n'
        first = True
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(RelayTelemetryBucket).order_by(
                        RelayTelemetryBucket.bucket_start
                    )
                )
            ).scalars().all()
        for r in rows:
            if since and r.bucket_start < since:
                continue
            if until and r.bucket_start >= until:
                continue
            if format == "csv":
                line = (
                    f"{r.relay_url},{r.bucket_kind},{r.bucket_start},"
                    f"{int(r.frame_count or 0)},"
                    f"{int(r.state_changes or 0)},"
                    f"{r.last_state or ''}\n"
                )
                yield line.encode("utf-8")
            else:
                import json as _json
                blob = _json.dumps({
                    "relay_url": r.relay_url,
                    "bucket_kind": r.bucket_kind,
                    "bucket_start": r.bucket_start,
                    "frame_count": int(r.frame_count or 0),
                    "state_changes": int(r.state_changes or 0),
                    "last_state": r.last_state or "",
                })
                prefix = b"" if first else b",\n"
                first = False
                yield prefix + blob.encode("utf-8")
        if format == "json":
            yield b"\n]}"

    media = "text/csv" if format == "csv" else "application/json"
    return StreamingResponse(_stream(), media_type=media)


@router.delete(
    "/telemetry",
    summary="Manually purge telemetry buckets older than ``before``",
)
async def relay_telemetry_purge(body: TelemetryPurgeBody) -> dict:
    """Drop every bucket whose ``bucket_start`` < ``before`` (ISO8601).

    Mirrors the daily rollup sweeper's pruning step but lets the user
    free disk space immediately without waiting for retention.
    """
    from sqlalchemy import select, delete as sa_delete
    from nexus.storage import get_session
    from nexus.storage.models import RelayTelemetryBucket

    cutoff = (body.before or "").strip()
    if not cutoff:
        raise HTTPException(
            status_code=400,
            detail="'before' is required",
        )
    pruned = 0
    async with get_session() as session:
        rows = (
            await session.execute(select(RelayTelemetryBucket))
        ).scalars().all()
        for r in rows:
            if r.bucket_start < cutoff:
                await session.execute(
                    sa_delete(RelayTelemetryBucket).where(
                        (RelayTelemetryBucket.relay_url == r.relay_url)
                        & (RelayTelemetryBucket.bucket_kind == r.bucket_kind)
                        & (RelayTelemetryBucket.bucket_start == r.bucket_start)
                    )
                )
                pruned += 1
        await session.commit()
    return {"pruned": pruned, "before": cutoff}


__all__ = ["router"]
