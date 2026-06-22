"""Save-as-artifact falls back to the persisted log once a task has finished.

A completed task keeps no live log buffer, so the old endpoint 404'd with "No
live log to save" even though the task's rolling log was right there (and
downloadable). It must fall back to TaskRecord.logs.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import TaskRecord, database, get_session


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
def client(isolated_db, monkeypatch):
    captured = {}

    def _fake_write(task_id, lines):
        captured["task_id"] = task_id
        captured["lines"] = list(lines)
        return "live-log-test.log"

    # Avoid touching the real filesystem; assert on what the endpoint passes.
    monkeypatch.setattr("nexus.runtime.result_browser.write_log_artifact", _fake_write)

    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        c._captured = captured  # type: ignore[attr-defined]
        yield c


def _seed(task_id, *, status, logs):
    async def _go():
        async with get_session() as db:
            db.add(TaskRecord(id=task_id, parent_id="", depends_on="", status=status,
                              env_vars=json.dumps({}), payload=b"", worker="", logs=logs))
            await db.commit()
    asyncio.run(_go())


def test_save_falls_back_to_stored_log(client):
    _seed("done1", status="completed", logs="line one\nline two\n")
    res = client.post("/local/task_log_tail/done1/save")
    assert res.status_code == 200, res.text
    # Trailing blank line from the final newline is trimmed.
    assert client._captured["lines"] == ["line one", "line two"]
    assert client._captured["task_id"] == "done1"


def test_save_404_when_no_log_at_all(client):
    _seed("empty1", status="completed", logs="")
    res = client.post("/local/task_log_tail/empty1/save")
    assert res.status_code == 404
