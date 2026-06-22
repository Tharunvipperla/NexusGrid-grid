"""Wave 43 — group-scoped compute: scheduler pool filter."""

from __future__ import annotations

import json
import time

from nexus.scheduler.selection import select_task_for_worker, select_top_n_workers
from nexus.storage import TaskRecord


def _worker(free_ram: int = 4096) -> dict:
    return {
        "last_seen": time.time(),
        "stats": {
            "free_ram": free_ram,
            "dispatch_ram_cap_mb": free_ram,
            "cpu": 10,
            "connection_type": "lan",
            "capabilities": {},
        },
    }


def _task(task_id: str, target_groups: list[str] | None = None) -> TaskRecord:
    env: dict = {}
    if target_groups:
        env["NEXUS_META_TARGET_GROUPS"] = target_groups
    return TaskRecord(
        id=task_id,
        env_vars=json.dumps(env),
        payload=b"",
        status="queued",
        worker="",
    )


def test_group_scoped_task_only_offers_to_pool_member():
    workers = {"w-in": _worker(), "w-out": _worker()}
    task = _task("t1", target_groups=["g1"])
    pool = {"g1": {"w-in"}}

    # The in-pool worker gets it...
    got = select_task_for_worker("w-in", [task], workers, {}, pool)
    assert got is not None and got.id == "t1"

    # ...the out-of-pool worker does not.
    none = select_task_for_worker("w-out", [task], workers, {}, pool)
    assert none is None


def test_unscoped_task_is_grid_wide():
    workers = {"w-out": _worker()}
    task = _task("t2")  # no target_groups
    # A worker in no group still gets an unscoped task.
    got = select_task_for_worker("w-out", [task], workers, {}, {"g1": {"w-in"}})
    assert got is not None and got.id == "t2"


def test_empty_pool_for_scoped_task_offers_to_nobody():
    workers = {"w-out": _worker()}
    task = _task("t3", target_groups=["g1"])
    # Group exists in pool but has no eligible workers -> nobody runs it.
    got = select_task_for_worker("w-out", [task], workers, {}, {"g1": set()})
    assert got is None


def test_top_n_respects_group_scope():
    workers = {"w-in": _worker(), "w-out": _worker()}
    task = _task("t4", target_groups=["g1"])
    pool = {"g1": {"w-in"}}
    chosen = select_top_n_workers(task, 5, workers, group_pool=pool)
    assert chosen == ["w-in"]


def test_top_n_unscoped_considers_all():
    workers = {"w-a": _worker(8192), "w-b": _worker(2048)}
    task = _task("t5")
    chosen = select_top_n_workers(task, 5, workers)
    assert set(chosen) == {"w-a", "w-b"}
