"""Wave 70 — lightweight chat groups (Group.kind)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def test_chat_group_creation_and_kind_exposure(client):
    r = client.post("/local/groups", json={"name": "weekend plans", "kind": "chat"})
    assert r.status_code == 200
    gid = r.json()["id"]
    listed = {g["id"]: g for g in client.get("/local/groups").json()["groups"]}
    assert listed[gid]["kind"] == "chat"
    assert client.get(f"/local/groups/{gid}").json()["kind"] == "chat"


def test_default_kind_is_full(client):
    r = client.post("/local/groups", json={"name": "real group"})
    assert r.status_code == 200
    gid = r.json()["id"]
    assert client.get(f"/local/groups/{gid}").json()["kind"] == "full"


def test_invalid_kind_rejected(client):
    r = client.post("/local/groups", json={"name": "x", "kind": "channel"})
    assert r.status_code == 400
