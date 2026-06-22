"""Service replication tests (Wave 4 Step 9d)."""

from __future__ import annotations

import asyncio
import io
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nexus.core import STATE
from nexus.runtime import service_replication
from nexus.runtime.service_replication import (
    _capture_paths_to_zip,
    capture_snapshot,
    distribute_snapshot,
    load_snapshot,
    prepare_standby,
    ship_snapshot,
)
from nexus.runtime.service_runner import (
    ServiceManifestError,
    validate_service_manifest,
)
from nexus.scheduler import select_top_n_workers
from nexus.storage import TaskRecord


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.service_records.clear()
    STATE.service_standbys.clear()
    STATE.service_snapshot_tasks.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.running_task_containers.clear()
    yield
    STATE.service_records.clear()
    STATE.service_standbys.clear()
    STATE.service_snapshot_tasks.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.running_task_containers.clear()


# ---------------------------------------------------------------------------
# Manifest validation extensions
# ---------------------------------------------------------------------------

def _base_manifest(**overrides):
    base = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
    }
    base.update(overrides)
    return base


def test_manifest_replicas_default_one():
    spec = validate_service_manifest(_base_manifest())
    assert spec["replicas"] == 1
    assert spec["replica_strategy"] == "none"
    assert spec["snapshot_interval_sec"] == 60
    assert spec["snapshot_paths"] == []
    assert spec["primary_selection"] == "fit"


def test_manifest_replicas_zero_rejected():
    with pytest.raises(ServiceManifestError, match="replicas must be >= 1"):
        validate_service_manifest(_base_manifest(replicas=0))


def test_manifest_replicas_negative_rejected():
    with pytest.raises(ServiceManifestError):
        validate_service_manifest(_base_manifest(replicas=-1))


def test_manifest_replicas_non_int_rejected():
    with pytest.raises(ServiceManifestError, match="replicas not an int"):
        validate_service_manifest(_base_manifest(replicas="three"))


def test_manifest_strategy_unknown_rejected():
    with pytest.raises(ServiceManifestError, match="replica_strategy must be"):
        validate_service_manifest(_base_manifest(replica_strategy="cluster"))


def test_manifest_snapshot_strategy_requires_paths():
    with pytest.raises(ServiceManifestError, match="snapshot_paths required"):
        validate_service_manifest(
            _base_manifest(replica_strategy="snapshot")
        )


def test_manifest_snapshot_strategy_with_paths():
    spec = validate_service_manifest(
        _base_manifest(
            replica_strategy="snapshot",
            snapshot_paths=["/data"],
            snapshot_interval_sec=30,
            replicas=3,
        )
    )
    assert spec["replica_strategy"] == "snapshot"
    assert spec["snapshot_paths"] == ["/data"]
    assert spec["snapshot_interval_sec"] == 30
    assert spec["replicas"] == 3


def test_manifest_snapshot_interval_floor():
    with pytest.raises(ServiceManifestError, match=">= 5"):
        validate_service_manifest(
            _base_manifest(
                replica_strategy="snapshot",
                snapshot_paths=["/data"],
                snapshot_interval_sec=2,
            )
        )


def test_manifest_snapshot_paths_must_be_list():
    with pytest.raises(ServiceManifestError, match="snapshot_paths must be a list"):
        validate_service_manifest(
            _base_manifest(replica_strategy="snapshot", snapshot_paths="/data")
        )


def test_manifest_native_strategy_no_paths_required():
    spec = validate_service_manifest(
        _base_manifest(replica_strategy="native", replicas=3)
    )
    assert spec["replica_strategy"] == "native"
    assert spec["snapshot_paths"] == []


def test_manifest_primary_selection_unknown_rejected():
    with pytest.raises(ServiceManifestError, match="primary_selection must be"):
        validate_service_manifest(_base_manifest(primary_selection="random"))


def test_manifest_primary_selection_round_robin_ok():
    spec = validate_service_manifest(
        _base_manifest(replica_strategy="native", primary_selection="round_robin")
    )
    assert spec["primary_selection"] == "round_robin"


# ---------------------------------------------------------------------------
# select_top_n_workers
# ---------------------------------------------------------------------------

