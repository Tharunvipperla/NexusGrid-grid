"""Wave 16.6 — joiner-side incoming invitations.

Covers ``/local/invitations/incoming`` listing, accept (which calls the
existing /local/groups/join path under the hood), and reject (which
pushes /peer/group/invitation_decline back to the founder).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api import groups as groups_module
from nexus.api.group_peer import router as peer_router
from nexus.api.groups import invitations_router, router as local_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database
from nexus.storage.models import GroupInvitationOffer


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "incoming.db"
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
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(
        groups_module, "get_node_identity", lambda: "10.0.0.99:8001"
    )
    app = FastAPI()
    app.include_router(local_router)
    app.include_router(invitations_router)
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_recipient_offer(
    *,
    token: str = "tkn-1",
    group_id: str = "g-1",
    founder_address: str = "10.0.0.1:8000",
    status: str = "pending",
) -> None:
    async def _add():
        async with database.get_session() as s:
            s.add(GroupInvitationOffer(
                token=token,
                role="recipient",
                group_id=group_id,
                group_name="Cool Crew",
                founder_pubkey="fpk",
                founder_address=founder_address,
                target_peer_label="me",
                status=status,
                created_at="2026-01-01T00:00:00",
            ))
            await s.commit()
    asyncio.run(_add())


# ---- /incoming ----------------------------------------------------------


def test_incoming_lists_only_pending_recipient_rows(client):
    _seed_recipient_offer(token="t1", status="pending")
    _seed_recipient_offer(token="t2", status="accepted")
    _seed_recipient_offer(token="t3", status="rejected")
    # Add a sender-side row that must not appear.
    async def _add_sender():
        async with database.get_session() as s:
            s.add(GroupInvitationOffer(
                token="t-sender",
                role="sender",
                group_id="g-2",
                status="pending",
                created_at="2026-01-01T00:00:00",
            ))
            await s.commit()
    asyncio.run(_add_sender())

    body = client.get("/local/invitations/incoming").json()
    tokens_returned = {o["token"] for o in body["offers"]}
    assert tokens_returned == {"t1"}


def test_incoming_empty_when_none(client):
    body = client.get("/local/invitations/incoming").json()
    assert body == {"offers": []}


# ---- accept -------------------------------------------------------------


def test_accept_calls_post_join_group_and_flips_row(client, monkeypatch):
    _seed_recipient_offer(token="tkn-A", founder_address="10.0.0.1:8000")

    seen: dict = {}

    async def _fake_join(body):
        seen["admin_address"] = body.admin_address
        seen["invite_token"] = body.invite_token
        seen["joiner_address"] = body.joiner_address
        return {
            "group_id": body.invite_token,
            "group_name": "Cool Crew",
            "my_role": "member",
        }

    monkeypatch.setattr(groups_module, "post_join_group", _fake_join)

    res = client.post("/local/invitations/tkn-A/accept")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["join_result"]["my_role"] == "member"
    assert seen["admin_address"] == "10.0.0.1:8000"
    assert seen["invite_token"] == "tkn-A"
    assert seen["joiner_address"] == "10.0.0.99:8001"

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-A")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalar_one()
            assert row.status == "accepted"
            assert row.responded_at
    asyncio.run(_check())


def test_accept_404_for_unknown_token(client):
    res = client.post("/local/invitations/nope/accept")
    assert res.status_code == 404


def test_accept_409_when_already_decided(client):
    _seed_recipient_offer(token="tkn-X", status="accepted")
    res = client.post("/local/invitations/tkn-X/accept")
    assert res.status_code == 409


def test_accept_400_when_no_founder_address(client):
    _seed_recipient_offer(token="tkn-Y", founder_address="")
    res = client.post("/local/invitations/tkn-Y/accept")
    assert res.status_code == 400


# ---- reject -------------------------------------------------------------


def test_reject_pushes_decline_and_flips_row(client, monkeypatch):
    _seed_recipient_offer(token="tkn-R")

    seen: dict = {}

    async def _fake_post(
        address, path, body, admin_node_id="",
        link_relay_urls=None, link_grid_key="",
    ):
        seen["address"] = address
        seen["path"] = path
        seen["body"] = body
        return 200, {"ok": True}

    monkeypatch.setattr(groups_module, "_post_to_admin", _fake_post)

    res = client.post("/local/invitations/tkn-R/reject")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["pushed_ok"] is True
    assert seen["address"] == "10.0.0.1:8000"
    assert seen["path"] == "/peer/group/invitation_decline"
    assert seen["body"] == {"token": "tkn-R"}

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-R")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalar_one()
            assert row.status == "rejected"
            assert row.responded_at
    asyncio.run(_check())


def test_reject_still_flips_local_when_push_fails(client, monkeypatch):
    _seed_recipient_offer(token="tkn-R2")

    async def _fake_post(
        address, path, body, admin_node_id="",
        link_relay_urls=None, link_grid_key="",
    ):
        return 503, {"error": "unreachable"}

    monkeypatch.setattr(groups_module, "_post_to_admin", _fake_post)

    res = client.post("/local/invitations/tkn-R2/reject")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["pushed_ok"] is False
    assert "unreachable" in body["detail"]

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-R2")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalar_one()
            assert row.status == "rejected"
    asyncio.run(_check())


def test_reject_404_for_unknown_token(client):
    res = client.post("/local/invitations/nope/reject")
    assert res.status_code == 404


def test_reject_409_when_already_decided(client):
    _seed_recipient_offer(token="tkn-X", status="rejected")
    res = client.post("/local/invitations/tkn-X/reject")
    assert res.status_code == 409
