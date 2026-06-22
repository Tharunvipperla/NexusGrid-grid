"""Reliability-aware scheduling: per-worker outcome tally + selector preference."""

from __future__ import annotations

import json
import time

import pytest

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.scheduler import reliability as R
from nexus.scheduler.selection import select_task_for_worker
from nexus.storage import TaskRecord


@pytest.fixture(autouse=True)
def _clean():
    STATE.worker_outcomes.clear()
    prev = LOCAL_SETTINGS.get("prefer_reliable_workers", False)
    LOCAL_SETTINGS["prefer_reliable_workers"] = False
    yield
    STATE.worker_outcomes.clear()
    LOCAL_SETTINGS["prefer_reliable_workers"] = prev


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


def _task(task_id: str = "t", prefer=None) -> TaskRecord:
    env: dict = {}
    if prefer is not None:
        env["NEXUS_META_PREFER_RELIABLE"] = bool(prefer)
    return TaskRecord(
        id=task_id, parent_id="", env_vars=json.dumps(env),
        payload=b"", status="queued", worker="",
    )


# ---- counter + scoring -----------------------------------------------------

def test_record_and_ratio_laplace_smoothing():
    # No history → neutral 0.5.
    assert R.reliability_ratio("ghost") == 0.5
    R.record_worker_outcome("w", ok=True)
    R.record_worker_outcome("w", ok=True)
    R.record_worker_outcome("w", ok=False)
    # (2 + 1) / (3 + 2) = 0.6
    assert R.reliability_ratio("w") == pytest.approx(0.6)


def test_record_ignores_empty_worker():
    R.record_worker_outcome("", ok=True)
    R.record_worker_outcome(None, ok=False)
    assert STATE.worker_outcomes == {}


def test_bucket_orders_reliable_above_unreliable():
    for _ in range(10):
        R.record_worker_outcome("good", ok=True)
        R.record_worker_outcome("bad", ok=False)
    assert R.reliability_bucket("good") > R.reliability_bucket("bad")
    # An unknown worker sits between the two extremes.
    assert R.reliability_bucket("bad") < R.reliability_bucket("unknown") < R.reliability_bucket("good")


# ---- selector integration --------------------------------------------------
# w_fit has more RAM (better raw fitness); w_rel has a better track record.
# Reliability, when on, must outrank raw fitness.

def _two_workers():
    R.record_worker_outcome("w_rel", ok=True)
    for _ in range(10):
        R.record_worker_outcome("w_rel", ok=True)
        R.record_worker_outcome("w_fit", ok=False)
    return {"w_fit": _worker(free_ram=8192), "w_rel": _worker(free_ram=4096)}


def test_prefer_reliable_off_picks_best_fitness():
    workers = _two_workers()
    task = _task(prefer=False)  # explicit off
    assert select_task_for_worker("w_fit", [task], workers, {}, {}) is not None
    assert select_task_for_worker("w_rel", [task], workers, {}, {}) is None


def test_prefer_reliable_on_picks_reliable_node():
    workers = _two_workers()
    task = _task(prefer=True)  # per-task override on
    assert select_task_for_worker("w_rel", [task], workers, {}, {}) is not None
    assert select_task_for_worker("w_fit", [task], workers, {}, {}) is None


def test_task_inherits_node_default():
    workers = _two_workers()
    task = _task(prefer=None)  # no override → inherit node default
    LOCAL_SETTINGS["prefer_reliable_workers"] = True
    assert select_task_for_worker("w_rel", [task], workers, {}, {}) is not None
    assert select_task_for_worker("w_fit", [task], workers, {}, {}) is None


def test_task_override_beats_node_default():
    workers = _two_workers()
    LOCAL_SETTINGS["prefer_reliable_workers"] = True  # global on
    task = _task(prefer=False)                          # but this task opts out
    assert select_task_for_worker("w_fit", [task], workers, {}, {}) is not None
    assert select_task_for_worker("w_rel", [task], workers, {}, {}) is None
