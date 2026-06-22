"""DAG #2 — "one step per node" anti-affinity in select_task_for_worker."""

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


def _task(task_id: str, *, parent_id: str = "", one_step: bool = False) -> TaskRecord:
    env: dict = {}
    if one_step:
        env["NEXUS_META_ONE_STEP_PER_NODE"] = True
    return TaskRecord(
        id=task_id, parent_id=parent_id, env_vars=json.dumps(env),
        payload=b"", status="queued", worker="",
    )


def test_busy_worker_excluded_when_one_step_per_node():
    workers = {"w1": _worker(), "w2": _worker()}
    task = _task("wf_step2", parent_id="wf", one_step=True)
    # w1 is already running a sibling step of workflow "wf".
    busy = {"wf": {"w1"}}

    # w1 (busy) is refused this workflow's next step...
    assert select_task_for_worker("w1", [task], workers, {}, {}, busy) is None
    # ...but a free node w2 gets it.
    got = select_task_for_worker("w2", [task], workers, {}, {}, busy)
    assert got is not None and got.id == "wf_step2"


def test_no_antiaffinity_when_flag_off():
    workers = {"w1": _worker()}
    task = _task("wf_step2", parent_id="wf", one_step=False)
    busy = {"wf": {"w1"}}
    # Flag off → a busy worker may still take another step of the workflow.
    got = select_task_for_worker("w1", [task], workers, {}, {}, busy)
    assert got is not None and got.id == "wf_step2"


def test_antiaffinity_scoped_per_workflow():
    workers = {"w1": _worker()}
    task = _task("wfB_step1", parent_id="wfB", one_step=True)
    # w1 is busy with a *different* workflow (wfA) — wfB is unaffected.
    busy = {"wfA": {"w1"}}
    got = select_task_for_worker("w1", [task], workers, {}, {}, busy)
    assert got is not None and got.id == "wfB_step1"


def test_missing_busy_map_is_safe():
    workers = {"w1": _worker()}
    task = _task("wf_s1", parent_id="wf", one_step=True)
    # No workflow_busy passed (None) → behaves as if nothing is busy.
    got = select_task_for_worker("w1", [task], workers, {}, {})
    assert got is not None and got.id == "wf_s1"
