"""GPU passthrough: the manifest ``gpu`` value → Docker device-request mapping.

The count translation is pure and runs without Docker installed; the
``device_requests`` construction is asserted only when the Docker SDK is present.
A non-request must yield ``{}`` so every existing CPU task launches unchanged.
"""

import pytest

from nexus.runtime.docker_client import _gpu_device_count, docker_gpu_opts


@pytest.mark.parametrize(
    "value, expected",
    [
        ("all", -1),
        ("ALL", -1),
        (True, -1),
        ("-1", -1),
        (1, 1),
        (4, 4),
        ("2", 2),
    ],
)
def test_gpu_count_requests(value, expected):
    assert _gpu_device_count(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, False, 0, "", "0", "none", "off", -3],
)
def test_gpu_count_declines(value):
    assert _gpu_device_count(value) is None


@pytest.mark.parametrize("value", ["banana", "1.5", "gpu", [], {}])
def test_gpu_count_rejects_garbage(value):
    with pytest.raises(ValueError):
        _gpu_device_count(value)


def test_gpu_opts_empty_when_not_requested():
    # Backward compatibility: no GPU request => no extra docker kwargs at all.
    assert docker_gpu_opts(None) == {}
    assert docker_gpu_opts(0) == {}
    assert docker_gpu_opts("") == {}


def test_gpu_opts_builds_device_request_when_sdk_present():
    docker = pytest.importorskip("docker")
    opts = docker_gpu_opts("all")
    assert list(opts.keys()) == ["device_requests"]
    (req,) = opts["device_requests"]
    assert isinstance(req, docker.types.DeviceRequest)
    assert req.count == -1
    assert req.capabilities == [["gpu"]]

    (req2,) = docker_gpu_opts(2)["device_requests"]
    assert req2.count == 2


# --- service manifest gpu validation ---------------------------------------

from nexus.runtime.service_runner import ServiceManifestError, validate_service_manifest


def _svc_manifest(**extra):
    base = {"runtime": "service", "image": "ollama/ollama", "expose_ports": [11434]}
    base.update(extra)
    return base


def test_service_manifest_keeps_gpu_when_present(monkeypatch):
    monkeypatch.setattr("nexus.telemetry.hardware.detect_gpu", lambda: True)
    spec = validate_service_manifest(_svc_manifest(gpu="all"))
    assert spec["gpu"] == "all"


def test_service_manifest_gpu_absent_is_none(monkeypatch):
    monkeypatch.setattr("nexus.telemetry.hardware.detect_gpu", lambda: True)
    spec = validate_service_manifest(_svc_manifest())
    assert spec["gpu"] is None


def test_service_manifest_rejects_gpu_without_hardware(monkeypatch):
    monkeypatch.setattr("nexus.telemetry.hardware.detect_gpu", lambda: False)
    with pytest.raises(ServiceManifestError):
        validate_service_manifest(_svc_manifest(gpu="all"))


def test_service_manifest_rejects_garbage_gpu(monkeypatch):
    monkeypatch.setattr("nexus.telemetry.hardware.detect_gpu", lambda: True)
    with pytest.raises(ServiceManifestError):
        validate_service_manifest(_svc_manifest(gpu="banana"))


# --- hosted-service run-spec normalization ---------------------------------

from nexus.core.config import _normalize_run_spec


@pytest.mark.parametrize("value, expected", [("all", "all"), ("2", "2"), (1, 1), (True, True)])
def test_run_spec_keeps_gpu_request(value, expected):
    spec = _normalize_run_spec({"image": "ollama/ollama", "gpu": value})
    assert spec.get("gpu") == expected


@pytest.mark.parametrize("value", [None, "", "0", 0, False, "banana"])
def test_run_spec_drops_non_gpu(value):
    spec = _normalize_run_spec({"image": "ollama/ollama", "gpu": value})
    assert "gpu" not in spec


# --- replica runner CLI argv ----------------------------------------------

from nexus.runtime.replica_runner import _container_argv


def _ctx(gpu):
    return {
        "spec": {"image": "ollama/ollama", "cmd": "", "env": [], "ports": [], "gpu": gpu},
        "host_ports": [], "allow_outbound": False, "mem_mb": 512, "cpus": 1,
    }


def test_container_argv_adds_gpus_all():
    argv = _container_argv("docker", _ctx("all"))
    assert "--gpus" in argv
    assert argv[argv.index("--gpus") + 1] == "all"


def test_container_argv_adds_gpus_count():
    argv = _container_argv("docker", _ctx(2))
    assert argv[argv.index("--gpus") + 1] == "2"


def test_container_argv_no_gpus_when_unset():
    assert "--gpus" not in _container_argv("docker", _ctx(None))


# --- host GPU count (drives the Services toggle vs slider) -----------------

import types


def test_gpu_count_zero_without_gpu(monkeypatch):
    import nexus.telemetry.hardware as hw
    monkeypatch.setattr(hw, "_gpu_count", None)
    monkeypatch.setattr(hw, "detect_gpu", lambda: False)
    assert hw.gpu_count() == 0


def test_gpu_count_counts_nvidia_devices(monkeypatch):
    import nexus.telemetry.hardware as hw
    monkeypatch.setattr(hw, "_gpu_count", None)
    monkeypatch.setattr(hw, "_gpu_vendor", "nvidia")
    monkeypatch.setattr(hw, "detect_gpu", lambda: True)
    fake = types.SimpleNamespace(returncode=0, stdout="GPU 0: A\nGPU 1: B\n")
    monkeypatch.setattr(hw.subprocess, "run", lambda *a, **k: fake)
    assert hw.gpu_count() == 2


def test_gpu_count_falls_back_to_one_on_probe_failure(monkeypatch):
    import nexus.telemetry.hardware as hw
    monkeypatch.setattr(hw, "_gpu_count", None)
    monkeypatch.setattr(hw, "_gpu_vendor", "nvidia")
    monkeypatch.setattr(hw, "detect_gpu", lambda: True)
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(hw.subprocess, "run", boom)
    assert hw.gpu_count() == 1
