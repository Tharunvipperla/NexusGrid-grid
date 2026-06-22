"""C2 — DAG-aware resume planning: which steps re-queue vs re-arm vs untouched."""

from __future__ import annotations

from nexus.tasks.workflow_resume import plan_workflow_resume


def test_failed_with_deps_satisfied_is_requeued():
    tasks = [
        {"id": "a", "status": "completed", "depends_on": []},
        {"id": "b", "status": "failed", "depends_on": ["a"]},
    ]
    plan = plan_workflow_resume(tasks)
    assert plan["requeue"] == ["b"] and plan["rearm"] == []


def test_failed_with_unmet_deps_is_rearmed():
    tasks = [
        {"id": "a", "status": "failed", "depends_on": []},
        {"id": "b", "status": "failed", "depends_on": ["a"]},  # upstream not done
    ]
    plan = plan_workflow_resume(tasks)
    assert plan["requeue"] == ["a"]      # no deps -> requeue
    assert plan["rearm"] == ["b"]        # waits on a


def test_completed_and_inflight_untouched():
    tasks = [
        {"id": "a", "status": "completed", "depends_on": []},
        {"id": "b", "status": "processing", "depends_on": []},
        {"id": "c", "status": "queued", "depends_on": []},
        {"id": "d", "status": "waiting", "depends_on": ["b"]},
    ]
    assert plan_workflow_resume(tasks) == {"requeue": [], "rearm": []}


def test_cancelled_and_disrupted_counted_as_failed():
    tasks = [
        {"id": "a", "status": "completed", "depends_on": []},
        {"id": "b", "status": "disrupted", "depends_on": ["a"]},   # deps met -> requeue
        {"id": "c", "status": "cancelled", "depends_on": ["b"]},   # b not done -> rearm
    ]
    plan = plan_workflow_resume(tasks)
    assert plan["requeue"] == ["b"] and plan["rearm"] == ["c"]


def test_no_deps_failed_requeued():
    assert plan_workflow_resume(
        [{"id": "solo", "status": "failed", "depends_on": []}]
    ) == {"requeue": ["solo"], "rearm": []}


def test_empty_workflow():
    assert plan_workflow_resume([]) == {"requeue": [], "rearm": []}
