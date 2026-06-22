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
