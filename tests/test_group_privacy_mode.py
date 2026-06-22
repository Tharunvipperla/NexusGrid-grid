"""Wave 16.2 — privacy_mode wiring at the API + peer join_request branch.

Open mode preserves the Wave-15 flow. Private mode parks join requests
in a pending queue, does **not** consume the invite slot, and returns
HTTP 200 with ``status="pending"``.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as local_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database
from nexus.storage.models import GroupInviteLink, GroupPendingJoinRequest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "privacy.db"
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


# ---- create / edit ------------------------------------------------------


def test_create_group_defaults_to_open(client):
    g = client.post("/local/groups", json={"name": "A"}).json()
    assert g["privacy_mode"] == "open"


def test_create_group_accepts_private(client):
    g = client.post(
        "/local/groups", json={"name": "B", "privacy_mode": "private"}
    ).json()
    assert g["privacy_mode"] == "private"


def test_create_group_rejects_unknown_mode(client):
    res = client.post(
        "/local/groups", json={"name": "C", "privacy_mode": "secret"}
    )
    assert res.status_code == 400


def test_get_group_returns_privacy_mode(client):
    g = client.post(
        "/local/groups", json={"name": "D", "privacy_mode": "private"}
    ).json()
    detail = client.get(f"/local/groups/{g['id']}").json()
    assert detail["privacy_mode"] == "private"


def test_list_groups_returns_privacy_mode(client):
    client.post("/local/groups", json={"name": "Open"}).json()
    client.post(
        "/local/groups", json={"name": "Locked", "privacy_mode": "private"}
    ).json()
    listing = client.get("/local/groups").json()
    modes = {g["name"]: g["privacy_mode"] for g in listing["groups"]}
    assert modes["Open"] == "open"
    assert modes["Locked"] == "private"


def test_set_privacy_mode_flips_open_to_private(client):
    g = client.post("/local/groups", json={"name": "FlipMe"}).json()
    res = client.post(
        f"/local/groups/{g['id']}/privacy",
        json={"privacy_mode": "private"},
    )
    assert res.status_code == 200
    detail = client.get(f"/local/groups/{g['id']}").json()
    assert detail["privacy_mode"] == "private"


def test_set_privacy_mode_rejects_unknown(client):
    g = client.post("/local/groups", json={"name": "X"}).json()
    res = client.post(
        f"/local/groups/{g['id']}/privacy",
        json={"privacy_mode": "ghost"},
    )
    assert res.status_code == 400


def test_set_privacy_mode_404_for_unknown_group(client):
    res = client.post(
        "/local/groups/no-such/privacy",
        json={"privacy_mode": "private"},
    )
    assert res.status_code == 404


# ---- /peer/group/join_request branches on privacy_mode ------------------


def _other_pubkey() -> str:
    from nexus.security.group_grant import generate_keypair
    return generate_keypair()[1]


def test_open_group_join_request_issues_grant_immediately(client):
    g = client.post("/local/groups", json={"name": "OpenJoin"}).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    ).json()
    res = client.post(
        "/peer/group/join_request",
        json={"invite_token": inv["token"], "joiner_pubkey": _other_pubkey()},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["privacy_mode"] == "open"
    assert body["grant_blob_b64"]


def test_private_group_join_request_returns_pending_without_grant(client):
    g = client.post(
        "/local/groups", json={"name": "Closed", "privacy_mode": "private"}
    ).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    ).json()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": inv["token"],
            "joiner_pubkey": _other_pubkey(),
            "message": "let me in plz",
            "joiner_address": "10.0.0.5:8001",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "pending"
    assert body["privacy_mode"] == "private"
    assert body["request_id"]
    assert "grant_blob_b64" not in body


def test_private_group_join_does_not_consume_slot(isolated_db, client):
    g = client.post(
        "/local/groups", json={"name": "NoSlotBurn", "privacy_mode": "private"}
    ).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    ).json()
    client.post(
        "/peer/group/join_request",
        json={
            "invite_token": inv["token"],
            "joiner_pubkey": _other_pubkey(),
        },
    )

    async def _read_slots():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInviteLink).where(
                        GroupInviteLink.token == inv["token"]
                    )
                )
            ).scalar_one()
            return row.slots_filled, row.active

    slots_filled, active = asyncio.run(_read_slots())
    assert slots_filled == 0
    assert active == 1


def test_private_group_join_persists_pending_row_with_message(isolated_db, client):
    g = client.post(
        "/local/groups", json={"name": "WithMsg", "privacy_mode": "private"}
    ).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 2}
    ).json()
    joiner_pub = _other_pubkey()
    client.post(
        "/peer/group/join_request",
        json={
            "invite_token": inv["token"],
            "joiner_pubkey": joiner_pub,
            "message": "I run a model server you might like.",
            "joiner_address": "192.168.1.42:8000",
        },
    )

    async def _read_pending():
        async with database.get_session() as s:
            rows = (
                await s.execute(
                    select(GroupPendingJoinRequest).where(
                        GroupPendingJoinRequest.group_id == g["id"]
                    )
                )
            ).scalars().all()
            return [(r.joiner_pubkey, r.message, r.joiner_address, r.status) for r in rows]

    pending = asyncio.run(_read_pending())
    assert pending == [
        (joiner_pub, "I run a model server you might like.", "192.168.1.42:8000", "pending"),
    ]
