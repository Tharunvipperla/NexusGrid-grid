"""Task run-duration (#7): set_task_status stamps start/end, and the metadata
projection exposes started_at / completed_at / elapsed_secs."""

import json

from nexus.storage import TaskRecord
from nexus.tasks.lifecycle import set_task_status
from nexus.tasks.metadata import (
    _task_elapsed_secs,
    extract_task_metadata,
    parse_task_env,
)


def _task(env=None, status="queued"):
    return TaskRecord(
        id="t1",
        env_vars=json.dumps(env or {}),
        payload=b"",
        status=status,
        worker="w",
    )


def test_elapsed_none_when_not_started():
    assert _task_elapsed_secs({}) is None


def test_elapsed_uses_completed_when_finished():
    env = {"NEXUS_META_STARTED_AT": 100.0, "NEXUS_META_COMPLETED_AT": 142.0}
    assert _task_elapsed_secs(env) == 42.0


def test_elapsed_uses_now_while_running(monkeypatch):
    import nexus.tasks.metadata as md

    monkeypatch.setattr(md, "now_epoch", lambda: 130.0)
    assert _task_elapsed_secs({"NEXUS_META_STARTED_AT": 100.0}) == 30.0


def test_metadata_exposes_timing_fields():
    env = {"NEXUS_META_STARTED_AT": 100.0, "NEXUS_META_COMPLETED_AT": 150.0}
    m = extract_task_metadata(_task(env))
    assert m["started_at"] == 100.0
    assert m["completed_at"] == 150.0
    assert m["elapsed_secs"] == 50.0


def test_set_task_status_stamps_start_and_end():
    task = _task(status="queued")
    set_task_status(task, "processing", force=True)
    started = parse_task_env(task).get("NEXUS_META_STARTED_AT")
    assert started  # stamped on first run

    # re-entering a running state must not move the start time
    set_task_status(task, "serving", force=True)
    assert parse_task_env(task).get("NEXUS_META_STARTED_AT") == started

    set_task_status(task, "completed", force=True)
    assert parse_task_env(task).get("NEXUS_META_COMPLETED_AT")
