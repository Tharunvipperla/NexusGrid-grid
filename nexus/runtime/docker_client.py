"""Lazy Docker SDK client + per-profile container security options.

Extracted from node_modified.py:

* Docker module import + ``_docker_client`` singleton — lines 4-6, 267-286
* ``_get_docker_security_opts`` — lines 1458-1475

The Docker SDK is imported at module load but tolerated if missing — the
node may be run in native-only mode on hosts without Docker installed.
:func:`get_docker_client` raises ``RuntimeError`` only when Docker is
actively requested by a task.
"""

from __future__ import annotations

import logging

try:
    import docker as _docker_mod
except ImportError:
    _docker_mod = None  # type: ignore[assignment]

_log = logging.getLogger("nexus.runtime.docker_client")

_docker_client = None


def get_docker_client():
    """Return the shared Docker client, connecting on first use.

    Raises ``RuntimeError`` if the SDK is missing or the daemon is down.
    """
    global _docker_client
    if _docker_client is not None:
        return _docker_client
    if _docker_mod is None:
        raise RuntimeError(
            "Docker SDK is not installed. Install it with: pip install docker"
        )
    try:
        _docker_client = _docker_mod.from_env()
        _docker_client.ping()
        _log.info("Docker engine connected successfully.")
        return _docker_client
    except Exception as e:
        _docker_client = None
        raise RuntimeError(f"Docker engine is not running or not accessible: {e}")


def reset_docker_client() -> None:
    """Forget the cached client so the next call re-connects. Tests only."""
    global _docker_client
    _docker_client = None


def docker_security_opts(profile: str) -> dict:
    """Return ``docker run`` kwargs matching the given security profile.

    ``profile`` ∈ {``relaxed``, ``standard``, ``maximum``}. See
    :mod:`nexus.security.profiles` for the profile definitions themselves;
    this module only translates the profile name into Docker's option surface.
    """
    if profile == "relaxed":
        return {}
    opts: dict = {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": 512 if profile == "standard" else 256,
    }
    if profile == "maximum":
        opts["read_only"] = True
        opts["tmpfs"] = {
            "/tmp": "size=256m",
            "/var/tmp": "size=64m",
            "/root": "size=16m",
        }
        opts["user"] = "65534:65534"
    return opts


def _gpu_device_count(gpu) -> int | None:
    """Translate a manifest/run-spec ``gpu`` value into a Docker device count.

    Returns ``-1`` for "all GPUs", a positive int for a specific count, or
    ``None`` when no GPU is requested. Raises ``ValueError`` on a malformed
    value. Accepts ``"all"`` / ``True`` / ``N`` to request; ``None`` / ``0`` /
    ``""`` / ``False`` to decline.
    """
    if gpu is None or gpu is False:
        return None
    if gpu is True:
        return -1
    if isinstance(gpu, str):
        s = gpu.strip().lower()
        if s in ("", "0", "none", "false", "off", "no"):
            return None
        if s in ("all", "-1"):
            return -1
        if s.isdigit():
            n = int(s)
            return n if n >= 1 else None
        raise ValueError(f"invalid gpu value: {gpu!r}")
    if isinstance(gpu, int):
        return gpu if gpu >= 1 else None
    raise ValueError(f"invalid gpu value: {gpu!r}")


def _host_gpu_vendor(vendor: str | None) -> str | None:
    """Resolve the GPU vendor to target: the caller's override, else the detected
    host vendor. Local import avoids a load-time dependency on telemetry."""
    if vendor is not None:
        return vendor
    from nexus.telemetry.hardware import gpu_vendor
    return gpu_vendor()


# AMD/ROCm doesn't use ``--gpus``; it needs the kernel + render device nodes
# exposed plus the render/video groups. This is the documented ROCm-in-Docker
# recipe and is the v1 AMD passthrough (all GPUs — per-device pinning is future).
_AMD_DEVICES = ["/dev/kfd:/dev/kfd:rwm", "/dev/dri:/dev/dri:rwm"]
_AMD_GROUPS = ["render", "video"]
_AMD_CLI = ["--device", "/dev/kfd", "--device", "/dev/dri",
            "--group-add", "render", "--group-add", "video"]


def docker_gpu_opts(gpu, vendor: str | None = None) -> dict:
    """Return ``docker run`` (SDK) kwargs that expose the host GPU(s).

    ``gpu`` is the manifest/run-spec request: ``"all"`` / ``True`` / int ``N``
    asks for GPU(s); ``None`` / ``0`` / ``""`` asks for none. Returns ``{}`` when
    no GPU is requested, so the launch is unchanged for every existing (CPU) task.

    Vendor-aware (``vendor`` defaults to the detected host GPU vendor):
    **NVIDIA** uses Docker's ``device_requests`` (the SDK form of ``--gpus``);
    **AMD/ROCm** uses raw device mounts (``/dev/kfd`` + ``/dev/dri``) plus the
    render/video groups — a different mechanism entirely, not ``--gpus``.
    The **native** runtime needs none of this (a host subprocess sees the GPU),
    so this helper is only called on the Docker path. Raises ``ValueError`` on a
    malformed ``gpu`` value, and ``RuntimeError`` if an NVIDIA GPU is requested
    but the Docker SDK is absent.
    """
    count = _gpu_device_count(gpu)
    if count is None:
        return {}
    if _host_gpu_vendor(vendor) == "amd":
        return {"devices": list(_AMD_DEVICES), "group_add": list(_AMD_GROUPS)}
    # NVIDIA (default): device_requests is the SDK form of --gpus.
    if _docker_mod is None:
        raise RuntimeError("GPU requested but the Docker SDK is not installed")
    request = _docker_mod.types.DeviceRequest(count=count, capabilities=[["gpu"]])
    return {"device_requests": [request]}


def docker_gpu_cli_args(gpu, vendor: str | None = None) -> list[str]:
    """CLI equivalent of :func:`docker_gpu_opts` for runners that shell out to
    ``docker``/``podman`` (the replica runner). ``[]`` when no GPU is requested.

    NVIDIA → ``--gpus all`` / ``--gpus N``; AMD/ROCm → ``--device`` mounts +
    ``--group-add``. ``vendor`` defaults to the detected host GPU vendor.
    """
    count = _gpu_device_count(gpu)
    if count is None:
        return []
    if _host_gpu_vendor(vendor) == "amd":
        return list(_AMD_CLI)
    return ["--gpus", "all" if count == -1 else str(count)]


def docker_available() -> bool:
    """Return ``True`` when the Docker SDK module imported successfully."""
    return _docker_mod is not None


__all__ = [
    "get_docker_client",
    "reset_docker_client",
    "docker_security_opts",
    "docker_gpu_opts",
    "docker_gpu_cli_args",
    "docker_available",
]