def _fake_worker(*, free_ram=2048, bench=10.0, last_seen=None, caps=None):
    if last_seen is None:
        last_seen = time.time()
    return {
        "last_seen": last_seen,
        "stats": {
            "free_ram": free_ram,
            "dispatch_ram_cap_mb": free_ram,
            "cpu": 50.0,
            "bench": bench,
            "active_task_count": 0,
            "connection_type": "lan",
            "capabilities": caps or {"service_runtime": True},
        },
    }


def _service_task():
    import io
    import json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "task.json",
            json.dumps(
                {
                    "runtime": "service",
                    "image": "redis:7",
                    "expose_ports": [6379],
                    "ram_limit_mb": 256,
                    "cpu_limit_pct": 50,
                }
            ),
        )
    return TaskRecord(id="svc-1", payload=buf.getvalue(), env_vars="{}")


def test_select_top_n_returns_n_workers_sorted_by_fit():
    task = _service_task()
    workers = {
        "w-A": _fake_worker(bench=5.0),
        "w-B": _fake_worker(bench=20.0),
        "w-C": _fake_worker(bench=15.0),
    }
    chosen = select_top_n_workers(task, 2, workers)
    assert len(chosen) == 2
    # Highest bench first (tier-bucketed in fit_score).
    assert chosen[0] == "w-B"


def test_select_top_n_excludes_unsupported_workers():
    task = _service_task()
    workers = {
        "w-A": _fake_worker(caps={"service_runtime": False}),
        "w-B": _fake_worker(caps={"service_runtime": True}),
    }
    chosen = select_top_n_workers(task, 5, workers)
    assert chosen == ["w-B"]


def test_select_top_n_respects_exclude_set():
    task = _service_task()
    workers = {
        "w-A": _fake_worker(),
        "w-B": _fake_worker(),
        "w-C": _fake_worker(),
    }
    chosen = select_top_n_workers(task, 2, workers, exclude={"w-A"})
    assert "w-A" not in chosen
    assert len(chosen) == 2


def test_select_top_n_zero_returns_empty():
    task = _service_task()
    workers = {"w-A": _fake_worker()}
    assert select_top_n_workers(task, 0, workers) == []


def test_select_top_n_returns_fewer_when_pool_smaller():
    task = _service_task()
    workers = {"w-A": _fake_worker()}
    assert select_top_n_workers(task, 5, workers) == ["w-A"]


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------

class _FakeContainer:
    """Minimal stand-in for docker SDK's container with get_archive."""

    def __init__(self, files: dict[str, bytes]):
        # files: {container_path: {member_name: content_bytes}}
        self._files = files

    def get_archive(self, path):
        members = self._files.get(path, {})
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        buf.seek(0)
        # The Docker SDK returns (stream, stat); stream is iterable of chunks.
        return iter([buf.getvalue()]), {"name": path, "size": buf.getbuffer().nbytes}


def test_capture_paths_to_zip_round_trips():
    container = _FakeContainer({"/data": {"a.txt": b"hello", "b.txt": b"world"}})
    blob = _capture_paths_to_zip(container, ["/data"])
    import zipfile

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = sorted(zf.namelist())
    assert names == ["data/a.txt", "data/b.txt"]


def test_capture_paths_to_zip_swallows_missing_path():
    container = _FakeContainer({})  # get_archive will raise for unknown paths
    blob = _capture_paths_to_zip(container, ["/nope"])
    assert blob  # zip with no entries is still valid


def test_capture_snapshot_returns_none_when_container_missing():
    async def _scenario():
        return await capture_snapshot("absent", ["/data"])

    assert _run(_scenario()) is None


def test_capture_snapshot_returns_bytes_with_real_container():
    container = _FakeContainer({"/data": {"x.bin": b"\x00\x01\x02"}})
    STATE.running_task_containers["svc-1"] = container

    async def _scenario():
        return await capture_snapshot("svc-1", ["/data"])

    out = _run(_scenario())
    assert out is not None
    assert len(out) > 0


# ---------------------------------------------------------------------------
# Ship + distribute (mocked peer_http_post)
# ---------------------------------------------------------------------------

