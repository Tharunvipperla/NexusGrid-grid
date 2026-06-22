"""Service-runtime tests (Wave 4 Step 9a / item 3.1)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core import STATE
from nexus.runtime import service_runner
from nexus.runtime.service_runner import (
    ServiceManifestError,
    is_service_manifest,
    start_service,
    stop_service,
    validate_service_manifest,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_service_state():
    STATE.service_records.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_tunnels.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.running_task_containers.clear()
    yield
    STATE.service_records.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_tunnels.clear()
    STATE.service_watchdog_tasks.clear()
    STATE.running_task_containers.clear()


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def test_is_service_manifest():
    assert is_service_manifest({"runtime": "service"}) is True
    assert is_service_manifest({"runtime": "docker"}) is False
    assert is_service_manifest({"runtime": "SERVICE"}) is True  # case-insensitive
    assert is_service_manifest({}) is False


def test_validate_service_manifest_minimum():
    spec = validate_service_manifest(
        {"runtime": "service", "image": "redis:7", "expose_ports": [6379]}
    )
    assert spec["image"] == "redis:7"
    assert spec["expose_ports"] == [6379]
    assert spec["duration_sec"] == 3600  # default
    assert spec["idle_timeout_sec"] == 600  # default
    assert spec["service_kind"] == "tcp"
    assert spec["ram_limit_mb"] >= 64


def test_validate_service_manifest_full():
    spec = validate_service_manifest(
        {
            "runtime": "service",
            "image": "postgres:16",
            "expose_ports": [5432, 9090],
            "duration_sec": 120,
            "idle_timeout_sec": 30,
            "service_kind": "postgres",
            "ram_limit_mb": 1024,
            "cpu_limit_pct": 50,
        }
    )
    assert spec["expose_ports"] == [5432, 9090]
    assert spec["duration_sec"] == 120
    assert spec["idle_timeout_sec"] == 30
    assert spec["service_kind"] == "postgres"
    assert spec["ram_limit_mb"] == 1024
    assert spec["cpu_limit_pct"] == 50


def test_validate_rejects_non_service_runtime():
    with pytest.raises(ServiceManifestError, match="runtime must be 'service'"):
        validate_service_manifest({"runtime": "docker", "image": "redis:7"})


def test_validate_rejects_missing_image():
    with pytest.raises(ServiceManifestError, match="image is required"):
        validate_service_manifest({"runtime": "service", "expose_ports": [6379]})


def test_validate_rejects_empty_ports():
    with pytest.raises(ServiceManifestError, match="expose_ports"):
        validate_service_manifest(
            {"runtime": "service", "image": "redis:7", "expose_ports": []}
        )


def test_validate_rejects_bad_port_value():
    with pytest.raises(ServiceManifestError, match="expose_ports"):
        validate_service_manifest(
            {"runtime": "service", "image": "redis:7", "expose_ports": [70000]}
        )


def test_validate_rejects_non_int_port():
    with pytest.raises(ServiceManifestError, match="not an int"):
        validate_service_manifest(
            {"runtime": "service", "image": "redis:7", "expose_ports": ["abc"]}
        )


def test_validate_rejects_zero_duration():
    with pytest.raises(ServiceManifestError, match="duration_sec"):
        validate_service_manifest(
            {
                "runtime": "service",
                "image": "redis:7",
                "expose_ports": [6379],
                "duration_sec": 0,
            }
        )


def test_validate_idle_timeout_zero_is_allowed():
    spec = validate_service_manifest(
        {
            "runtime": "service",
            "image": "redis:7",
            "expose_ports": [6379],
            "idle_timeout_sec": 0,
        }
    )
    assert spec["idle_timeout_sec"] == 0


# ---------------------------------------------------------------------------
# start_service / stop_service (mocked Docker)
# ---------------------------------------------------------------------------

def _fake_docker_client(host_ports: dict[int, int]):
    """Build a Docker client that returns a container with the given ports."""

    def _make_container():
        container = MagicMock()
        container.attrs = {
            "NetworkSettings": {
                "Ports": {
                    f"{cport}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(hport)}]
                    for cport, hport in host_ports.items()
                }
            }
        }
        container.reload = MagicMock()
        container.stop = MagicMock()
        container.remove = MagicMock()
        return container

    client = SimpleNamespace()
    client.images = SimpleNamespace()
    client.images.get = MagicMock(return_value=MagicMock())
    client.images.pull = MagicMock()
    client.containers = SimpleNamespace()
    client.containers.run = MagicMock(side_effect=lambda **kw: _make_container())
    return client


def test_start_service_records_state_and_port_map():
    client = _fake_docker_client({6379: 49152})
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "duration_sec": 3600,
        "idle_timeout_sec": 0,
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            record = await start_service("svc-001", manifest)
        assert record["task_id"] == "svc-001"
        assert record["expose_ports"] == [6379]
        assert STATE.service_port_mappings["svc-001"] == {6379: 49152}
        assert STATE.service_records["svc-001"]["status"] == "running"
        assert "svc-001" in STATE.running_task_containers
        assert "svc-001" in STATE.service_watchdog_tasks
        await stop_service("svc-001", reason="manual")

    _run(_scenario())


def test_start_service_passes_correct_run_kwargs():
    client = _fake_docker_client({5432: 49200})
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)
        return MagicMock(
            attrs={
                "NetworkSettings": {
                    "Ports": {"5432/tcp": [{"HostPort": "49200"}]}
                }
            },
            reload=MagicMock(),
            stop=MagicMock(),
            remove=MagicMock(),
        )

    client.containers.run = MagicMock(side_effect=_capture)
    manifest = {
        "runtime": "service",
        "image": "postgres:16",
        "expose_ports": [5432],
        "ram_limit_mb": 1024,
        "cpu_limit_pct": 50,
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            await start_service("svc-pg", manifest, env={"POSTGRES_PASSWORD": "x"})
        assert captured["image"] == "postgres:16"
        assert captured["detach"] is True
        assert captured["ports"] == {"5432/tcp": None}
        assert captured["mem_limit"] == "1024m"
        assert captured["cpu_quota"] == 50000
        assert captured["environment"]["POSTGRES_PASSWORD"] == "x"
        await stop_service("svc-pg", reason="manual")

    _run(_scenario())


def test_start_service_fails_when_ports_not_bound():
    client = _fake_docker_client({})  # empty -> no host port assigned
    # Force the loop to give up by clearing host ports outright
    container = MagicMock(
        attrs={"NetworkSettings": {"Ports": {"6379/tcp": []}}},
        reload=MagicMock(),
        stop=MagicMock(),
        remove=MagicMock(),
    )
    client.containers.run = MagicMock(return_value=container)

    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
    }
    with patch.object(service_runner, "get_docker_client", return_value=client):
        with pytest.raises(RuntimeError, match="did not bind every requested port"):
            _run(start_service("svc-fail", manifest))

    # State was cleaned up after the failure.
    assert "svc-fail" not in STATE.service_records
    assert "svc-fail" not in STATE.running_task_containers


def test_stop_service_cancels_watchdog_and_clears_state():
    client = _fake_docker_client({6379: 49152})
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "duration_sec": 3600,
        "idle_timeout_sec": 0,
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            await start_service("svc-stop", manifest)
        return await stop_service("svc-stop", reason="manual")

    result = _run(_scenario())
    assert result is True
    assert "svc-stop" not in STATE.service_port_mappings
    assert "svc-stop" not in STATE.running_task_containers
    assert STATE.service_records["svc-stop"]["status"] == "stopped"
    assert STATE.service_records["svc-stop"]["stop_reason"] == "manual"


def test_stop_service_returns_false_for_unknown_task():
    assert _run(stop_service("svc-nonexistent", reason="manual")) is False


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

def test_watchdog_stops_on_duration_limit():
    """A service past its expires_at gets stopped on the next tick."""
    client = _fake_docker_client({6379: 49152})
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "duration_sec": 3600,
        "idle_timeout_sec": 0,
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client), \
             patch.object(service_runner, "_WATCHDOG_INTERVAL_SEC", 0.01):
            await start_service("svc-dur", manifest)
            # Simulate the deadline already having passed.
            async with STATE.service_lock:
                STATE.service_records["svc-dur"]["expires_at"] = time.time() - 1
            # Wait for the watchdog to tick; the original watchdog was
            # created inside this patch, so it sleeps 0.01s per iteration.
            for _ in range(50):
                await asyncio.sleep(0.05)
                async with STATE.service_lock:
                    rec = STATE.service_records.get("svc-dur", {})
                    if rec.get("stop_reason"):
                        return rec["stop_reason"]
            return None

    reason = _run(_scenario())
    assert reason == "duration_limit"


# ---------------------------------------------------------------------------
# Wave 8.1 — environment-field validation + injection
# ---------------------------------------------------------------------------

def test_validate_accepts_environment_dict():
    spec = validate_service_manifest(
        {
            "runtime": "service",
            "image": "redis:7",
            "expose_ports": [6379],
            "environment": {"FOO": "bar", "BAZ": "qux"},
        }
    )
    assert spec["environment"] == {"FOO": "bar", "BAZ": "qux"}


def test_validate_environment_default_is_empty_dict():
    spec = validate_service_manifest(
        {"runtime": "service", "image": "redis:7", "expose_ports": [6379]}
    )
    assert spec["environment"] == {}


def test_validate_rejects_non_dict_environment():
    with pytest.raises(ServiceManifestError, match="environment must be a dict"):
        validate_service_manifest(
            {
                "runtime": "service",
                "image": "redis:7",
                "expose_ports": [6379],
                "environment": [("FOO", "bar")],
            }
        )


def test_validate_rejects_reserved_environment_prefix():
    for bad_key in ("NEXUS_FOO", "_NEXUS_BAR"):
        with pytest.raises(ServiceManifestError, match="reserved prefix"):
            validate_service_manifest(
                {
                    "runtime": "service",
                    "image": "redis:7",
                    "expose_ports": [6379],
                    "environment": {bad_key: "x"},
                }
            )


def test_validate_rejects_environment_too_many_entries():
    big = {f"K{i}": "v" for i in range(65)}
    with pytest.raises(ServiceManifestError, match="at most 64 entries"):
        validate_service_manifest(
            {
                "runtime": "service",
                "image": "redis:7",
                "expose_ports": [6379],
                "environment": big,
            }
        )


def test_validate_rejects_environment_payload_too_large():
    huge_value = "x" * (16 * 1024 + 1)
    with pytest.raises(ServiceManifestError, match="exceeds 16 KB"):
        validate_service_manifest(
            {
                "runtime": "service",
                "image": "redis:7",
                "expose_ports": [6379],
                "environment": {"BIG": huge_value},
            }
        )


def test_start_service_passes_manifest_environment_to_container():
    client = _fake_docker_client({6379: 49152})
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)
        return MagicMock(
            attrs={"NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": "49152"}]}}},
            reload=MagicMock(),
            stop=MagicMock(),
            remove=MagicMock(),
        )

    client.containers.run = MagicMock(side_effect=_capture)
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "environment": {"DEMO": "v1", "MODE": "prod"},
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            await start_service("svc-env", manifest)
        # Manifest env present.
        assert captured["environment"]["DEMO"] == "v1"
        assert captured["environment"]["MODE"] == "prod"
        await stop_service("svc-env", reason="manual")

    _run(_scenario())


def test_start_service_caller_env_overrides_manifest_env():
    """Runtime-injected env (caller-supplied) wins over manifest env on collision."""
    client = _fake_docker_client({6379: 49152})
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)
        return MagicMock(
            attrs={"NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": "49152"}]}}},
            reload=MagicMock(),
            stop=MagicMock(),
            remove=MagicMock(),
        )

    client.containers.run = MagicMock(side_effect=_capture)
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
        "environment": {"FOO": "from_manifest"},
    }

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            await start_service("svc-clash", manifest, env={"FOO": "from_caller"})
        assert captured["environment"]["FOO"] == "from_caller"
        await stop_service("svc-clash", reason="manual")

    _run(_scenario())


# ---------------------------------------------------------------------------
# Wave 8.2 — service_friendly auto-default
# ---------------------------------------------------------------------------

def test_pick_service_profile_defaults_to_service_friendly_under_maximum():
    from nexus.runtime.service_runner import _pick_service_profile

    assert _pick_service_profile({"security_profile": "maximum"}) == "service_friendly"


def test_pick_service_profile_honors_explicit_override():
    from nexus.runtime.service_runner import _pick_service_profile

    out = _pick_service_profile(
        {"security_profile": "maximum", "service_security_profile": "standard"}
    )
    assert out == "standard"


def test_pick_service_profile_passes_through_non_maximum_globals():
    from nexus.runtime.service_runner import _pick_service_profile

    for global_profile in ("relaxed", "standard", "service_friendly"):
        assert (
            _pick_service_profile({"security_profile": global_profile})
            == global_profile
        )


def test_pick_service_profile_handles_missing_setting():
    from nexus.runtime.service_runner import _pick_service_profile

    # Empty settings dict -> defaults to "maximum" globally -> auto-picks
    # "service_friendly" for service tasks.
    assert _pick_service_profile({}) == "service_friendly"


def test_start_service_uses_service_friendly_under_default_profile():
    """Sanity check: a service task with no overrides gets writable-root opts."""
    from nexus.core import LOCAL_SETTINGS

    client = _fake_docker_client({6379: 49152})
    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)
        return MagicMock(
            attrs={"NetworkSettings": {"Ports": {"6379/tcp": [{"HostPort": "49152"}]}}},
            reload=MagicMock(),
            stop=MagicMock(),
            remove=MagicMock(),
        )

    client.containers.run = MagicMock(side_effect=_capture)
    manifest = {
        "runtime": "service",
        "image": "redis:7",
        "expose_ports": [6379],
    }

    prev_profile = LOCAL_SETTINGS.get("security_profile")
    prev_override = LOCAL_SETTINGS.get("service_security_profile")
    LOCAL_SETTINGS["security_profile"] = "maximum"
    LOCAL_SETTINGS.pop("service_security_profile", None)

    async def _scenario():
        with patch.object(service_runner, "get_docker_client", return_value=client):
            await start_service("svc-prof", manifest)
        # service_friendly does NOT set read_only or user; maximum does.
        assert "read_only" not in captured or captured.get("read_only") is not True
        assert "user" not in captured
        # cap_drop is still present (defense in depth).
        assert captured.get("cap_drop") == ["ALL"]
        await stop_service("svc-prof", reason="manual")

    try:
        _run(_scenario())
    finally:
        if prev_profile is None:
            LOCAL_SETTINGS.pop("security_profile", None)
        else:
            LOCAL_SETTINGS["security_profile"] = prev_profile
        if prev_override is not None:
            LOCAL_SETTINGS["service_security_profile"] = prev_override
