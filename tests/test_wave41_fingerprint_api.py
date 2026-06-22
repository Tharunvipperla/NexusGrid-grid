"""Wave 41 — fingerprint freeze / propose / accept endpoints."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.groups import router as groups_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_keys import get_local_group_pubkey
from nexus.storage import database, get_session
from nexus.storage.models import (
    Group,
    GroupMember,
    GroupMemberRole,
    GroupRelayCodeprintProposal,
)
from nexus.utils.time import iso_now


FP_GOOD = "abcdef0123456789abcdef0123456789"
FP_OTHER = "0123456789abcdef0123456789abcdef"


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
    res = client.post("/local/groups", json={"name": "g"})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _swap_founder(group_id: str, new_founder: str) -> None:
    """Re-write Group.founder_pubkey to demote the local node from
    founder → admin, so we can exercise the propose path with the same
    local key. The local row already has the relay:host perm via the
    founder role; that's fine for the test."""
    async def _go():
        async with get_session() as s:
            g = await s.get(Group, group_id)
            g.founder_pubkey = new_founder
            await s.commit()
    asyncio.run(_go())


def test_founder_can_freeze_fingerprint(client):
    gid = _create_group(client)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint",
        json={"fingerprint": FP_GOOD},
    )
    assert res.status_code == 200, res.text
    assert res.json()["fingerprint"] == FP_GOOD

    async def _check():
        async with get_session() as s:
            g = await s.get(Group, gid)
            return g.relay_code_fingerprint
    assert asyncio.run(_check()) == FP_GOOD


def test_founder_can_clear_fingerprint(client):
    gid = _create_group(client)
    client.post(
        f"/local/groups/{gid}/relays/code_fingerprint",
        json={"fingerprint": FP_GOOD},
    )
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint",
        json={"fingerprint": ""},
    )
    assert res.status_code == 200, res.text
    assert res.json()["fingerprint"] == ""


def test_non_founder_cannot_set_directly(client):
    gid = _create_group(client)
    _swap_founder(gid, "ff" * 32)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint",
        json={"fingerprint": FP_GOOD},
    )
    assert res.status_code == 403


def test_admin_can_propose(client):
    gid = _create_group(client)
    _swap_founder(gid, "ff" * 32)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending"
    assert body["proposal_id"]


def test_founder_cannot_use_propose(client):
    gid = _create_group(client)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    )
    assert res.status_code == 400


def test_founder_accepts_proposal(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    # Demote to admin to file a proposal …
    _swap_founder(gid, "ff" * 32)
    proposal_id = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    # … then become founder again and accept.
    _swap_founder(gid, me)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "accepted"
    assert body["fingerprint"] == FP_GOOD

    async def _check():
        async with get_session() as s:
            g = await s.get(Group, gid)
            row = await s.get(GroupRelayCodeprintProposal, proposal_id)
            return g.relay_code_fingerprint, row.status
    fp, status = asyncio.run(_check())
    assert fp == FP_GOOD
    assert status == "accepted"


def test_founder_rejects_proposal(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    _swap_founder(gid, "ff" * 32)
    proposal_id = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    _swap_founder(gid, me)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
        "?decision=reject"
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "rejected"

    async def _check():
        async with get_session() as s:
            g = await s.get(Group, gid)
            return g.relay_code_fingerprint
    # Rejected proposals leave fingerprint untouched.
    assert asyncio.run(_check()) == ""


def test_non_founder_cannot_accept(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    _swap_founder(gid, "ff" * 32)
    proposal_id = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    # Stay non-founder this time.
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
    )
    assert res.status_code == 403


def test_proposal_idempotency_blocks_double_accept(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    _swap_founder(gid, "ff" * 32)
    proposal_id = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    _swap_founder(gid, me)
    first = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
    )
    assert first.status_code == 200
    second = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
    )
    assert second.status_code == 409


def test_list_proposals_returns_only_pending(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    _swap_founder(gid, "ff" * 32)
    p1 = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    p2 = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_OTHER},
    ).json()["proposal_id"]
    _swap_founder(gid, me)
    # Reject the first.
    client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{p1}?decision=reject"
    )
    res = client.get(
        f"/local/groups/{gid}/relays/code_fingerprint/proposals"
    )
    assert res.status_code == 200, res.text
    ids = [p["id"] for p in res.json()["proposals"]]
    assert ids == [p2]


def test_invalid_fingerprint_format_rejected(client):
    gid = _create_group(client)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint",
        json={"fingerprint": "not-hex"},
    )
    assert res.status_code == 422


def test_decision_must_be_accept_or_reject(client):
    gid = _create_group(client)
    me = get_local_group_pubkey()
    _swap_founder(gid, "ff" * 32)
    proposal_id = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/propose",
        json={"fingerprint": FP_GOOD},
    ).json()["proposal_id"]
    _swap_founder(gid, me)
    res = client.post(
        f"/local/groups/{gid}/relays/code_fingerprint/accept/{proposal_id}"
        "?decision=bogus"
    )
    assert res.status_code == 400