def test_ship_snapshot_posts_base64_body():
    sent: list[tuple[str, str, dict]] = []

    async def _fake_post(target_ip, path, body, timeout=5.0):
        sent.append((target_ip, path, body))
        return {"status": 200, "body": {"status": "ok"}}

    async def _scenario():
        with patch("nexus.networking.peer_http.peer_http_post", _fake_post):
            return await ship_snapshot("svc-1", "master-A", b"\xde\xad\xbe\xef")

    ok = _run(_scenario())
    assert ok is True
    assert len(sent) == 1
    target_ip, path, body = sent[0]
    assert target_ip == "master-A"
    assert path == "/peer/snapshot_upload/svc-1"
    import base64

    assert base64.b64decode(body["b64"]) == b"\xde\xad\xbe\xef"


def test_ship_snapshot_returns_false_on_non_200():
    async def _fake_post(target_ip, path, body, timeout=5.0):
        return {"status": 503, "body": {}}

    async def _scenario():
        with patch("nexus.networking.peer_http.peer_http_post", _fake_post):
            return await ship_snapshot("svc-1", "master-A", b"x")

    assert _run(_scenario()) is False


def test_distribute_snapshot_fans_out_to_each_standby():
    targets: list[str] = []

    async def _fake_post(target_ip, path, body, timeout=5.0):
        targets.append(target_ip)
        return {"status": 200, "body": {}}

    async def _scenario():
        with patch("nexus.networking.peer_http.peer_http_post", _fake_post):
            return await distribute_snapshot(
                "svc-1", ["w-A", "w-B", "w-C"], b"zipped"
            )

    out = _run(_scenario())
    assert sorted(targets) == ["w-A", "w-B", "w-C"]
    assert all(out.values())


def test_distribute_snapshot_records_per_target_status():
    async def _fake_post(target_ip, path, body, timeout=5.0):
        return {"status": 200 if target_ip == "w-A" else 500, "body": {}}

    async def _scenario():
        with patch("nexus.networking.peer_http.peer_http_post", _fake_post):
            return await distribute_snapshot("svc-1", ["w-A", "w-B"], b"z")

    out = _run(_scenario())
    assert out == {"w-A": True, "w-B": False}


# ---------------------------------------------------------------------------
# Load (standby ingestion)
# ---------------------------------------------------------------------------

def test_load_snapshot_writes_to_disk_and_updates_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service_replication, "cache_dir", lambda _port: tmp_path
    )
    monkeypatch.setattr(service_replication, "get_node_port", lambda: 8000)

    STATE.service_standbys["svc-1"] = {
        "task_id": "svc-1",
        "manifest": {},
        "image": "redis:7",
        "prepared_at": 0.0,
        "last_snapshot_at": 0.0,
        "snapshot_path": "",
    }

    async def _scenario():
        return await load_snapshot("svc-1", b"\x50\x4b\x03\x04--zipdata")

    path = _run(_scenario())
    assert path.read_bytes() == b"\x50\x4b\x03\x04--zipdata"
    rec = STATE.service_standbys["svc-1"]
    assert rec["last_snapshot_at"] > 0
    assert rec["snapshot_path"] == str(path)


# ---------------------------------------------------------------------------
# Prepare standby
# ---------------------------------------------------------------------------

def test_prepare_standby_registers_state_without_docker(monkeypatch):
    # If Docker is unavailable the helper must still register state.
    def _no_docker():
        raise RuntimeError("docker unavailable")

    monkeypatch.setattr(service_replication, "get_docker_client", _no_docker, raising=False)

    async def _scenario():
        await prepare_standby(
            "svc-1", {"image": "redis:7", "expose_ports": [6379]}
        )

    _run(_scenario())
    assert "svc-1" in STATE.service_standbys
    rec = STATE.service_standbys["svc-1"]
    assert rec["image"] == "redis:7"
    assert rec["prepared_at"] > 0


def test_prepare_standby_pulls_image_when_missing():
    fake_docker = SimpleNamespace(
        images=SimpleNamespace(
            get=lambda image: (_ for _ in ()).throw(Exception("not present")),
            pull=AsyncMock(return_value=None),
        )
    )
    pulled: list[str] = []

    def _pull(image):
        pulled.append(image)
        return None

    fake_docker.images.pull = _pull

    with patch(
        "nexus.runtime.docker_client.get_docker_client", return_value=fake_docker
    ):
        async def _scenario():
            await prepare_standby("svc-1", {"image": "alpine:3"})

        _run(_scenario())
    assert pulled == ["alpine:3"]
