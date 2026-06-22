"""Inter-service composition tests (Wave 4 Step 9f)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core import STATE
from nexus.networking.tunnel import (
    _peer_owns_service,
    ensure_dependency_tunnel,
)
from nexus.runtime.service_runner import (
    ServiceManifestError,
    _wire_dependency_tunnels,
    validate_service_manifest,
)
from nexus.scheduler.dag import _notify_dependents_after_promotion


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.service_records.clear()
    STATE.service_tunnels.clear()
    STATE.service_dep_grants.clear()
    STATE.service_dependents.clear()
    yield
    STATE.service_records.clear()
    STATE.service_tunnels.clear()
    STATE.service_dep_grants.clear()
    STATE.service_dependents.clear()


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def _base_manifest() -> dict:
    return {
        "runtime": "service",
        "image": "myapp/api",
        "expose_ports": [8080],
    }


def test_validate_accepts_depends_on():
    mf = _base_manifest()
    mf["depends_on"] = [
        {"service_id": "svc-db", "alias": "db"},
        {"service_id": "svc_cache"},  # alias defaults to service_id (must be alnum/_)
    ]
    spec = validate_service_manifest(mf)
    assert spec["depends_on"] == [
        {"service_id": "svc-db", "alias": "DB"},
        {"service_id": "svc_cache", "alias": "SVC_CACHE"},
    ]


def test_validate_rejects_non_list_depends_on():
    mf = _base_manifest()
    mf["depends_on"] = "svc-db"
    with pytest.raises(ServiceManifestError, match="depends_on must be a list"):
        validate_service_manifest(mf)


def test_validate_rejects_missing_service_id():
    mf = _base_manifest()
    mf["depends_on"] = [{"alias": "DB"}]
    with pytest.raises(ServiceManifestError, match="missing service_id"):
        validate_service_manifest(mf)


def test_validate_rejects_non_alphanumeric_alias():
    mf = _base_manifest()
    mf["depends_on"] = [{"service_id": "svc-db", "alias": "MY-DB!"}]
    with pytest.raises(ServiceManifestError, match="alias"):
        validate_service_manifest(mf)


def test_validate_empty_depends_on_returns_empty_list():
    spec = validate_service_manifest(_base_manifest())
    assert spec["depends_on"] == []


# ---------------------------------------------------------------------------
# _peer_owns_service honours dep grants
# ---------------------------------------------------------------------------

def test_peer_owns_service_unknown_returns_false():
    assert _peer_owns_service("any-peer", "no-such-task") is False


def test_peer_owns_service_master_ip_match():
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "master_ip": "10.0.0.1",
    }
    assert _peer_owns_service("10.0.0.1", "svc-1") is True
    assert _peer_owns_service("10.0.0.2", "svc-1") is False


def test_peer_owns_service_dep_grant_match():
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "master_ip": "10.0.0.1",
    }
    STATE.service_dep_grants["svc-1"] = {"worker-A", "worker-B"}
    assert _peer_owns_service("worker-A", "svc-1") is True
    assert _peer_owns_service("worker-B", "svc-1") is True
    assert _peer_owns_service("worker-C", "svc-1") is False


# ---------------------------------------------------------------------------
# ensure_dependency_tunnel
# ---------------------------------------------------------------------------

def test_ensure_dependency_tunnel_creates_synthetic_record_and_listener():
    """First call opens a fresh listener and seeds service_records."""
    with patch(
        "nexus.networking.tunnel.ensure_local_listener",
        AsyncMock(return_value=15432),
    ) as ell:
        port = _run(ensure_dependency_tunnel("svc-db", "worker-A", 5432))

    assert port == 15432
    ell.assert_awaited_once_with("svc-db", "worker-A")
    rec = STATE.service_records["svc-db"]
    assert rec["expose_ports"] == [5432]
    assert rec["worker_id"] == "worker-A"


def test_ensure_dependency_tunnel_returns_cached_port_for_same_primary():
    STATE.service_records["svc-db"] = {
        "task_id": "svc-db",
        "expose_ports": [5432],
        "master_ip": "",
        "worker_id": "worker-A",
    }
    STATE.service_tunnels["svc-db"] = {
        "server": MagicMock(),
        "port": 9999,
        "peer_id": "worker-A",
        "streams": {},
    }
    with patch(
        "nexus.networking.tunnel.ensure_local_listener", AsyncMock()
    ) as ell, patch(
        "nexus.networking.tunnel.reroute_tunnel", AsyncMock()
    ) as rr:
        port = _run(ensure_dependency_tunnel("svc-db", "worker-A", 5432))

    assert port == 9999
    ell.assert_not_called()
    rr.assert_not_called()


def test_ensure_dependency_tunnel_reroutes_when_primary_changed():
    STATE.service_records["svc-db"] = {
        "task_id": "svc-db",
        "expose_ports": [5432],
        "master_ip": "",
        "worker_id": "worker-A",
    }
    STATE.service_tunnels["svc-db"] = {
        "server": MagicMock(),
        "port": 9999,
        "peer_id": "worker-A",
        "streams": {},
    }
    with patch(
        "nexus.networking.tunnel.ensure_local_listener", AsyncMock()
    ) as ell, patch(
        "nexus.networking.tunnel.reroute_tunnel", AsyncMock(return_value=0)
    ) as rr:
        port = _run(ensure_dependency_tunnel("svc-db", "worker-B", 5432))

    assert port == 9999
    ell.assert_not_called()
    rr.assert_awaited_once_with("svc-db", "worker-B")


# ---------------------------------------------------------------------------
# _wire_dependency_tunnels (env injection)
# ---------------------------------------------------------------------------

def test_wire_dependency_tunnels_injects_service_env_vars():
    deps = [
        {"service_id": "svc-db", "alias": "DB"},
        {"service_id": "svc-cache", "alias": "CACHE"},
    ]
    container_env = {
        "NEXUS_DEP_DB_PRIMARY": "worker-A",
        "NEXUS_DEP_DB_PORT": "5432",
        "NEXUS_DEP_CACHE_PRIMARY": "worker-B",
        "NEXUS_DEP_CACHE_PORT": "6379",
    }

    async def fake_tunnel(dep_id, primary, port):
        return 15000 + port  # deterministic, distinct local ports

    with patch(
        "nexus.networking.tunnel.ensure_dependency_tunnel",
        AsyncMock(side_effect=fake_tunnel),
    ):
        _run(_wire_dependency_tunnels("consumer", deps, container_env))

    assert container_env["NEXUS_SERVICE_DB_HOST"] == "127.0.0.1"
    assert container_env["NEXUS_SERVICE_DB_PORT"] == "20432"
    assert container_env["NEXUS_SERVICE_CACHE_HOST"] == "127.0.0.1"
    assert container_env["NEXUS_SERVICE_CACHE_PORT"] == "21379"


def test_wire_dependency_tunnels_skips_dep_without_master_resolution():
    """A dep the master couldn't resolve at dispatch is skipped (warning logged)."""
    deps = [{"service_id": "svc-db", "alias": "DB"}]
    container_env: dict = {}  # no NEXUS_DEP_*

    with patch(
        "nexus.networking.tunnel.ensure_dependency_tunnel",
        AsyncMock(),
    ) as et:
        _run(_wire_dependency_tunnels("consumer", deps, container_env))

    et.assert_not_called()
    assert "NEXUS_SERVICE_DB_HOST" not in container_env


