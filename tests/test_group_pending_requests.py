"""Wave 16.3 — admin endpoints for the private-mode pending queue.

Covers list / approve / reject; approve consumes exactly one slot,
reject leaves the slot alone; both refuse to re-decide a row that
isn't ``pending``; auth-gating is exercised by the dependency-override
pattern from test_group_api.py.
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
from nexus.security.group_grant import generate_keypair
from nexus.storage import database
from nexus.storage.models import GroupInviteLink, GroupPendingJoinRequest


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "pending.db"
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


def _setup_private_group_with_pending(client, slot_cap: int = 2):
    g = client.post(
        "/local/groups", json={"name": "Closed", "privacy_mode": "private"}
    ).json()
    inv = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": slot_cap}
    ).json()
    joiner_pub = generate_keypair()[1]
    resp = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": inv["token"],
            "joiner_pubkey": joiner_pub,
            "message": "let me in",
            "joiner_address": "10.0.0.5:8001",
        },
    ).json()
    return g, inv, joiner_pub, resp["request_id"]


# ---- list ---------------------------------------------------------------


def test_list_pending_returns_what_was_submitted(client):
    g, _inv, joiner_pub, rid = _setup_private_group_with_pending(client)
    body = client.get(f"/local/groups/{g['id']}/pending_requests").json()
    requests = body["requests"]
    assert len(requests) == 1
    r = requests[0]
    assert r["id"] == rid
    assert r["joiner_pubkey"] == joiner_pub
    assert r["message"] == "let me in"
    assert r["joiner_address"] == "10.0.0.5:8001"
    assert r["status"] == "pending"


def test_list_pending_empty_for_open_group(client):
    g = client.post("/local/groups", json={"name": "Open"}).json()
    body = client.get(f"/local/groups/{g['id']}/pending_requests").json()
    assert body["requests"] == []


def test_list_pending_404_for_unknown_group(client):
    res = client.get("/local/groups/no-such/pending_requests")
    assert res.status_code == 404


# ---- approve ------------------------------------------------------------


def test_approve_returns_grant_and_marks_approved(client):
    g, _inv, joiner_pub, rid = _setup_private_group_with_pending(client)
    res = client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/approve"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["joiner_pubkey"] == joiner_pub
    assert body["grant_blob_b64"]
    # The row flipped on disk.
    listing = client.get(f"/local/groups/{g['id']}/pending_requests").json()
    statuses = [r["status"] for r in listing["requests"] if r["id"] == rid]
    assert statuses == ["approved"]


def test_approve_consumes_exactly_one_slot(client):
    g, inv, _pk, rid = _setup_private_group_with_pending(client, slot_cap=3)

    async def _read_slots():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInviteLink).where(
                        GroupInviteLink.token == inv["token"]
                    )
                )
            ).scalar_one()
            return row.slots_filled

    before = asyncio.run(_read_slots())
    assert before == 0
    client.post(f"/local/groups/{g['id']}/pending_requests/{rid}/approve")
    after = asyncio.run(_read_slots())
    assert after == 1


def test_approve_404_for_unknown_request(client):
    g = client.post(
        "/local/groups", json={"name": "C", "privacy_mode": "private"}
    ).json()
    res = client.post(
        f"/local/groups/{g['id']}/pending_requests/bogus/approve"
    )
    assert res.status_code == 404


def test_approve_409_when_already_decided(client):
    g, _inv, _pk, rid = _setup_private_group_with_pending(client)
    client.post(f"/local/groups/{g['id']}/pending_requests/{rid}/approve")
    res2 = client.post(f"/local/groups/{g['id']}/pending_requests/{rid}/approve")
    assert res2.status_code == 409


# ---- reject -------------------------------------------------------------


def test_reject_marks_rejected_and_stores_reason(client):
    g, _inv, _pk, rid = _setup_private_group_with_pending(client)
    res = client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/reject",
        json={"reason": "not a fit right now"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "not a fit right now"
    listing = client.get(f"/local/groups/{g['id']}/pending_requests").json()
    row = [r for r in listing["requests"] if r["id"] == rid][0]
    assert row["status"] == "rejected"
    assert row["decision_reason"] == "not a fit right now"


def test_reject_does_not_consume_slot(client):
    g, inv, _pk, rid = _setup_private_group_with_pending(client, slot_cap=2)

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

    before = asyncio.run(_read_slots())
    client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/reject", json={}
    )
    after = asyncio.run(_read_slots())
    assert before == (0, 1)
    assert after == (0, 1)


def test_reject_409_when_already_decided(client):
    g, _inv, _pk, rid = _setup_private_group_with_pending(client)
    client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/reject", json={}
    )
    res2 = client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/reject", json={}
    )
    assert res2.status_code == 409


def test_reject_then_approve_409(client):
    """Once a row is rejected we can't flip it to approved."""
    g, _inv, _pk, rid = _setup_private_group_with_pending(client)
    client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/reject", json={}
    )
    res = client.post(
        f"/local/groups/{g['id']}/pending_requests/{rid}/approve"
    )
    assert res.status_code == 409
