"""Long-running service-task runtime.

A *service task* is a task whose manifest declares ``runtime: "service"``.
Instead of running to completion, the container stays up exposing TCP
ports until either ``duration_sec`` elapses, ``idle_timeout_sec`` of zero
tunnel activity passes, or it crashes.

This module owns:

* :func:`validate_service_manifest` — ensures the manifest's service-only
  fields are well-formed before we start a container.
* :func:`start_service` — pulls the image (if needed), runs the container
  detached with ``ports=...`` so Docker assigns host ports, reads the
  resolved port mapping, registers state, and spawns the watchdog.
* :func:`stop_service` — graceful shutdown path used by the watchdog and
  by ``/local/services/{id}/stop`` (added in 9c).
* :func:`service_watchdog` — async coroutine that supervises the service
  for duration / idle / health.

The TCP tunnel itself (Step 9b) lives in :mod:`nexus.networking.tunnel`.
This module is intentionally tunnel-agnostic so it remains shippable on
its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime.docker_client import (
    docker_gpu_opts,
    docker_security_opts,
    get_docker_client,
)
from nexus.runtime.worker_state import (
    register_running_container,
    unregister_running_container,
)
from nexus.utils.time import timestamp

_log = logging.getLogger("nexus.runtime.service_runner")


_DEFAULT_DURATION_SEC = 3600
_DEFAULT_IDLE_TIMEOUT_SEC = 600
_WATCHDOG_INTERVAL_SEC = 5
_HEALTH_PROBE_TIMEOUT = 1.0


class ServiceManifestError(ValueError):
    """The manifest's service-only fields failed validation."""


# ---------------------------------------------------------------------------
# Cloud task-data sources
# ---------------------------------------------------------------------------

# Provider types accepted in `data_sources` / `workspace_source` manifest
# Fields. Only ``gdrive`` has a real driver in; the other three reuse
# The ``CloudProvider`` registry stubs and will raise at fetch time
# until later waves implement them.
VALID_DATA_SOURCE_TYPES = {"gdrive", "s3", "r2", "b2"}

# Cap on per-task data-source fan-out so a malformed manifest can't fan out
# to thousands of provider calls.
_MAX_DATA_SOURCES = 8

# Folder-id sanity cap. Drive folder ids are typically <= 44 chars; S3 keys
# can be longer. 256 is generous and keeps the manifest small.
_MAX_FOLDER_ID_LEN = 256


def _validate_mount_path(raw: str) -> str:
    """Reject absolute paths, parent-traversal, and NUL bytes.

    Empty string is allowed (means "merge into workspace root"). Returns
    the normalised relative path with forward-slash separators.
    """
    p = str(raw or "").strip().replace("\\", "/")
    if not p:
        return ""
    if p.startswith("/"):
        raise ServiceManifestError(
            f"data_sources mount_path must be relative: {raw!r}"
        )
    if "\x00" in p:
        raise ServiceManifestError("data_sources mount_path must not contain NUL")
    if any(seg == ".." for seg in p.split("/")):
        raise ServiceManifestError(
            f"data_sources mount_path must not contain '..': {raw!r}"
        )
    return p


def _validate_one_source(entry: dict, *, label: str) -> dict:
    if not isinstance(entry, dict):
        raise ServiceManifestError(f"{label} entry must be a dict: {entry!r}")
    ptype = str(entry.get("type") or "").strip().lower()
    if ptype not in VALID_DATA_SOURCE_TYPES:
        raise ServiceManifestError(
            f"{label} type must be one of {sorted(VALID_DATA_SOURCE_TYPES)}: {ptype!r}"
        )
    cred_raw = entry.get("credential_id")
    cred_id = str(cred_raw or "").strip()
    if not cred_id:
        raise ServiceManifestError(f"{label} credential_id is required")
    if len(cred_id) > 128:
        raise ServiceManifestError(
            f"{label} credential_id exceeds 128 chars"
        )
    if not all(c.isalnum() or c in "-_" for c in cred_id):
        raise ServiceManifestError(
            f"{label} credential_id has invalid chars: {cred_raw!r}"
        )
    folder_id = str(entry.get("folder_id") or "").strip()
    if not folder_id:
        raise ServiceManifestError(f"{label} folder_id is required")
    if len(folder_id) > _MAX_FOLDER_ID_LEN:
        raise ServiceManifestError(
            f"{label} folder_id exceeds {_MAX_FOLDER_ID_LEN} chars"
        )
    return {"type": ptype, "credential_id": cred_id, "folder_id": folder_id}


