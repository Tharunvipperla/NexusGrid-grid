"""Wave 43.E — union targeting (nodes ∪ groups) + per-dispatch member block."""

from __future__ import annotations

import json
import time

from nexus.scheduler.selection import select_task_for_worker
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


def _task(
    task_id: str,
    *,
    preferred_workers: list[str] | None = None,
    target_groups: list[str] | None = None,
    blocked_members: list[str] | None = None,
) -> TaskRecord:
    env: dict = {}
    if preferred_workers:
        env["NEXUS_META_PREFERRED_WORKERS"] = preferred_workers
    if target_groups:
        env["NEXUS_META_TARGET_GROUPS"] = target_groups
    if blocked_members:
        env["NEXUS_META_BLOCKED_MEMBERS"] = blocked_members
    return TaskRecord(
        id=task_id, env_vars=json.dumps(env), payload=b"", status="queued", worker=""
    )


def test_union_of_node_and_group_targets():
    # The scheduler offers a task to the single best worker, so to test that
    # both a node-target and a group-member are *eligible* we present each as
    # the only candidate in turn (avoids an equal-fitness tie deciding it).
    task = _task("t1", preferred_workers=["w-node"], target_groups=["g1"])
    pool = {"g1": {"w-grp"}}

    # The manually-picked node is eligible (node side of the union)...
    got_node = select_task_for_worker("w-node", [task], {"w-node": _worker()}, {}, pool)
    assert got_node is not None and got_node.id == "t1"
    # ...and so is the group member (group side of the union).
    got_grp = select_task_for_worker("w-grp", [task], {"w-grp": _worker()}, {}, pool)
    assert got_grp is not None and got_grp.id == "t1"
    # A worker that's neither picked nor in the group is not eligible.
    assert select_task_for_worker("w-other", [task], {"w-other": _worker()}, {}, pool) is None


def test_blocked_member_excluded_from_group_pool():
    task = _task(
        "t2",
        preferred_workers=["w-node"],
        target_groups=["g1"],
        blocked_members=["w-grp"],
    )
    pool = {"g1": {"w-grp"}}

    # The blocked member can't get it even though it's in the group pool...
    assert select_task_for_worker("w-grp", [task], {"w-grp": _worker()}, {}, pool) is None
    # ...but the manually-picked node still can.
    got = select_task_for_worker("w-node", [task], {"w-node": _worker()}, {}, pool)
    assert got is not None and got.id == "t2"


def test_node_only_target_unchanged():
    workers = {"w-node": _worker(), "w-other": _worker()}
    task = _task("t3", preferred_workers=["w-node"])
    assert select_task_for_worker("w-node", [task], workers, {}, {}).id == "t3"
    assert select_task_for_worker("w-other", [task], workers, {}, {}) is None


def test_grid_wide_when_nothing_scoped():
    workers = {"w-any": _worker()}
    task = _task("t4")
    assert select_task_for_worker("w-any", [task], workers, {}, {}).id == "t4"
