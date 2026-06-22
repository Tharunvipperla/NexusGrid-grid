"""Worker fitness scoring + support predicate.

Extracted from node_modified.py:

* ``worker_supports_task`` — lines 2555-2598
* ``worker_fit_score`` — lines 2601-2644

These are the decision primitives used by :mod:`nexus.scheduler.selection`
to pick the best worker for a task. Both are pure functions of the
``worker_info`` dict (as shipped over the wire from each worker) and the
``TaskRecord``. No side-effects, no I/O apart from the cached manifest
read.
"""

from __future__ import annotations

import time

from nexus.core import LOCAL_SETTINGS, normalize_list_field
from nexus.runtime import task_required_caps
from nexus.scheduler.manifest import read_task_manifest
from nexus.storage import TaskRecord


def worker_unsupported_reason(worker_info: dict, task: TaskRecord) -> str | None:
    """The first hard requirement of *task* the worker fails, or ``None``.

    This is the single source of truth for the support gates — dispatch
    uses it through :func:`worker_supports_task`, and the queue-insight
    endpoint reports the returned reason verbatim so "why is this queued?"
    can never drift from what the scheduler actually does.

    Gates:
    * ``allow_cross_region_workers`` — relay-only workers are excluded when
      disabled.
    * ``supported_images`` — the image in the task manifest must match one
      of the patterns the worker advertises.
    * ``require_gpu`` / ``required_tags`` / ``preferred_region``.
    """
    stats = worker_info.get("stats", {})
    connection_type = str(stats.get("connection_type", "lan"))
    manifest = read_task_manifest(cache_key=task.id)
    # Per-task override of the dispatcher's own cross-region preference;
    # absent = the node setting. This is the requester's choice for its
    # own task, so both directions are allowed.
    _xr = manifest.get("allow_cross_region")
    allow_cross = (
        bool(_xr)
        if _xr is not None
        else bool(LOCAL_SETTINGS.get("allow_cross_region_workers", True))
    )
    if connection_type == "relay" and not allow_cross:
        return "relay-only worker, and this dispatch excludes cross-region workers"
    caps = stats.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    req = task_required_caps(task)

    task_runtime = manifest.get("runtime", "docker")
    if task_runtime == "docker":
        required_image = str(manifest.get("image", "")).strip()
        supported_images = normalize_list_field(caps.get("supported_images", []))
        if (
            required_image
            and supported_images
            and not any(
                required_image == p
                or (str(p).endswith("*") and required_image.startswith(str(p)[:-1]))
                for p in supported_images
            )
        ):
            return f"doesn't allow the docker image '{required_image}'"
    elif task_runtime == "service":
        # Service tasks require explicit capability advertisement.
        if not bool(caps.get("service_runtime", False)):
            return "can't host service-runtime tasks"

    if req["require_gpu"] and not bool(caps.get("gpu", False)):
        return "has no GPU (task requires one)"
    worker_tags = set(normalize_list_field(caps.get("tags", [])))
    required_tags = set(req["required_tags"])
    if required_tags and not required_tags.issubset(worker_tags):
        missing = ", ".join(sorted(required_tags - worker_tags))
        return f"missing required tag(s): {missing}"
    preferred_region = req["preferred_region"]
    if (
        preferred_region
        and str(caps.get("region", ""))
        and str(caps.get("region", "")) != preferred_region
    ):
        return f"in region '{caps.get('region')}', task wants '{preferred_region}'"
    return None


def worker_supports_task(worker_info: dict, task: TaskRecord) -> bool:
    """``True`` if the worker meets every hard requirement of *task*."""
    return worker_unsupported_reason(worker_info, task) is None


def worker_fit_score(
    worker_info: dict,
    req_ram: int,
    req_cpu: int,
    require_gpu: bool = False,
) -> tuple | None:
    """Return a comparable tuple score, or ``None`` if the worker is unusable.

    Higher tuples win when sorted descending. Dimensions, in order:

    1. GPU alignment — GPU task wants a GPU worker.
    2. Network tier — LAN > relay.
    3. Bench tier — non-zero benchmark score, blunted to 5-pt buckets.
    4. RAM tier — does the worker have at least ``req_ram`` dispatchable?
    5. RAM headroom above the request.
    6. Free VRAM headroom.
    7. CPU headroom.
    8. Raw free RAM.
    9. Inverse active-task count (fewer active tasks wins).
    """
    stats = worker_info.get("stats", {})
    if time.time() - float(worker_info.get("last_seen", 0) or 0) > 12:
        return None
    free_ram = int(stats.get("free_ram", 0) or 0)
    dispatch_cap = int(stats.get("dispatch_ram_cap_mb", free_ram) or free_ram)
    if dispatch_cap < 128:
        return None
    cpu = float(stats.get("cpu", 100) or 100)
    active_task_count = int(stats.get("active_task_count", 0) or 0)
    ram_tier = 1 if dispatch_cap >= req_ram else 0
    caps = stats.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    worker_has_gpu = bool(caps.get("gpu", False))
    gpu_free_vram = int(stats.get("dispatch_gpu_cap_mb", 0) or 0)
    if require_gpu and worker_has_gpu:
        gpu_score = 2
    elif require_gpu:
        gpu_score = 0
    elif worker_has_gpu:
        gpu_score = 0
    else:
        gpu_score = 1
    connection_type = str(stats.get("connection_type", "lan"))
    network_tier = 2 if connection_type == "lan" else 1
    try:
        bench = float(stats.get("bench", 0.0) or 0.0)
    except (TypeError, ValueError):
        bench = 0.0
    bench_tier = int(max(0.0, bench) // 5)  # 5-point buckets
    return (
        gpu_score,
        network_tier,
        bench_tier,
        ram_tier,
        dispatch_cap - req_ram,
        gpu_free_vram,
        req_cpu - cpu,
        free_ram,
        -active_task_count,
    )


__all__ = ["worker_supports_task", "worker_unsupported_reason", "worker_fit_score"]