def validate_data_sources(manifest: dict) -> dict:
    """Normalize ``data_sources`` and ``workspace_source`` from a manifest.

    Returns a dict with two keys:

    * ``data_sources`` — list of ``{type, credential_id, folder_id, mount_path}``
      auxiliary sources merged into the workspace AFTER the bundle zip
      extracts on the worker.
    * ``workspace_source`` — optional ``{type, credential_id, folder_id}``;
      when set, the worker treats this folder AS the workspace (no
      ``mount_path``). Composes with ``data_sources`` if both are set.

    Both fields are absent / empty by default — backward compatible with
    every 8 manifest.
    """
    raw = manifest.get("data_sources") or []
    if not isinstance(raw, list):
        raise ServiceManifestError("data_sources must be a list")
    if len(raw) > _MAX_DATA_SOURCES:
        raise ServiceManifestError(
            f"data_sources may have at most {_MAX_DATA_SOURCES} entries"
        )
    sources: list[dict] = []
    for entry in raw:
        validated = _validate_one_source(entry, label="data_sources")
        validated["mount_path"] = _validate_mount_path(entry.get("mount_path") or "")
        sources.append(validated)

    ws_raw = manifest.get("workspace_source")
    workspace_source: dict | None = None
    if ws_raw:
        workspace_source = _validate_one_source(ws_raw, label="workspace_source")

    return {"data_sources": sources, "workspace_source": workspace_source}


