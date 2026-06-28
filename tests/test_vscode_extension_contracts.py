"""Regression guards for the ``/local/*`` contracts the VS Code extension relies
on. The extension (``extensions/vscode/``) is a thin client that speaks these
wire formats directly, so a backend change that renames a field or alters a
status would silently break it. These lock the behaviour the extension expects:

- dispatch / DAG / service via ``POST /local/add_workflow`` (the exact step
  shapes the extension's ``postSteps``/``runDag``/``buildService`` emit),
- the four node-setting toggles the extension exposes,
- foreign-storage deposit/retrieve validation + listing.

In-process router tests with the project's TestClient harness (mirrors
``test_step_gate``/``test_wave68_settings_partial``); no second node needed.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router, _SETTINGS_ALL_FIELDS
from nexus.core import LOCAL_SETTINGS
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import TaskRecord, database, get_session


# --- harness ---------------------------------------------------------------

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


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.txt", "ws\n")
    return buf.getvalue()


def _deploy(client, wf_id, steps, **form):
    """Post a workflow the way the extension's postSteps does (zip + form)."""
    data = {"workflow_id": wf_id, "workflow_json": json.dumps(steps)}
    data.update(form)
    return client.post(
        "/local/add_workflow",
        files={"file": ("ws.zip", _zip(), "application/zip")},
        data=data,
    )


def _manifest(client, task_id):
    r = client.get(f"/local/task_manifest/{task_id}")
    assert r.status_code == 200, r.text
    return r.json()["manifest"]


def _status(task_id):
    async def _go():
        async with get_session() as db:
            row = await db.get(TaskRecord, task_id)
            return row.status if row else None
    return asyncio.run(_go())


# --- dispatch (single step) ------------------------------------------------

def test_single_step_dispatch_creates_one_workflow_task(client):
    # The extension's one-off dispatch is a one-step workflow; it must land as a
    # single task stamped with the workflow id as parent (this is what the
    # telemetry tab keys "Dispatch" vs "DAG" on).
    r = _deploy(client, "wf1", [
        {"id": "s", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python hello.py", "depends_on": []},
    ])
    assert r.status_code == 200, r.text
    man = _manifest(client, "wf1_s")
    assert man["image"] == "python:3.11-slim"
    assert man["runtime"] == "docker"
    assert man["entrypoint"] == "python hello.py"
    async def _parent():
        async with get_session() as db:
            return (await db.get(TaskRecord, "wf1_s")).parent_id
    assert asyncio.run(_parent()) == "wf1"


# --- DAG (multi step) ------------------------------------------------------

def test_multistep_dag_honors_depends_on(client):
    r = _deploy(client, "wf2", [
        {"id": "a", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python prep.py", "depends_on": []},
        {"id": "b", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python agg.py", "depends_on": ["a"]},
    ])
    assert r.status_code == 200, r.text
    # Entry step is runnable; the dependent waits on it.
    assert _status("wf2_a") == "queued"
    assert _status("wf2_b") == "waiting"


def test_dag_step_resource_keys_map_to_manifest(client):
    # The extension emits ram_limit (GB→MB upstream) and cpu_limit; the server
    # must map them onto ram_limit_mb / cpu_limit_pct.
    r = _deploy(client, "wf3", [
        {"id": "s", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python x.py", "depends_on": [],
         "ram_limit": 2048, "cpu_limit": 50},
    ])
    assert r.status_code == 200, r.text
    man = _manifest(client, "wf3_s")
    assert man["ram_limit_mb"] == 2048
    assert man["cpu_limit_pct"] == 50


# --- service ---------------------------------------------------------------

def test_service_step_passes_through_service_fields(client):
    # buildService emits runtime:"service" + expose_ports/service_kind; the
    # server must keep those on the manifest (the start endpoint refuses any
    # task whose runtime isn't "service").
    r = _deploy(client, "wf4", [
        {"id": "svc", "runtime": "service", "image": "redis:7", "entrypoint": "",
         "expose_ports": [6379], "service_kind": "tcp", "depends_on": []},
    ])
    assert r.status_code == 200, r.text
    man = _manifest(client, "wf4_svc")
    assert man["runtime"] == "service"
    assert man["expose_ports"] == [6379]
    assert man["service_kind"] == "tcp"


# --- targeting / overrides -------------------------------------------------

def test_target_groups_accepted(client):
    # The DAG/dispatch profiles can carry target_groups; it must deploy.
    r = _deploy(client, "wf5", [
        {"id": "s", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python x.py", "depends_on": []},
    ], target_groups=json.dumps(["team-a"]))
    assert r.status_code == 200, r.text
    assert _status("wf5_s") in ("queued", "waiting")


def test_preferred_worker_must_be_trusted(client):
    # The extension's Set Target only offers real workers; a worker that isn't a
    # trusted compute peer is rejected server-side (guards that contract).
    r = _deploy(client, "wf6", [
        {"id": "s", "runtime": "docker", "image": "python:3.11-slim",
         "entrypoint": "python x.py", "depends_on": []},
    ], preferred_workers=json.dumps(["9.9.9.9"]))
    assert r.status_code == 400
    assert "trusted" in r.json()["detail"].lower()


# --- node-setting toggles --------------------------------------------------

def test_toggle_fields_are_known_settings():
    # Toggle Node Setting writes these via settings_partial, which 400s on any
    # unknown field — so they MUST stay in the allow-list.
    for f in ("node_online", "node_gpu", "cache_venvs", "foreign_storage_accept_offers"):
        assert f in _SETTINGS_ALL_FIELDS, f


def test_toggle_round_trips_via_settings_partial(client):
    for field in ("cache_venvs", "foreign_storage_accept_offers"):
        r = client.post("/local/settings_partial", json={field: True})
        assert r.status_code == 200, r.text
        assert LOCAL_SETTINGS[field] is True
        r = client.post("/local/settings_partial", json={field: False})
        assert r.status_code == 200, r.text
        assert LOCAL_SETTINGS[field] is False


# --- foreign storage: deposit / retrieve / list ----------------------------

def test_deposit_requires_target_file_password(client):
    r = client.post("/local/foreign_storage/deposit",
                    json={"target_peer": "auto", "file_path": "x"})  # no password
    assert r.status_code == 400


def test_deposit_missing_file_is_404(client):
    r = client.post("/local/foreign_storage/deposit",
                    json={"target_peer": "auto", "file_path": "/no/such/file.bin",
                          "password": "pw", "queue_if_offline": False})
    assert r.status_code == 404


def test_retrieve_requires_password_and_path(client):
    r = client.post("/local/foreign_storage/retrieve/whatever", json={"password": "pw"})
    assert r.status_code == 400


def test_retrieve_unknown_deposit_is_404(client):
    r = client.post("/local/foreign_storage/retrieve/deadbeef",
                    json={"password": "pw", "save_to_path": "/tmp"})
    assert r.status_code == 404


def test_my_deposits_lists_empty(client):
    r = client.get("/local/foreign_storage/my_deposits")
    assert r.status_code == 200
    assert r.json()["deposits"] == []


def test_peer_capacities_returns_peers_key(client):
    r = client.get("/local/foreign_storage/peer_capacities")
    assert r.status_code == 200
    assert "peers" in r.json()
