"""Failover + traffic switch tests (Wave 4 Step 9e)."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core import STATE
from nexus.networking.tunnel import reroute_tunnel
from nexus.runtime import service_replication, service_runner
from nexus.runtime.service_replication import (
    extract_snapshot,
    promote_standby,
    snapshot_dir_for,
)
from nexus.runtime.service_runner import start_with_snapshot
from nexus.scheduler.dag import service_health_pass


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
    STATE.service_tunnels.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_snapshot_tasks.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.active_workers.clear()
    yield
    STATE.service_records.clear()
    STATE.service_standbys.clear()
    STATE.service_tunnels.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_snapshot_tasks.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.active_workers.clear()


# ---------------------------------------------------------------------------
# reroute_tunnel
# ---------------------------------------------------------------------------

def test_reroute_tunnel_no_record_returns_zero():
    assert _run(reroute_tunnel("missing", "w-new")) == 0


def test_reroute_tunnel_updates_peer_id_and_clears_streams():
    writer = MagicMock()
    writer.close = MagicMock()
    STATE.service_tunnels["svc-1"] = {
        "server": MagicMock(),
        "port": 12345,
        "peer_id": "w-old",
        "streams": {
            "tun-a": {"side": "master", "peer_id": "w-old", "writer": writer},
        },
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock(return_value=True)) as snd:
        closed = _run(reroute_tunnel("svc-1", "w-new"))

    assert closed == 1
    rec = STATE.service_tunnels["svc-1"]
    assert rec["peer_id"] == "w-new"
    assert rec["streams"] == {}
    # tunnel_close was sent to the old peer for the rerouted stream.
    assert snd.await_count == 1
    target, frame = snd.await_args.args
    assert target == "w-old"
    assert frame["type"] == "tunnel_close"
    assert frame["tunnel_id"] == "tun-a"
    assert frame["reason"] == "rerouted"


def test_reroute_tunnel_keeps_listener_open():
    server = MagicMock()
    STATE.service_tunnels["svc-1"] = {
        "server": server,
        "port": 5555,
        "peer_id": "w-old",
        "streams": {},
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock(return_value=True)):
        _run(reroute_tunnel("svc-1", "w-new"))

    # Listener stays bound; close() is NOT called.
    assert server.close.call_count == 0
    assert STATE.service_tunnels["svc-1"]["port"] == 5555


# ---------------------------------------------------------------------------
# extract_snapshot
# ---------------------------------------------------------------------------

def test_extract_snapshot_missing_zip_raises():
    with pytest.raises(FileNotFoundError):
        _run(extract_snapshot("never-staged"))


def test_extract_snapshot_unzips_files_to_staging(tmp_path, monkeypatch):
    # Stage a zip on disk under the expected snapshot_dir.
    base = snapshot_dir_for("svc-x")
    src = base / "snapshot.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/data/file1.txt", b"hello")
        zf.writestr("data/data/sub/file2.txt", b"world")
    src.write_bytes(buf.getvalue())

    try:
        staging = _run(extract_snapshot("svc-x"))
        assert (staging / "data" / "data" / "file1.txt").read_bytes() == b"hello"
        assert (staging / "data" / "data" / "sub" / "file2.txt").read_bytes() == b"world"
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# start_with_snapshot — volume-mount construction
# ---------------------------------------------------------------------------

def test_start_with_snapshot_builds_volume_for_data_path(tmp_path):
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "snapshot_paths": ["/data"],
    }

    fake_staging = tmp_path / "staging"
    (fake_staging / "data" / "data").mkdir(parents=True)

    captured: dict = {}

    async def fake_extract(_task_id):
        return fake_staging

    async def fake_start(task_id, mf, env, *, master_ip="", extra_volumes=None):
        captured["extra_volumes"] = extra_volumes
        return {"task_id": task_id}

    with patch.object(service_replication, "extract_snapshot", fake_extract), \
         patch.object(service_runner, "start_service", fake_start):
        record = _run(start_with_snapshot("svc-1", manifest, master_ip="m"))

    assert record["promoted"] is True
    extras = captured["extra_volumes"]
    assert len(extras) == 1
    host_path, spec = next(iter(extras.items()))
    assert spec == {"bind": "/data", "mode": "rw"}
    assert host_path.endswith(str(fake_staging / "data" / "data")) or \
        host_path == str(fake_staging / "data" / "data")


def test_start_with_snapshot_handles_multilevel_paths(tmp_path):
    manifest = {
        "runtime": "service",
        "image": "postgres:15",
        "expose_ports": [5432],
        "snapshot_paths": ["/var/lib/postgresql"],
    }
    fake_staging = tmp_path / "staging"
    (fake_staging / "var" / "lib" / "postgresql" / "postgresql").mkdir(parents=True)

    captured: dict = {}

    async def fake_extract(_task_id):
        return fake_staging

    async def fake_start(task_id, mf, env, *, master_ip="", extra_volumes=None):
        captured["extra_volumes"] = extra_volumes
        return {"task_id": task_id}

    with patch.object(service_replication, "extract_snapshot", fake_extract), \
         patch.object(service_runner, "start_service", fake_start):
        _run(start_with_snapshot("svc-pg", manifest))

    extras = captured["extra_volumes"]
    assert len(extras) == 1
    host_path, spec = next(iter(extras.items()))
    assert spec == {"bind": "/var/lib/postgresql", "mode": "rw"}
    expected = str(fake_staging / "var" / "lib" / "postgresql" / "postgresql")
    assert host_path == expected


def test_start_with_snapshot_no_paths_passes_no_volumes():
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
    }
    captured: dict = {}

    async def fake_start(task_id, mf, env, *, master_ip="", extra_volumes=None):
        captured["extra_volumes"] = extra_volumes
        return {"task_id": task_id}

    with patch.object(service_runner, "start_service", fake_start):
        _run(start_with_snapshot("svc-1", manifest))

    assert captured["extra_volumes"] is None


# ---------------------------------------------------------------------------
# promote_standby
# ---------------------------------------------------------------------------

def test_promote_standby_no_record_raises():
    with pytest.raises(RuntimeError, match="not a standby"):
        _run(promote_standby("nonexistent"))


def test_promote_standby_invokes_start_with_snapshot_and_clears():
    STATE.service_standbys["svc-1"] = {
        "task_id": "svc-1",
        "manifest": {"runtime": "service", "image": "redis:7", "expose_ports": [6379]},
        "image": "redis:7",
        "master_ip": "10.0.0.1",
    }

    async def fake_start(task_id, mf, *, master_ip="", env=None):
        return {"task_id": task_id, "image": mf.get("image"), "master": master_ip}

    with patch(
        "nexus.runtime.service_runner.start_with_snapshot", AsyncMock(side_effect=fake_start)
    ) as start:
        rec = _run(promote_standby("svc-1"))

    assert rec["task_id"] == "svc-1"
    assert rec["master"] == "10.0.0.1"
    start.assert_awaited_once()
    # Standby record was cleared after promotion.
    assert "svc-1" not in STATE.service_standbys


# ---------------------------------------------------------------------------
# service_health_pass
# ---------------------------------------------------------------------------

def test_health_pass_no_action_when_primary_online():
    STATE.active_workers["w-A"] = {"last_seen": time.time()}
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "worker_id": "w-A",
        "standbys": ["w-B"],
        "replica_strategy": "snapshot",
        "status": "running",
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock()) as snd, \
         patch("nexus.telemetry.audit.record_audit_event", AsyncMock()) as audit:
        _run(service_health_pass())

    snd.assert_not_called()
    audit.assert_not_called()
    assert STATE.service_records["svc-1"]["worker_id"] == "w-A"


def test_health_pass_skips_failed_services():
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "worker_id": "",
        "standbys": [],
        "status": "failed",
    }

    with patch("nexus.telemetry.audit.record_audit_event", AsyncMock()) as audit:
        _run(service_health_pass())

    audit.assert_not_called()


def test_health_pass_promotes_first_online_standby():
    STATE.active_workers["w-A"] = {"last_seen": time.time() - 30}  # offline
    STATE.active_workers["w-B"] = {"last_seen": time.time() - 30}  # offline
    STATE.active_workers["w-C"] = {"last_seen": time.time()}        # online
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "worker_id": "w-A",
        "standbys": ["w-B", "w-C"],
        "replica_strategy": "snapshot",
        "status": "running",
    }
    STATE.service_tunnels["svc-1"] = {
        "server": MagicMock(),
        "port": 5555,
        "peer_id": "w-A",
        "streams": {},
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock(return_value=True)) as snd, \
         patch("nexus.telemetry.audit.record_audit_event", AsyncMock()) as audit:
        _run(service_health_pass())

    rec = STATE.service_records["svc-1"]
    assert rec["worker_id"] == "w-C"
    assert "w-C" not in rec["standbys"]
    assert rec["status"] == "running"
    # Listener now points at the new primary.
    assert STATE.service_tunnels["svc-1"]["peer_id"] == "w-C"

    actions = [c.args[0] for c in audit.await_args_list]
    assert "service_primary_lost" in actions
    assert "service_primary_promoted" in actions

    promote_calls = [
        c for c in snd.await_args_list
        if c.args[1].get("type") == "service_promote_with_snapshot"
    ]
    assert len(promote_calls) == 1
    assert promote_calls[0].args[0] == "w-C"


def test_health_pass_marks_failed_when_no_standbys_online():
    STATE.active_workers["w-A"] = {"last_seen": time.time() - 30}
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "worker_id": "w-A",
        "standbys": [],
        "replica_strategy": "snapshot",
        "status": "running",
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock()), \
         patch("nexus.telemetry.audit.record_audit_event", AsyncMock()) as audit:
        _run(service_health_pass())

    rec = STATE.service_records["svc-1"]
    assert rec["status"] == "failed"
    assert rec["worker_id"] == ""
    actions = [c.args[0] for c in audit.await_args_list]
    assert "service_no_replicas_left" in actions


def test_health_pass_marks_failed_when_all_standbys_offline():
    STATE.active_workers["w-A"] = {"last_seen": time.time() - 30}
    STATE.active_workers["w-B"] = {"last_seen": time.time() - 30}
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "worker_id": "w-A",
        "standbys": ["w-B"],
        "replica_strategy": "snapshot",
        "status": "running",
    }

    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock()), \
         patch("nexus.telemetry.audit.record_audit_event", AsyncMock()) as audit:
        _run(service_health_pass())

    actions = [c.args[0] for c in audit.await_args_list]
    assert "service_no_replicas_left" in actions
    assert STATE.service_records["svc-1"]["status"] == "failed"