def _pick_service_profile(local_settings: dict) -> str:
    """Choose the docker security profile for ``runtime: service``.

    The default node-wide ``security_profile`` is ``maximum`` (read-only
    root, non-root user, tmpfs mounts). That breaks most stateful service
    images out of the box — postgres init scripts, redis AOF rewrites,
    mongo journal all need to write to root paths. introduced
    ``service_friendly`` (cap-drop + no-new-privileges, but root-writable)
    for exactly this case but never wired it as the default for service
    tasks, so the gap kept showing up.

    Resolution order:

    1. Explicit operator override via ``service_security_profile`` setting
       wins if set (any value, including ``"maximum"`` to opt out).
    2. If the global ``security_profile`` is ``"maximum"``, return
       ``"service_friendly"``.
    3. Otherwise honor the global setting (``relaxed`` / ``standard`` /
       explicit ``service_friendly``).

    Batch tasks (``runtime`` ≠ ``"service"``) are unaffected — they keep
    using ``LOCAL_SETTINGS["security_profile"]`` directly via the
    executor.
    """
    override = str(local_settings.get("service_security_profile") or "").strip()
    if override:
        return override
    global_profile = str(local_settings.get("security_profile", "maximum") or "maximum")
    if global_profile == "maximum":
        return "service_friendly"
    return global_profile


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def validate_service_manifest(manifest: dict) -> dict:
    """Return a normalized service manifest, raising on invalid input."""
    if manifest.get("runtime") != "service":
        raise ServiceManifestError("manifest.runtime must be 'service'")
    image = str(manifest.get("image", "") or "").strip()
    if not image:
        raise ServiceManifestError("manifest.image is required for service runtime")

    raw_ports = manifest.get("expose_ports") or []
    if not isinstance(raw_ports, list) or not raw_ports:
        raise ServiceManifestError(
            "manifest.expose_ports must be a non-empty list of TCP ports"
        )
    # Accept range entries `{from, to}` alongside int ports.
    # Range size is capped at 100 to prevent accidental docker port-bind storms.
    ports: list[int] = []
    for p in raw_ports:
        if isinstance(p, dict):
            try:
                lo = int(p.get("from"))
                hi = int(p.get("to"))
            except (TypeError, ValueError):
                raise ServiceManifestError(
                    f"expose_ports range entry needs int from/to: {p!r}"
                )
            if not (1 <= lo <= hi <= 65535):
                raise ServiceManifestError(
                    f"expose_ports range out of bounds: {p!r}"
                )
            if hi - lo + 1 > 100:
                raise ServiceManifestError(
                    f"expose_ports range exceeds 100 ports: {p!r}"
                )
            ports.extend(range(lo, hi + 1))
            continue
        try:
            port = int(p)
        except (TypeError, ValueError):
            raise ServiceManifestError(f"expose_ports entry not an int: {p!r}")
        if not (1 <= port <= 65535):
            raise ServiceManifestError(f"expose_ports entry out of range: {port}")
        ports.append(port)

    duration_raw = manifest.get("duration_sec", _DEFAULT_DURATION_SEC)
    if duration_raw is None:
        duration_raw = _DEFAULT_DURATION_SEC
    try:
        duration = int(duration_raw)
    except (TypeError, ValueError):
        raise ServiceManifestError(f"duration_sec not an int: {duration_raw!r}")
    if duration <= 0:
        raise ServiceManifestError("duration_sec must be positive")

    idle_raw = manifest.get("idle_timeout_sec", _DEFAULT_IDLE_TIMEOUT_SEC)
    if idle_raw is None:
        idle_raw = _DEFAULT_IDLE_TIMEOUT_SEC
    try:
        idle = int(idle_raw)
    except (TypeError, ValueError):
        raise ServiceManifestError(f"idle_timeout_sec not an int: {idle_raw!r}")
    if idle < 0:
        raise ServiceManifestError("idle_timeout_sec must be non-negative (0 disables)")

    service_kind = str(manifest.get("service_kind", "tcp") or "tcp").strip().lower()
    ram = int(manifest.get("ram_limit_mb", 512) or 512)
    cpu = int(manifest.get("cpu_limit_pct", 100) or 100)
    entrypoint = manifest.get("entrypoint", "") or ""

    # Replication.
    replicas_raw = manifest.get("replicas", 1)
    if replicas_raw is None:
        replicas_raw = 1
    try:
        replicas = int(replicas_raw)
    except (TypeError, ValueError):
        raise ServiceManifestError(f"replicas not an int: {replicas_raw!r}")
    if replicas < 1:
        raise ServiceManifestError("replicas must be >= 1")

    strategy = str(manifest.get("replica_strategy", "none") or "none").strip().lower()
    if strategy not in {"none", "snapshot", "native"}:
        raise ServiceManifestError(
            f"replica_strategy must be 'none' | 'snapshot' | 'native' (got {strategy!r})"
        )

    snap_interval_raw = manifest.get("snapshot_interval_sec", 60)
    try:
        snap_interval = int(snap_interval_raw or 60)
    except (TypeError, ValueError):
        raise ServiceManifestError(
            f"snapshot_interval_sec not an int: {snap_interval_raw!r}"
        )
    if snap_interval < 5:
        raise ServiceManifestError("snapshot_interval_sec must be >= 5")

    snap_paths_raw = manifest.get("snapshot_paths") or []
    if not isinstance(snap_paths_raw, list):
        raise ServiceManifestError("snapshot_paths must be a list of container paths")
    snap_paths = [str(p) for p in snap_paths_raw if str(p).strip()]
    if strategy == "snapshot" and not snap_paths:
        raise ServiceManifestError(
            "snapshot_paths required when replica_strategy='snapshot'"
        )

    primary_selection = str(
        manifest.get("primary_selection", "fit") or "fit"
    ).strip().lower()
    if primary_selection not in {"fit", "round_robin"}:
        raise ServiceManifestError(
            f"primary_selection must be 'fit' | 'round_robin' (got {primary_selection!r})"
        )

    # Inter-service composition.
    deps_raw = manifest.get("depends_on") or []
    if not isinstance(deps_raw, list):
        raise ServiceManifestError("depends_on must be a list of {service_id, alias} dicts")
    deps: list[dict] = []
    for entry in deps_raw:
        if not isinstance(entry, dict):
            raise ServiceManifestError(f"depends_on entry must be a dict: {entry!r}")
        sid = str(entry.get("service_id", "") or "").strip()
        if not sid:
            raise ServiceManifestError("depends_on entry missing service_id")
        alias = str(entry.get("alias", "") or sid).strip()
        if not alias.replace("_", "").isalnum():
            raise ServiceManifestError(
                f"depends_on alias must be alphanumeric (or underscore): {alias!r}"
            )
        deps.append({"service_id": sid, "alias": alias.upper()})

    # Per-tunnel rate limit (MB/s, 0 = unlimited).
    try:
        rate_limit_mb_s = int(manifest.get("rate_limit_mb_s", 0) or 0)
    except (TypeError, ValueError):
        raise ServiceManifestError(
            f"rate_limit_mb_s not an int: {manifest.get('rate_limit_mb_s')!r}"
        )
    if rate_limit_mb_s < 0:
        raise ServiceManifestError("rate_limit_mb_s must be >= 0")

    tls_terminate = bool(manifest.get("tls_terminate", False))
    session_replay = bool(manifest.get("session_replay", False))
    shared_tunnel = bool(manifest.get("shared_tunnel", False))
    protocol = str(manifest.get("protocol", "tcp") or "tcp").strip().lower()
    if protocol not in {"tcp", "udp"}:
        raise ServiceManifestError(
            f"protocol must be 'tcp' | 'udp' (got {protocol!r})"
        )

    # Container env vars from the manifest. NEXUS_*/_NEXUS* are
    # Reserved so dep-injection and other runtime-supplied
    # vars cannot be clobbered by user input.
    env_raw = manifest.get("environment") or {}
    if not isinstance(env_raw, dict):
        raise ServiceManifestError("environment must be a dict[str, str]")
    if len(env_raw) > 64:
        raise ServiceManifestError("environment may have at most 64 entries")
    environment: dict[str, str] = {}
    total_bytes = 0
    for k, v in env_raw.items():
        if not isinstance(k, str) or not k:
            raise ServiceManifestError(f"environment key must be a non-empty string: {k!r}")
        if k.startswith("NEXUS_") or k.startswith("_NEXUS"):
            raise ServiceManifestError(
                f"environment key {k!r} uses a reserved prefix (NEXUS_ / _NEXUS)"
            )
        sv = str(v) if v is not None else ""
        environment[k] = sv
        total_bytes += len(k) + len(sv)
    if total_bytes > 16 * 1024:
        raise ServiceManifestError("environment payload exceeds 16 KB")

    # Cloud task-data sources. Normalised here so service-task
    # dispatch surfaces the same validation errors as the submit-time path
    # in nexus.api.local.add_workflow.
    data_block = validate_data_sources(manifest)

    # GPU passthrough. Reuse the docker helper's parser so the manifest and the
    # launch agree on what counts as a valid request. The service runs on THIS
    # host, so a request requires a GPU to actually be present here — fail fast
    # with a clear message rather than a confusing docker error at start time.
    from nexus.runtime.docker_client import _gpu_device_count
    from nexus.telemetry.hardware import detect_gpu

    try:
        gpu_count = _gpu_device_count(manifest.get("gpu"))
    except ValueError as exc:
        raise ServiceManifestError(str(exc))
    if gpu_count is not None and not detect_gpu():
        raise ServiceManifestError(
            "manifest requests a GPU but none was detected on this host"
        )
    gpu = manifest.get("gpu") if gpu_count is not None else None

    return {
        "image": image,
        "gpu": gpu,
        "expose_ports": ports,
        "duration_sec": duration,
        "idle_timeout_sec": idle,
        "service_kind": service_kind,
        "ram_limit_mb": max(64, ram),
        "cpu_limit_pct": max(1, min(800, cpu)),
        "entrypoint": entrypoint,
        "network_required": bool(manifest.get("network_required", True)),
        "replicas": replicas,
        "replica_strategy": strategy,
        "snapshot_interval_sec": snap_interval,
        "snapshot_paths": snap_paths,
        "primary_selection": primary_selection,
        "depends_on": deps,
        "rate_limit_mb_s": rate_limit_mb_s,
        "tls_terminate": tls_terminate,
        "session_replay": session_replay,
        "shared_tunnel": shared_tunnel,
        "protocol": protocol,
        "environment": environment,
        "data_sources": data_block["data_sources"],
        "workspace_source": data_block["workspace_source"],
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start_service(
    task_id: str,
    manifest: dict,
    env: dict | None = None,
    *,
    master_ip: str = "",
    extra_volumes: dict | None = None,
) -> dict:
    """Start a service container detached and return the live record.

    Raises :class:`ServiceManifestError` on bad manifest, or generic
    ``RuntimeError`` if Docker is unavailable. Returns the dict that gets
    cached in ``STATE.service_records[task_id]``.

    *extra_volumes* is a Docker-SDK ``volumes`` mapping —
    ``{host_path: {"bind": container_path, "mode": "rw"}}`` — used by the
    snapshot-promotion path to preload the new primary's data dirs.
    """
    spec = validate_service_manifest(manifest)
    docker_client = get_docker_client()  # raises RuntimeError if unavailable

    # Pull image if missing.
    try:
        await asyncio.to_thread(docker_client.images.get, spec["image"])
    except Exception:
        _log.info("[service:%s] pulling image %s", task_id, spec["image"])
        await asyncio.to_thread(docker_client.images.pull, spec["image"])

    # Manifest-supplied environment goes in first; the caller's
    # `env` (runtime-injected NEXUS_* discovery + master-side vars) wins on
    # collision. The validator already rejects reserved-prefix keys from
    # the manifest so collisions on dep-injection vars are impossible.
    container_env: dict = dict(spec.get("environment") or {})
    container_env.update(env or {})
    # Open dep tunnels and inject NEXUS_SERVICE_<ALIAS>_*
    # discovery vars before containers.run. Master pre-resolves the dep's
    # primary at dispatch and embeds NEXUS_DEP_<ALIAS>_PRIMARY/PORT in env.
    if spec["depends_on"]:
        await _wire_dependency_tunnels(task_id, spec["depends_on"], container_env)

    sec_opts = docker_security_opts(_pick_service_profile(LOCAL_SETTINGS))
    container_kwargs: dict[str, Any] = {
        "image": spec["image"],
        "detach": True,
        "ports": {f"{p}/tcp": None for p in spec["expose_ports"]},
        "mem_limit": f"{spec['ram_limit_mb']}m",
        "cpu_quota": int((spec["cpu_limit_pct"] / 100.0) * 100000),
        "environment": container_env,
        "network_mode": "bridge",
    }
    container_kwargs.update(sec_opts)
    # GPU passthrough (docker path): forward the host GPU when the manifest asks
    # for it; an unset request yields {} and leaves the launch unchanged.
    container_kwargs.update(docker_gpu_opts(spec.get("gpu")))
    if spec["entrypoint"]:
        container_kwargs["command"] = spec["entrypoint"]
    if extra_volumes:
        container_kwargs["volumes"] = dict(extra_volumes)

    container = await asyncio.to_thread(
        docker_client.containers.run, **container_kwargs
    )
    await register_running_container(task_id, container)

    # Resolve host-side ports. Docker assigns these asynchronously; one
    # reload() is usually enough but loop briefly for safety.
    port_map: dict[int, int] = {}
    for _ in range(10):
        try:
            await asyncio.to_thread(container.reload)
        except Exception as exc:
            _log.warning("[service:%s] container.reload failed: %s", task_id, exc)
            break
        ports_attr = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
        port_map = {}
        for cport in spec["expose_ports"]:
            entries = ports_attr.get(f"{cport}/tcp") or []
            if entries and entries[0].get("HostPort"):
                port_map[cport] = int(entries[0]["HostPort"])
        if len(port_map) == len(spec["expose_ports"]):
            break
        await asyncio.sleep(0.2)

    if len(port_map) != len(spec["expose_ports"]):
        await _force_stop(container)
        await unregister_running_container(task_id)
        raise RuntimeError(
            f"Docker did not bind every requested port for {task_id}: "
            f"want {spec['expose_ports']}, got {port_map}"
        )

    started_at = time.time()
    record = {
        "task_id": task_id,
        "image": spec["image"],
        "expose_ports": spec["expose_ports"],
        "service_kind": spec["service_kind"],
        "duration_sec": spec["duration_sec"],
        "idle_timeout_sec": spec["idle_timeout_sec"],
        "started_at": started_at,
        "expires_at": started_at + spec["duration_sec"],
        "master_ip": master_ip,
        "status": "running",
        "replica_strategy": spec["replica_strategy"],
        "replicas": spec["replicas"],
        "snapshot_paths": list(spec["snapshot_paths"]),
        "snapshot_interval_sec": spec["snapshot_interval_sec"],
        "rate_limit_mb_s": spec.get("rate_limit_mb_s", 0),
        "tls_terminate": spec.get("tls_terminate", False),
        "session_replay": spec.get("session_replay", False),
        "shared_tunnel": spec.get("shared_tunnel", False),
        "protocol": spec.get("protocol", "tcp"),
    }
    async with STATE.service_lock:
        STATE.service_records[task_id] = record
        STATE.service_port_mappings[task_id] = dict(port_map)
        STATE.service_last_activity[task_id] = started_at

    watchdog = asyncio.create_task(
        service_watchdog(task_id),
        name=f"nexus.service.watchdog.{task_id}",
    )
    async with STATE.service_lock:
        STATE.service_watchdog_tasks[task_id] = watchdog

    # Snapshot ticker : primary worker periodically ships
    # the configured data paths to the master.
    if spec["replica_strategy"] == "snapshot" and master_ip and spec["snapshot_paths"]:
        from nexus.runtime.service_replication import snapshot_ticker

        ticker = asyncio.create_task(
            snapshot_ticker(
                task_id,
                master_ip,
                list(spec["snapshot_paths"]),
                spec["snapshot_interval_sec"],
            ),
            name=f"nexus.service.snapshot.{task_id}",
        )
        async with STATE.service_lock:
            STATE.service_snapshot_tasks[task_id] = ticker

    _log.info(
        "[service:%s] started image=%s port_map=%s duration=%ds idle=%ds",
        task_id,
        spec["image"],
        port_map,
        spec["duration_sec"],
        spec["idle_timeout_sec"],
    )
    return record


async def stop_service(task_id: str, reason: str = "manual") -> bool:
    """Stop the service container. Returns True if anything was stopped."""
    async with STATE.service_lock:
        record = STATE.service_records.get(task_id)
        watchdog = STATE.service_watchdog_tasks.pop(task_id, None)
        ticker = STATE.service_snapshot_tasks.pop(task_id, None)

    # Self-cancel guard: if the watchdog is the calling task, cancelling it
    # mid-flight raises CancelledError on the next await and stop_service
    # never finishes (record["stop_reason"] would never be written).
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    if watchdog and not watchdog.done() and watchdog is not current:
        watchdog.cancel()
    if ticker and not ticker.done() and ticker is not current:
        ticker.cancel()

    if not record:
        return False

    container = STATE.running_task_containers.get(task_id)
    stopped = False
    if container is not None:
        await _force_stop(container)
        stopped = True

    async with STATE.service_lock:
        rec = STATE.service_records.get(task_id)
        if rec:
            rec["status"] = "stopped"
            rec["stop_reason"] = reason
            rec["stopped_at"] = time.time()
        STATE.service_port_mappings.pop(task_id, None)
        STATE.service_last_activity.pop(task_id, None)

    await unregister_running_container(task_id)

    try:
        from nexus.telemetry.audit import record_audit_event

        await record_audit_event(
            "service_stopped",
            actor=task_id,
            task_id=task_id,
            severity="info",
            details=f"reason={reason}",
        )
    except Exception:
        pass

    _log.info("[service:%s] stopped reason=%s", task_id, reason)
    return stopped


async def _force_stop(container) -> None:
    try:
        await asyncio.to_thread(container.stop, timeout=5)
    except Exception:
        pass
    try:
        await asyncio.to_thread(container.remove, force=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

async def service_watchdog(task_id: str) -> None:
    """Supervise a running service: duration, idle, container health."""
    try:
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_SEC)
            async with STATE.service_lock:
                record = STATE.service_records.get(task_id)
                last_activity = STATE.service_last_activity.get(task_id, 0.0)
                ports = STATE.service_port_mappings.get(task_id, {})
            if not record:
                return  # stop_service already cleaned up

            now = time.time()
            if now >= record["expires_at"]:
                _log.info("[service:%s] duration limit reached", task_id)
                await stop_service(task_id, reason="duration_limit")
                return

            idle_timeout = int(record.get("idle_timeout_sec", 0) or 0)
            if idle_timeout > 0 and last_activity and (now - last_activity) >= idle_timeout:
                _log.info(
                    "[service:%s] idle timeout (%.0fs since last byte)",
                    task_id,
                    now - last_activity,
                )
                await stop_service(task_id, reason="idle_timeout")
                return

            # Health probe — try connecting to the first exposed host port.
            if ports:
                host_port = next(iter(ports.values()))
                healthy = await _probe_tcp("127.0.0.1", host_port)
                async with STATE.service_lock:
                    rec = STATE.service_records.get(task_id)
                    if rec is not None:
                        rec["last_health_check"] = now
                        rec["status"] = "running" if healthy else "unhealthy"
    except asyncio.CancelledError:
        return
    except Exception as exc:
        _log.exception("[service:%s] watchdog crashed: %s", task_id, exc)
        await stop_service(task_id, reason=f"watchdog_error: {exc}")


async def _probe_tcp(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_HEALTH_PROBE_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Helpers used by the executor branch
# ---------------------------------------------------------------------------

def is_service_manifest(manifest: dict) -> bool:
    """Cheap check before attempting validation."""
    return str(manifest.get("runtime", "")).lower() == "service"


# ---------------------------------------------------------------------------
# Snapshot-restore start path 
# ---------------------------------------------------------------------------

async def start_with_snapshot(
    task_id: str,
    manifest: dict,
    *,
    master_ip: str = "",
    env: dict | None = None,
) -> dict:
    """Start a service from a previously-staged snapshot zip (promotion path).

    Extracts the snapshot into the on-disk staging dir, computes bind-mount
    pairs for each ``snapshot_paths`` entry, then defers to
    :func:`start_service` with the resulting ``extra_volumes``.
    """
    from nexus.runtime.service_replication import extract_snapshot

    snap_paths = list(manifest.get("snapshot_paths") or [])
    extra_volumes: dict = {}
    if snap_paths:
        staging = await extract_snapshot(task_id)
        for cpath in snap_paths:
            stripped = cpath.lstrip("/")
            if not stripped:
                continue
            basename = stripped.rstrip("/").rsplit("/", 1)[-1]
            host_dir = staging / stripped / basename
            host_dir.mkdir(parents=True, exist_ok=True)
            extra_volumes[str(host_dir)] = {"bind": cpath, "mode": "rw"}

    record = await start_service(
        task_id,
        manifest,
        env,
        master_ip=master_ip,
        extra_volumes=extra_volumes or None,
    )
    record["promoted"] = True
    record["promoted_at"] = time.time()
    return record


# ---------------------------------------------------------------------------
# Inter-service composition 
# ---------------------------------------------------------------------------

async def _wire_dependency_tunnels(
    task_id: str, deps: list[dict], container_env: dict
) -> None:
    """For each dep, ensure a worker-local tunnel and inject discovery env.

    The master has already injected ``NEXUS_DEP_<ALIAS>_PRIMARY`` and
    ``NEXUS_DEP_<ALIAS>_PORT`` into the task env at dispatch time. Here we
    open (or reuse) a 127.0.0.1 listener pointed at the dep's primary and
    expose the bound port to the container as ``NEXUS_SERVICE_<ALIAS>_HOST``
    + ``NEXUS_SERVICE_<ALIAS>_PORT``. Missing dep env vars are skipped with
    a warning so the container can still start (and crash-loop if needed).
    """
    from nexus.networking.tunnel import ensure_dependency_tunnel

    for dep in deps:
        alias = str(dep.get("alias") or "").strip().upper()
        dep_id = str(dep.get("service_id") or "").strip()
        if not alias or not dep_id:
            continue
        primary = str(container_env.get(f"NEXUS_DEP_{alias}_PRIMARY", "") or "")
        port_str = str(container_env.get(f"NEXUS_DEP_{alias}_PORT", "") or "")
        if not primary or not port_str:
            _log.warning(
                "[service:%s] dep alias=%s id=%s missing master-resolved address",
                task_id,
                alias,
                dep_id,
            )
            continue
        try:
            container_port = int(port_str)
        except ValueError:
            _log.warning(
                "[service:%s] dep alias=%s bad port %r", task_id, alias, port_str
            )
            continue
        local_port = await ensure_dependency_tunnel(dep_id, primary, container_port)
        container_env[f"NEXUS_SERVICE_{alias}_HOST"] = "127.0.0.1"
        container_env[f"NEXUS_SERVICE_{alias}_PORT"] = str(local_port)


__all__ = [
    "ServiceManifestError",
    "VALID_DATA_SOURCE_TYPES",
    "validate_service_manifest",
    "validate_data_sources",
    "start_service",
    "start_with_snapshot",
    "stop_service",
    "service_watchdog",
    "is_service_manifest",
]
