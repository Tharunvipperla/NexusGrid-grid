"""Service replication primitives.

Three strategies, configured per service via ``replica_strategy`` in the
manifest:

* ``none`` — single primary, no standbys (= Step 9a behaviour).
* ``snapshot`` — primary exports its data dirs every ``snapshot_interval_sec``;
  master forwards snapshots to standbys, which stage them on disk. Standby
  containers are **not** running until promotion (Step 9e).
* ``native`` — every replica runs a full container; the user's image owns
  cluster discovery (e.g. Redis Cluster, Postgres streaming replication).
  Master picks one as primary for tunnel routing; rest are dial-fallbacks.

This module owns the snapshot pipeline. The native path needs nothing
beyond the regular service runner — the master just spawns N tasks.

The pipeline:

    primary worker --(/peer/snapshot_upload)--> master
            master --(/peer/snapshot_load)----> each standby

Snapshots are zip files of the manifest-declared ``snapshot_paths``,
extracted from the running container with ``container.get_archive``. The
network format is JSON + base64 to keep the wire path uniform with the
existing ``peer_http_post`` machinery — payloads are bounded by
``max_result_bytes`` (default 100 MB) for the same reason task results are.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import tarfile
import time
import zipfile
from pathlib import Path

from nexus.core import STATE, cache_dir, get_node_port
from nexus.utils import safe_extractall

_log = logging.getLogger("nexus.runtime.service_replication")


_DEFAULT_SNAPSHOT_INTERVAL_SEC = 60


# ---------------------------------------------------------------------------
# Capture (primary worker side)
# ---------------------------------------------------------------------------

def _capture_paths_to_zip(container, paths: list[str]) -> bytes:
    """Return a zip of the named *paths* extracted from *container*.

    Uses ``container.get_archive(path)`` (Docker SDK) which yields a tar
    stream + a ``stat`` dict. We re-pack the tar entries into a single zip
    so the standby can extract with the stdlib ``zipfile`` module without
    needing tar tooling or ``docker cp`` itself.

    (5a.4.4): pause the container around the multi-path
    capture so all paths reflect the same point-in-time. Cost: ~50 ms
    freeze visible to clients. Trade-off taken because torn snapshots
    across `/data` + `/wal` were the more common, harder-to-debug
    failure mode.
    """
    paused = False
    try:
        try:
            container.pause()
            paused = True
        except Exception as exc:
            _log.debug("container.pause failed (continuing unpaused): %s", exc)

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in paths:
                try:
                    stream, _stat = container.get_archive(path)
                except Exception as exc:
                    _log.warning("get_archive(%s) failed: %s", path, exc)
                    continue
                buf = io.BytesIO()
                for chunk in stream:
                    buf.write(chunk)
                buf.seek(0)
                try:
                    with tarfile.open(fileobj=buf, mode="r|*") as tf:
                        for member in tf:
                            if not member.isfile():
                                continue
                            f = tf.extractfile(member)
                            if f is None:
                                continue
                            arcname = f"{path.lstrip('/')}/{member.name}"
                            zf.writestr(arcname, f.read())
                except tarfile.TarError as exc:
                    _log.warning("tar parse failed for %s: %s", path, exc)
        return out.getvalue()
    finally:
        if paused:
            try:
                container.unpause()
            except Exception as exc:
                _log.warning("container.unpause failed: %s", exc)


async def capture_snapshot(task_id: str, paths: list[str]) -> bytes | None:
    """Capture *paths* from the running container for *task_id*.

    Returns the zip bytes, or ``None`` if the container is gone or every
    path failed.
    """
    container = STATE.running_task_containers.get(task_id)
    if container is None:
        return None
    try:
        zip_bytes = await asyncio.to_thread(
            _capture_paths_to_zip, container, list(paths)
        )
    except Exception as exc:
        _log.exception("[snapshot:%s] capture failed: %s", task_id, exc)
        return None
    if not zip_bytes:
        return None
    return zip_bytes


# ---------------------------------------------------------------------------
# Ship (primary worker -> master)
# ---------------------------------------------------------------------------

async def ship_snapshot(
    task_id: str, master_ip: str, zip_bytes: bytes
) -> bool:
    """POST a captured snapshot to the master. Returns True on 200 OK."""
    from nexus.networking.peer_http import peer_http_post

    body = {
        "task_id": task_id,
        "b64": base64.b64encode(zip_bytes).decode("ascii"),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
    }
    res = await peer_http_post(
        master_ip, f"/peer/snapshot_upload/{task_id}", body, timeout=30.0
    )
    ok = int(res.get("status", 0)) == 200
    if not ok:
        _log.warning(
            "[snapshot:%s] ship to %s returned %s",
            task_id,
            master_ip,
            res.get("status"),
        )
    return ok


# ---------------------------------------------------------------------------
# Distribute (master -> standbys)
# ---------------------------------------------------------------------------

async def distribute_snapshot(
    task_id: str, standby_ips: list[str], zip_bytes: bytes
) -> dict[str, bool]:
    """Forward a snapshot to every standby. Returns ``{ip: ok}``."""
    from nexus.networking.peer_http import peer_http_post

    body = {
        "task_id": task_id,
        "b64": base64.b64encode(zip_bytes).decode("ascii"),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
    }
    out: dict[str, bool] = {}

    async def _one(ip: str) -> None:
        try:
            res = await peer_http_post(
                ip, f"/peer/snapshot_load/{task_id}", body, timeout=30.0
            )
            out[ip] = int(res.get("status", 0)) == 200
        except Exception as exc:
            _log.warning("[snapshot:%s] distribute to %s failed: %s", task_id, ip, exc)
            out[ip] = False

    await asyncio.gather(*(_one(ip) for ip in standby_ips))
    return out


# ---------------------------------------------------------------------------
# Standby (load + prepare)
# ---------------------------------------------------------------------------

def snapshot_dir_for(task_id: str) -> Path:
    """Return the on-disk directory where this node stages snapshots for *task_id*."""
    base = cache_dir(get_node_port()) / "services" / task_id
    base.mkdir(parents=True, exist_ok=True)
    return base


async def load_snapshot(task_id: str, zip_bytes: bytes) -> Path:
    """Persist a received snapshot zip to disk. Returns the file path."""
    target = snapshot_dir_for(task_id) / "snapshot.zip"

    def _write() -> None:
        target.write_bytes(zip_bytes)

    await asyncio.to_thread(_write)
    async with STATE.service_lock:
        rec = STATE.service_standbys.get(task_id)
        if rec is not None:
            rec["last_snapshot_at"] = time.time()
            rec["snapshot_path"] = str(target)
    _log.info("[snapshot:%s] staged %d bytes -> %s", task_id, len(zip_bytes), target)
    return target


async def extract_snapshot(task_id: str) -> Path:
    """Unzip the staged snapshot into a sibling ``staging`` dir (Step 9e).

    Returns the staging directory path. Idempotent: a previous extraction
    is wiped first so the new primary starts from the latest snapshot.
    """
    base = snapshot_dir_for(task_id)
    src = base / "snapshot.zip"
    if not src.exists():
        raise FileNotFoundError(f"no staged snapshot for {task_id}: {src}")
    staging = base / "staging"

    def _do_extract() -> None:
        import shutil

        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        # Network-sourced archive (a peer's snapshot): reject path-traversal
        # entries rather than trusting the sender. Matches every other unzip
        # in the codebase (B3 / backup / plugin packages / workspace).
        with zipfile.ZipFile(src, "r") as zf:
            safe_extractall(zf, str(staging))

    await asyncio.to_thread(_do_extract)
    _log.info("[snapshot:%s] extracted -> %s", task_id, staging)
    return staging


async def promote_standby(task_id: str) -> dict:
    """Standby-side: bring up the service from the staged snapshot.

    Looks up the manifest cached during :func:`prepare_standby`, calls
    :func:`start_with_snapshot`, and clears the standby record. Returns the
    new service record.
    """
    from nexus.runtime.service_runner import start_with_snapshot

    async with STATE.service_lock:
        rec = STATE.service_standbys.get(task_id)
    if rec is None:
        raise RuntimeError(f"not a standby for {task_id}")
    manifest = dict(rec.get("manifest") or {})
    master_ip = str(rec.get("master_ip") or "")

    record = await start_with_snapshot(
        task_id, manifest, master_ip=master_ip
    )

    async with STATE.service_lock:
        STATE.service_standbys.pop(task_id, None)
    _log.info("[promote:%s] started as new primary", task_id)
    return record


async def prepare_standby(
    task_id: str, manifest: dict, master_ip: str = ""
) -> None:
    """Standby-side: register intent, pull image so promotion is fast."""
    from nexus.runtime.docker_client import get_docker_client

    image = str(manifest.get("image", "") or "").strip()
    async with STATE.service_lock:
        STATE.service_standbys[task_id] = {
            "task_id": task_id,
            "manifest": dict(manifest),
            "image": image,
            "master_ip": master_ip,
            "prepared_at": time.time(),
            "last_snapshot_at": 0.0,
            "snapshot_path": "",
        }
    if not image:
        return
    try:
        client = get_docker_client()
    except Exception as exc:
        _log.warning("[standby:%s] docker unavailable: %s", task_id, exc)
        return
    try:
        await asyncio.to_thread(client.images.get, image)
    except Exception:
        try:
            _log.info("[standby:%s] pulling %s", task_id, image)
            await asyncio.to_thread(client.images.pull, image)
        except Exception as exc:
            _log.warning("[standby:%s] image pull failed: %s", task_id, exc)


async def refresh_standby_image(task_id: str, image: str) -> None:
    """Re-pull *image* on this standby so promotion uses the latest tag.

    master sends ``service_image_refresh`` periodically;
    handler delegates here. No-op if docker is unavailable or the standby
    is no longer registered.
    """
    from nexus.runtime.docker_client import get_docker_client

    async with STATE.service_lock:
        if task_id not in STATE.service_standbys:
            return
    image = (image or "").strip()
    if not image:
        return
    try:
        client = get_docker_client()
    except Exception as exc:
        _log.warning("[standby:%s] docker unavailable: %s", task_id, exc)
        return
    try:
        _log.info("[standby:%s] refreshing image %s", task_id, image)
        await asyncio.to_thread(client.images.pull, image)
        async with STATE.service_lock:
            rec = STATE.service_standbys.get(task_id)
            if rec is not None:
                rec["last_image_refresh_at"] = time.time()
    except Exception as exc:
        _log.warning("[standby:%s] image refresh failed: %s", task_id, exc)


# ---------------------------------------------------------------------------
# Snapshot ticker (runs on the primary worker)
# ---------------------------------------------------------------------------

async def snapshot_ticker(
    task_id: str, master_ip: str, paths: list[str], interval_sec: int
) -> None:
    """Periodically capture + ship the service's data paths to the master."""
    interval = max(5, int(interval_sec or _DEFAULT_SNAPSHOT_INTERVAL_SEC))
    try:
        while True:
            await asyncio.sleep(interval)
            if task_id not in STATE.service_records:
                return
            zip_bytes = await capture_snapshot(task_id, paths)
            if zip_bytes is None:
                continue
            ok = await ship_snapshot(task_id, master_ip, zip_bytes)
            if ok:
                async with STATE.service_lock:
                    rec = STATE.service_records.get(task_id)
                    if rec is not None:
                        rec["last_snapshot_at"] = time.time()
                        rec["last_snapshot_bytes"] = len(zip_bytes)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        _log.exception("[snapshot:%s] ticker crashed: %s", task_id, exc)


__all__ = [
    "capture_snapshot",
    "ship_snapshot",
    "distribute_snapshot",
    "load_snapshot",
    "prepare_standby",
    "refresh_standby_image",
    "snapshot_ticker",
    "snapshot_dir_for",
]
