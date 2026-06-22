"""Wave 9.7: IP/copyright consent gate for cloud task-data sources."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.local import router as local_router
from nexus.core import LOCAL_SETTINGS
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.security import task_data_terms as terms
from nexus.storage import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    LOCAL_SETTINGS.pop("task_data_terms_accepted_version", None)
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    LOCAL_SETTINGS.pop("task_data_terms_accepted_version", None)
    tokens._reset_for_testing()


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# terms module
# ---------------------------------------------------------------------------

def test_terms_module_initial_state():
    LOCAL_SETTINGS.pop("task_data_terms_accepted_version", None)
    assert terms.current_version() == "v1"
    assert terms.accepted_version() == ""
    assert terms.is_current_accepted() is False


def test_terms_module_accepts_when_local_settings_match():
    LOCAL_SETTINGS["task_data_terms_accepted_version"] = terms.current_version()
    assert terms.is_current_accepted() is True
    LOCAL_SETTINGS.pop("task_data_terms_accepted_version", None)


def test_terms_version_bump_requires_reacceptance(monkeypatch):
    LOCAL_SETTINGS["task_data_terms_accepted_version"] = "v1"
    assert terms.is_current_accepted() is True
    monkeypatch.setattr(terms, "TASK_DATA_TERMS_VERSION", "v2")
    assert terms.is_current_accepted() is False
    LOCAL_SETTINGS.pop("task_data_terms_accepted_version", None)


# ---------------------------------------------------------------------------
# /local/task_data_terms endpoints
# ---------------------------------------------------------------------------

def test_get_terms_endpoint_before_accept(client):
    res = client.get("/local/task_data_terms")
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == "v1"
    assert "licensed" in body["text"].lower()
    assert body["accepted"] is False
    assert body["accepted_version"] == ""


def test_accept_endpoint_records_audit(client):
    res = client.post("/local/task_data_terms/accept")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # GET now reports accepted.
    res = client.get("/local/task_data_terms")
    body = res.json()
    assert body["accepted"] is True
    assert body["accepted_version"] == "v1"

    # Audit row exists.
    from nexus.storage import get_session
    from nexus.storage.models import AuditEvent

    async def _check():
        async with get_session() as db:
            rows = (
                await db.execute(
                    select(AuditEvent).filter(
                        AuditEvent.action == "task.data_terms_accepted"
                    )
                )
            ).scalars().all()
            return rows

    rows = asyncio.run(_check())
    assert len(rows) == 1
    assert "version=v1" in rows[0].details


# ---------------------------------------------------------------------------
# add_workflow gate
# ---------------------------------------------------------------------------

def _build_workflow_payload(with_cloud: bool):
    task = {
        "id": "demo",
        "runtime": "docker",
        "image": "python:3.11-slim",
        "entrypoint": "python main.py",
        "depends_on": [],
    }
    if with_cloud:
        task["data_sources"] = [
            {
                "type": "gdrive",
                "credential_id": "test-cred",
                "folder_id": "FOLDER",
                "mount_path": "data",
            }
        ]
    return [task]


def _zip_bytes(name: str = "main.py", body: bytes = b"print(1)\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, body)
    return buf.getvalue()


def test_add_workflow_412_when_cloud_source_and_terms_not_accepted(client):
    res = client.post(
        "/local/add_workflow",
        data={
            "workflow_id": "wf1",
            "workflow_json": json.dumps(_build_workflow_payload(with_cloud=True)),
        },
        files={"file": ("workspace.zip", _zip_bytes(), "application/zip")},
    )
    assert res.status_code == 412, res.text
    detail = res.json()["detail"]
    assert detail["version"] == "v1"
    assert "licensed" in detail["terms"].lower()


def test_add_workflow_passes_gate_after_accept(client):
    # Accept first.
    res = client.post("/local/task_data_terms/accept")
    assert res.status_code == 200

    res = client.post(
        "/local/add_workflow",
        data={
            "workflow_id": "wf2",
            "workflow_json": json.dumps(_build_workflow_payload(with_cloud=True)),
        },
        files={"file": ("workspace.zip", _zip_bytes(), "application/zip")},
    )
    # Gate is past — workflow now succeeds (the credential lookup happens
    # at dispatch time, not submit time, so a bogus credential_id is fine
    # for this gate-only test).
    assert res.status_code == 200, res.text
    assert "Deployed" in res.json()["message"]


def test_add_workflow_unaffected_when_no_cloud_sources(client):
    """A workflow without cloud sources never hits the gate."""
    res = client.post(
        "/local/add_workflow",
        data={
            "workflow_id": "wf3",
            "workflow_json": json.dumps(_build_workflow_payload(with_cloud=False)),
        },
        files={"file": ("workspace.zip", _zip_bytes(), "application/zip")},
    )
    assert res.status_code == 200, res.text
