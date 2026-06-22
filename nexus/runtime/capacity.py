"""Dispatch-capacity + allowed-image gate + required-capability helpers.

Extracted from node_modified.py:

* ``get_dispatch_capacity_mb`` — lines 2268-2273
* ``can_pull_more_tasks`` / ``can_pull_task_from_master`` — lines 2276-2297
* ``refresh_worker_task_leases`` — lines 2300-2323
* ``image_allowed`` — lines 2329-2344
* ``local_capabilities`` / ``task_required_caps`` — lines 2347-2367

These are the predicates the worker-client and scheduler loops use to
decide whether a task can even be accepted. Everything here is pure
read-only against :data:`nexus.core.LOCAL_SETTINGS`, live process state
(``psutil``), or the database — there is no mutation of in-flight tasks.
"""

from __future__ import annotations

import psutil
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, normalize_list_field
from nexus.storage import TaskRecord, get_session
from nexus.tasks.lease import refresh_task_lease
from nexus.tasks.metadata import parse_task_env
from nexus.telemetry.hardware import get_gpu_stats
from nexus.utils import now_epoch

# Per-worker rate-limit for lease refreshes; see refresh_worker_task_leases.
_LAST_LEASE_REFRESH_AT: dict[str, float] = {}


def get_dispatch_capacity_mb() -> tuple[int, int]:
    """Return ``(free_ram_mb, dispatchable_mb)`` for *this* host right now."""
    sys_ram = psutil.virtual_memory().total // (1024 * 1024)
    free_ram = psutil.virtual_memory().available // (1024 * 1024)
    abs_max = int(sys_ram * (LOCAL_SETTINGS["max_ram_pct"] / 100.0))
    dispatch_cap = min(abs_max, max(128, free_ram - 256))
    return free_ram, dispatch_cap


async def can_pull_more_tasks() -> bool:
    """Can we accept one additional task given current CPU/RAM load?"""
    from nexus.runtime.worker_state import get_local_worker_snapshot  # local: cycle

    snapshot = await get_local_worker_snapshot()
    free_ram = psutil.virtual_memory().available // (1024 * 1024)
    concurrency_limit = max(1, min(4, psutil.cpu_count(logical=False) or 1))
    return snapshot["active_task_count"] < concurrency_limit and free_ram >= 512


async def can_pull_task_from_master(master_ip: str) -> bool:
    """Worker-side: may we accept a new task from *master_ip* right now?

    Honors ``node_online``, per-master concurrency (``sharing_mode`` +
    ``max_serving_masters``), and the generic capacity gate.
    """
    from nexus.runtime.idle_detect import is_node_online_effective
    from nexus.runtime.worker_state import get_local_worker_snapshot  # local: cycle

    if not is_node_online_effective():
        return False
    snapshot = await get_local_worker_snapshot()
    if not await can_pull_more_tasks():
        return False
    active_masters = set(snapshot["serving_masters"])
    if master_ip in active_masters:
        return True
    allowed_masters = (
        1
        if LOCAL_SETTINGS["sharing_mode"] == "single"
        else max(1, int(LOCAL_SETTINGS["max_serving_masters"]))
    )
    return len(active_masters) < allowed_masters


async def refresh_worker_task_leases(worker_id: str) -> None:
    """Bump lease expiry on every ``processing`` task owned by *worker_id*.

    Rate-limited to once every 2 seconds per worker to avoid hammering
    SQLite during heartbeat bursts.
    """
    now = now_epoch()
    if now - float(_LAST_LEASE_REFRESH_AT.get(worker_id, 0) or 0) < 2.0:
        return
    _LAST_LEASE_REFRESH_AT[worker_id] = now
    async with get_session() as db:
        tasks = (
            (
                await db.execute(
                    select(TaskRecord).filter(
                        TaskRecord.worker == worker_id,
                        TaskRecord.status == "processing",
                    )
                )
            )
            .scalars()
            .all()
        )
        changed = False
        for task in tasks:
            refresh_task_lease(task)
            changed = True
        if changed:
            await db.commit()


def image_allowed(image: str) -> bool:
    """Does ``image`` match any pattern in ``LOCAL_SETTINGS['allowed_images']``?

    Patterns may end in ``*`` for prefix match, otherwise equality is
    required. Empty whitelist ⇒ nothing allowed.
    """
    image = str(image or "").strip()
    if not image:
        return False
    patterns = LOCAL_SETTINGS.get("allowed_images", []) or []
    if not patterns:
        return False
    for pattern in patterns:
        p = str(pattern).strip()
        if not p:
            continue
        if p.endswith("*") and image.startswith(p[:-1]):
            return True
        if image == p:
            return True
    return False


def _foreign_storage_capability_active() -> bool:
    from nexus.runtime.foreign_storage_quota import (
        effective_free_gb,
        is_effectively_accepting,
    )

    return bool(is_effectively_accepting() and effective_free_gb() > 0.0)


def _foreign_storage_advertised_free_gb() -> float:
    from nexus.runtime.foreign_storage_quota import (
        effective_free_gb,
        is_effectively_accepting,
    )

    if not is_effectively_accepting():
        return 0.0
    return round(effective_free_gb(), 3)


def local_capabilities() -> dict:
    """The capability dict this node advertises to masters (gpu/tags/region)."""
    caps: dict = {
        "gpu": bool(LOCAL_SETTINGS.get("node_gpu", False)),
        "region": str(LOCAL_SETTINGS.get("node_region", "local")),
        "tags": normalize_list_field(LOCAL_SETTINGS.get("node_tags", [])),
        "supported_images": normalize_list_field(
            LOCAL_SETTINGS.get("allowed_images", [])
        ),
        # This worker can host long-running service tasks.
        # The fitness check rejects service-runtime tasks for workers that
        # don't advertise this bit, so rolling deploys are safe.
        "service_runtime": True,
        # Pre-flight: this node can host foreign-storage deposits.
        # Made the bit dynamic — only True when the operator has not
        # opted out AND there's actually room for at least 1 GB. Depositors
        # filter on this before sending an offer.
        "foreign_storage": _foreign_storage_capability_active(),
        # This node accepts ``action: "cloud"`` eviction responses
        # and can stream the encrypted bundle to the depositor's bucket.
        # Old hosts (no v2 bit) keep the classic download/forward/let_go set.
        "foreign_storage_v2": True,
        # Advertised free GB so depositors can rank/pick hosts
        # without a follow-up RTT. Drops to 0 when the operator opts out.
        "foreign_storage_free_gb": _foreign_storage_advertised_free_gb(),
    }
    if caps["gpu"]:
        caps["gpu_stats"] = get_gpu_stats()
    return caps


def task_required_caps(task: TaskRecord) -> dict:
    """Inverse of :func:`local_capabilities` — what *this task* needs."""
    env = parse_task_env(task)
    return {
        "require_gpu": bool(env.get("NEXUS_META_REQUIRE_GPU", False)),
        "required_tags": normalize_list_field(env.get("NEXUS_META_REQUIRED_TAGS", [])),
        "preferred_region": str(env.get("NEXUS_META_PREFERRED_REGION", "")).strip(),
    }


__all__ = [
    "get_dispatch_capacity_mb",
    "can_pull_more_tasks",
    "can_pull_task_from_master",
    "refresh_worker_task_leases",
    "image_allowed",
    "local_capabilities",
    "task_required_caps",
]
