"""Wave 16.7 — pre-join /peer/group/info probe.

Validates that the probe returns the group's privacy_mode without
consuming the invite token, refuses invalid tokens, and that the
local /local/groups/probe proxy forwards the call.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api import groups as groups_module
from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as local_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "probe.db"
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
    app.include_router(local_router)
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


# ---- /peer/group/info ---------------------------------------------------


def test_info_returns_privacy_mode_for_private_group(client):
    g = client.post(
        "/local/groups", json={"name": "Closed", "privacy_mode": "private"}
    ).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 3}
    ).json()
    res = client.post(
        "/peer/group/info", json={"invite_token": inv["token"]}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["group_id"] == g["id"]
    assert body["group_name"] == "Closed"
    assert body["privacy_mode"] == "private"
    assert body["slots_remaining"] == 3


def test_info_returns_open_for_open_group(client):
    g = client.post("/local/groups", json={"name": "Open"}).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 0}
    ).json()
    res = client.post(
        "/peer/group/info", json={"invite_token": inv["token"]}
    )
    body = res.json()
    assert body["privacy_mode"] == "open"
    # slot_cap == 0 means uncapped — reported as -1.
    assert body["slots_remaining"] == -1


def test_info_404_for_unknown_token(client):
    res = client.post("/peer/group/info", json={"invite_token": "nope"})
    assert res.status_code == 404


def test_info_does_not_consume_slot(client):
    g = client.post("/local/groups", json={"name": "Open"}).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    ).json()
    for _ in range(3):
        res = client.post(
            "/peer/group/info", json={"invite_token": inv["token"]}
        )
        assert res.status_code == 200
        assert res.json()["slots_remaining"] == 1


# ---- /local/groups/probe ------------------------------------------------


def test_local_probe_forwards_to_admin(client, monkeypatch):
    seen: dict = {}

    async def _fake_post(
        address, path, body, admin_node_id="",
        link_relay_urls=None, link_grid_key="",
    ):
        seen["address"] = address
        seen["path"] = path
        seen["body"] = body
        return 200, {
            "group_id": "g-1",
            "group_name": "Squad",
            "founder_pubkey": "fpk",
            "privacy_mode": "private",
            "slots_remaining": 2,
        }

    monkeypatch.setattr(groups_module, "_post_to_admin", _fake_post)
    res = client.post(
        "/local/groups/probe",
        json={"admin_address": "10.0.0.1:8000", "invite_token": "tok-1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["privacy_mode"] == "private"
    assert seen["address"] == "10.0.0.1:8000"
    assert seen["path"] == "/peer/group/info"
    assert seen["body"] == {"invite_token": "tok-1"}


def test_local_probe_surfaces_admin_404(client, monkeypatch):
    async def _fake_post(
        address, path, body, admin_node_id="",
        link_relay_urls=None, link_grid_key="",
    ):
        return 404, {"detail": "invite invalid: not_found"}
    monkeypatch.setattr(groups_module, "_post_to_admin", _fake_post)
    res = client.post(
        "/local/groups/probe",
        json={"admin_address": "10.0.0.1:8000", "invite_token": "bad"},
    )
    assert res.status_code == 404
