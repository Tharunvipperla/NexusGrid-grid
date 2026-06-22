"""A3 step gate — per-level DAG approval.

Covers the tri-state metadata round-trip, the `_gate_on` precedence
(per-dispatch override beats node default), the scheduler holding a gated step
at ``awaiting_approval`` once its deps complete (while the first level still
runs), and the ``/workflows/{id}/approve_step`` endpoint releasing that level.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.core import LOCAL_SETTINGS
from nexus.scheduler.dag import _gate_on, release_ready_tasks
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import TaskRecord, database, get_session
from nexus.tasks.metadata import build_task_metadata, extract_task_metadata


def _task(task_id, *, parent="wf", depends_on="", status="waiting", gate=None):
    env = build_task_metadata({}, step_gate=gate)
    return TaskRecord(
        id=task_id, parent_id=parent, depends_on=depends_on,
        status=status, env_vars=json.dumps(env), payload=b"", worker="",
    )


# ---- metadata round-trip + precedence (pure) --------------------------------

def test_metadata_roundtrip_tristate():
    assert extract_task_metadata(_task("a", gate=True))["step_gate"] is True
    assert extract_task_metadata(_task("a", gate=False))["step_gate"] is False
    # Unset -> key absent -> None (inherit the node default).
    assert extract_task_metadata(_task("a", gate=None))["step_gate"] is None


def test_gate_on_precedence():
    prev = LOCAL_SETTINGS.get("step_gate", False)
    try:
        LOCAL_SETTINGS["step_gate"] = False
        assert _gate_on(_task("a", gate=None)) is False   # inherit default off
        assert _gate_on(_task("a", gate=True)) is True     # override on
        LOCAL_SETTINGS["step_gate"] = True
        assert _gate_on(_task("a", gate=None)) is True      # inherit default on
        assert _gate_on(_task("a", gate=False)) is False    # override off
    finally:
        LOCAL_SETTINGS["step_gate"] = prev


# ---- scheduler + approve endpoint (integration) -----------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed(tasks):
    async def _go():
        async with get_session() as db:
            for t in tasks:
                db.add(t)
            await db.commit()
    asyncio.run(_go())


def _status(task_id):
    async def _go():
        async with get_session() as db:
            return (await db.get(TaskRecord, task_id)).status
    return asyncio.run(_go())


def test_first_level_runs_then_child_is_gated(isolated_db):
    # a: no deps; b: depends on a. Both gated.
    _seed([_task("a", gate=True), _task("b", depends_on="a", gate=True)])

    asyncio.run(release_ready_tasks())
    # First level runs (only its nodes get assigned); the child waits on deps.
    assert _status("a") == "queued"
    assert _status("b") == "waiting"

    # a finishes -> b's deps are met, but the gate holds it for approval.
    async def _complete_a():
        async with get_session() as db:
            (await db.get(TaskRecord, "a")).status = "completed"
            await db.commit()
    asyncio.run(_complete_a())

    queued, gated = asyncio.run(release_ready_tasks())
    assert (queued, gated) == (0, 1)
    assert _status("b") == "awaiting_approval"


def test_no_gate_child_queues_straight_through(isolated_db):
    _seed([_task("a", status="completed", gate=False),
           _task("b", depends_on="a", gate=False)])
    asyncio.run(release_ready_tasks())
    assert _status("b") == "queued"   # deps met, no gate -> queued, not held


def test_approve_step_releases_the_waiting_level(client):
    _seed([_task("b", depends_on="a", status="awaiting_approval", gate=True),
           _task("c", depends_on="a", status="awaiting_approval", gate=True)])
    res = client.post("/local/workflows/wf/approve_step")
    assert res.status_code == 200, res.text
    assert sorted(res.json()["released"]) == ["b", "c"]
    assert _status("b") == "queued" and _status("c") == "queued"


def test_approve_step_404_when_nothing_waiting(client):
    _seed([_task("b", depends_on="a", status="waiting", gate=True)])
    res = client.post("/local/workflows/wf/approve_step")
    assert res.status_code == 404


def _complete(*task_ids):
    async def _go():
        async with get_session() as db:
            for tid in task_ids:
                (await db.get(TaskRecord, tid)).status = "completed"
            await db.commit()
    asyncio.run(_go())


def test_gate_releases_whole_parallel_level_together(client):
    """step1 ∥ step2 → step3: both parallel firsts run, then step3 is gated.
    A diamond confirms a gated parallel level (B ∥ C) releases together on one
    approval, while a straight chain still advances one step at a time."""
    _seed([
        _task("stepA", gate=True),                          # level 0 (no deps)
        _task("stepB", depends_on="stepA", gate=True),      # level 1, parallel
        _task("stepC", depends_on="stepA", gate=True),      # level 1, parallel
        _task("stepD", depends_on="stepB,stepC", gate=True),  # level 2 (joins)
    ])
    # First level runs automatically (only its node is assigned up front).
    asyncio.run(release_ready_tasks())
    assert _status("stepA") == "queued"
    assert _status("stepB") == _status("stepC") == _status("stepD") == "waiting"

    # stepA done -> the whole parallel level B∥C is held (not just one of them).
    _complete("stepA")
    asyncio.run(release_ready_tasks())
    assert _status("stepB") == "awaiting_approval"
    assert _status("stepC") == "awaiting_approval"
    assert _status("stepD") == "waiting"   # join not ready — only one level held

    # One approval releases BOTH parallel siblings together.
    res = client.post("/local/workflows/wf/approve_step")
    assert sorted(res.json()["released"]) == ["stepB", "stepC"]
    assert _status("stepB") == _status("stepC") == "queued"

    # The join waits for BOTH, then is gated on its own (chain = one at a time).
    _complete("stepB")
    asyncio.run(release_ready_tasks())
    assert _status("stepD") == "waiting"   # stepC not done yet
    _complete("stepC")
    asyncio.run(release_ready_tasks())
    assert _status("stepD") == "awaiting_approval"


def _step_gate_meta(task_id):
    async def _go():
        async with get_session() as db:
            return extract_task_metadata(await db.get(TaskRecord, task_id))["step_gate"]
    return asyncio.run(_go())


def test_add_workflow_threads_step_gate_into_both_branches(client):
    """Regression: ``add_workflow`` must stamp NEXUS_META_STEP_GATE on every
    task it builds — both the sliced and the plain branch. A live demo caught
    the plain branch silently dropping it."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.txt", "ws\n")
    dag = json.dumps([
        {"id": "plain", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python x.py", "depends_on": []},
        {"id": "sliced", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python y.py", "depends_on": ["plain"], "slice_count": 2},
    ])
    res = client.post(
        "/local/add_workflow",
        files={"file": ("ws.zip", buf.getvalue(), "application/zip")},
        data={"workflow_id": "wf", "workflow_json": dag, "step_gate": "on"},
    )
    assert res.status_code == 200, res.text
    # Plain branch (the one the bug dropped) and each slice of the sliced branch.
    assert _step_gate_meta("wf_plain") is True
    assert _step_gate_meta("wf_sliced_p0") is True
    assert _step_gate_meta("wf_sliced_p1") is True


def test_add_workflow_step_gate_inherits_when_unset(client):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.txt", "ws\n")
    dag = json.dumps([{"id": "s", "runtime": "docker", "image": "python:3.11-slim",
                       "entrypoint": "python x.py", "depends_on": []}])
    res = client.post(
        "/local/add_workflow",
        files={"file": ("ws.zip", buf.getvalue(), "application/zip")},
        data={"workflow_id": "wf2", "workflow_json": dag},  # step_gate omitted
    )
    assert res.status_code == 200, res.text
    assert _step_gate_meta("wf2_s") is None   # inherit the node default