# ---------------------------------------------------------------------------
# Failover propagation: _notify_dependents_after_promotion
# ---------------------------------------------------------------------------

def test_notify_dependents_no_consumers_is_noop():
    rec = {"expose_ports": [5432]}
    with patch("nexus.networking.tunnel._send_to_peer", AsyncMock()) as snd:
        _run(_notify_dependents_after_promotion("svc-db", "worker-C", rec))
    snd.assert_not_called()


def test_notify_dependents_sends_changed_to_each_consumer_and_grant_to_new_primary():
    STATE.service_dependents["svc-db"] = {"api-1", "api-2"}
    STATE.service_records["api-1"] = {"worker_id": "worker-X"}
    STATE.service_records["api-2"] = {"worker_id": "worker-Y"}
    rec = {"expose_ports": [5432]}

    with patch(
        "nexus.networking.tunnel._send_to_peer", AsyncMock(return_value=True)
    ) as snd:
        _run(_notify_dependents_after_promotion("svc-db", "worker-C", rec))

    sent = [(c.args[0], c.args[1]) for c in snd.await_args_list]
    changed = [(t, f) for t, f in sent if f.get("type") == "service_dep_changed"]
    grants = [(t, f) for t, f in sent if f.get("type") == "service_dep_grant"]

    # One service_dep_changed to each consumer worker.
    consumer_targets = sorted(t for t, _ in changed)
    assert consumer_targets == ["worker-X", "worker-Y"]
    for _, frame in changed:
        assert frame["task_id"] == "svc-db"
        assert frame["primary"] == "worker-C"
        assert frame["port"] == 5432

    # One service_dep_grant pushed to the new primary listing both consumers.
    assert len(grants) == 1
    target, frame = grants[0]
    assert target == "worker-C"
    assert frame["task_id"] == "svc-db"
    assert sorted(frame["peers"]) == ["worker-X", "worker-Y"]


def test_notify_dependents_skips_consumers_without_worker():
    """A consumer task with no recorded worker is skipped silently."""
    STATE.service_dependents["svc-db"] = {"api-orphan"}
    STATE.service_records["api-orphan"] = {}  # no worker_id
    rec = {"expose_ports": [5432]}

    with patch(
        "nexus.networking.tunnel._send_to_peer", AsyncMock(return_value=True)
    ) as snd:
        _run(_notify_dependents_after_promotion("svc-db", "worker-C", rec))

    # No consumers means no grant push either.
    assert snd.await_count == 0


def test_notify_dependents_no_expose_ports_is_noop():
    """If we don't know the dep's container port we cannot reroute consumers."""
    STATE.service_dependents["svc-db"] = {"api-1"}
    STATE.service_records["api-1"] = {"worker_id": "worker-X"}
    rec = {"expose_ports": []}

    with patch(
        "nexus.networking.tunnel._send_to_peer", AsyncMock()
    ) as snd:
        _run(_notify_dependents_after_promotion("svc-db", "worker-C", rec))

    snd.assert_not_called()
