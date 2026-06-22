"""Wave 69 — group profile pictures.

A ``role:assign`` holder sets a small ``data:image/...;base64,`` URL on
the group; it lands on the local ``Group.avatar`` column and is synced to
members via the durable ``group.meta`` frame. Size/shape validation keeps
a hostile admin from bloating members' databases.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.groups import router as groups_router
from nexus.runtime.group_inbox import AVATAR_MAX_CHARS, _avatar_valid
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import Group


PNG_AVATAR = "data:image/png;base64," + "A" * 200


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "groups.db"
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
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _create_group(client) -> str:
    r = client.post("/local/groups", json={"name": "avatar-crew"})
    assert r.status_code == 200
    return r.json()["id"]


def test_avatar_validation_rules():
    assert _avatar_valid("")                                    # clears
    assert _avatar_valid(PNG_AVATAR)
    assert not _avatar_valid("https://evil.example/x.png")      # not a data URL
    assert not _avatar_valid("data:text/html;base64,AAAA")      # not an image
    assert not _avatar_valid("data:image/png;base64," + "A" * (AVATAR_MAX_CHARS + 1))


def test_set_and_clear_avatar_roundtrip(client):
    gid = _create_group(client)
    r = client.post(f"/local/groups/{gid}/avatar", json={"avatar": PNG_AVATAR})
    assert r.status_code == 200 and r.json()["avatar_set"] is True

    async def _avatar():
        async with get_session() as db:
            g = (
                await db.execute(select(Group).filter(Group.id == gid))
            ).scalar_one()
            return g.avatar

    assert asyncio.run(_avatar()) == PNG_AVATAR
    # list + detail expose it
    assert client.get("/local/groups").json()["groups"][0]["avatar"] == PNG_AVATAR
    assert client.get(f"/local/groups/{gid}").json()["avatar"] == PNG_AVATAR
    # clear
    r = client.post(f"/local/groups/{gid}/avatar", json={"avatar": ""})
    assert r.status_code == 200 and r.json()["avatar_set"] is False
    assert asyncio.run(_avatar()) == ""


def test_bad_avatar_rejected(client):
    gid = _create_group(client)
    r = client.post(
        f"/local/groups/{gid}/avatar",
        json={"avatar": "data:image/png;base64," + "A" * (AVATAR_MAX_CHARS + 1)},
    )
    assert r.status_code == 400
    r = client.post(
        f"/local/groups/{gid}/avatar", json={"avatar": "http://x/y.png"}
    )
    assert r.status_code == 400
