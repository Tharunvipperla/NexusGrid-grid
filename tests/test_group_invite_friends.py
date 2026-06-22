"""Wave 16.5 — targeted invitation push.

Covers ``/local/groups/{id}/invite_friends`` (sender side),
``/peer/group/invitation_offer`` (recipient inbound),
``/peer/group/invitation_decline`` (sender inbound), the resend
endpoint, and the sent-listing.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api import groups as groups_module
from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as local_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database
from nexus.storage.models import (
    GroupInvitationOffer,
    GroupInviteLink,
    Peer,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "invite_friends.db"
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
    # Stub identity so we don't depend on the host's network.
    monkeypatch.setattr(
        groups_module, "get_node_identity", lambda: "10.0.0.99:8001"
    )
    app = FastAPI()
    app.include_router(local_router)
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_trusted_peer(ip: str, display_name: str = "") -> None:
    async def _add():
        async with database.get_session() as s:
            s.add(
                Peer(
                    ip=ip,
                    status="trusted",
                    role="dual",
                    display_name=display_name,
                    resolved_ip="",
                )
            )
            await s.commit()
    asyncio.run(_add())


def _stub_push(monkeypatch, sink: list, *, ok: bool = True, detail: str = "ok"):
    async def _fake(**kwargs):
        sink.append(kwargs)
        return ok, detail
    monkeypatch.setattr(groups_module, "_push_invitation_offer", _fake)


# ---- invite_friends -----------------------------------------------------


def test_invite_friends_mints_and_records_sender_row(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")

    sink: list = []
    _stub_push(monkeypatch, sink, ok=True)

    res = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["ok"] is True
    assert r["target_peer_label"] == "alice"
    assert r["token"]

    # Sender-side offer row exists.
    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        GroupInvitationOffer.role == "sender"
                    )
                )
            ).scalar_one()
            assert row.status == "pending"
            assert row.target_peer_label == "alice"
            # The mint token row was also created.
            invite = await s.get(GroupInviteLink, row.token)
            assert invite is not None
            assert int(invite.slot_cap or 0) == 1
    asyncio.run(_check())

    # The push helper was called with our identity.
    assert sink[0]["founder_address"] == "10.0.0.99:8001"
    assert sink[0]["peer_address"] == "10.0.0.5:8001"


def test_invite_friends_skips_non_trusted_peer(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    # Peer exists but is *not* trusted.
    async def _add():
        async with database.get_session() as s:
            s.add(Peer(ip="9.9.9.9:1", status="pending_out", role="dual"))
            await s.commit()
    asyncio.run(_add())

    sink: list = []
    _stub_push(monkeypatch, sink)

    res = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["9.9.9.9:1"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["results"][0]["ok"] is False
    assert "trusted" in body["results"][0]["detail"]
    # No invite minted, no push call.
    assert sink == []


def test_invite_friends_empty_list_400(client):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    res = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": []},
    )
    assert res.status_code == 400


def test_invite_friends_records_failure_when_push_fails(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")

    sink: list = []
    _stub_push(monkeypatch, sink, ok=False, detail="unreachable")

    res = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    )
    body = res.json()
    r = body["results"][0]
    assert r["ok"] is False
    assert r["detail"] == "unreachable"
    # Row was still persisted so the founder can resend later.
    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        GroupInvitationOffer.role == "sender"
                    )
                )
            ).scalar_one()
            assert row.status == "pending"
    asyncio.run(_check())


# ---- list sent ----------------------------------------------------------


def test_list_sent_returns_sender_rows_only(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")
    _stub_push(monkeypatch, [], ok=True)
    client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    )
    # Also add a recipient-side row directly — it must not appear.
    async def _add_recipient():
        async with database.get_session() as s:
            s.add(GroupInvitationOffer(
                token="other-tok",
                role="recipient",
                group_id=g["id"],
                status="pending",
                created_at="2026-01-01T00:00:00",
            ))
            await s.commit()
    asyncio.run(_add_recipient())

    body = client.get(f"/local/groups/{g['id']}/invitations/sent").json()
    assert len(body["offers"]) == 1
    assert body["offers"][0]["role"] == "sender"
    assert body["offers"][0]["target_peer_label"] == "alice"


# ---- /peer/group/invitation_offer (inbound) ----------------------------


def test_peer_invitation_offer_creates_recipient_row(client):
    res = client.post(
        "/peer/group/invitation_offer",
        json={
            "token": "tkn-abc",
            "group_id": "g-1",
            "group_name": "Cool Crew",
            "founder_pubkey": "fpk",
            "founder_address": "10.0.0.1:8000",
            "target_peer_label": "bob",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-abc")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalar_one()
            assert row.status == "pending"
            assert row.group_name == "Cool Crew"
            assert row.founder_address == "10.0.0.1:8000"
    asyncio.run(_check())


def test_peer_invitation_offer_idempotent_on_resend(client):
    body = {
        "token": "tkn-x",
        "group_id": "g-2",
        "group_name": "Squad",
        "founder_pubkey": "fpk",
        "founder_address": "10.0.0.1:8000",
        "target_peer_label": "bob",
    }
    client.post("/peer/group/invitation_offer", json=body)
    # Simulate the recipient locally rejecting (status flipped) then a
    # resend arriving — should flip back to pending.
    async def _mark_rejected():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-x")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalar_one()
            row.status = "rejected"
            row.responded_at = "2026-01-01T00:00:00"
            await s.commit()
    asyncio.run(_mark_rejected())

    res = client.post("/peer/group/invitation_offer", json=body)
    assert res.status_code == 200

    async def _check():
        async with database.get_session() as s:
            rows = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == "tkn-x")
                        & (GroupInvitationOffer.role == "recipient")
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == "pending"
            assert rows[0].responded_at == ""
    asyncio.run(_check())


# ---- /peer/group/invitation_decline (inbound on founder) ---------------


def test_peer_invitation_decline_flips_sender_row(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")
    _stub_push(monkeypatch, [], ok=True)
    out = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    ).json()
    token = out["results"][0]["token"]

    res = client.post(
        "/peer/group/invitation_decline", json={"token": token}
    )
    assert res.status_code == 200

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == token)
                        & (GroupInvitationOffer.role == "sender")
                    )
                )
            ).scalar_one()
            assert row.status == "rejected"
            assert row.responded_at
            # Token itself NOT consumed — the underlying invite row still
            # has slots_filled == 0 so a resend is possible.
            invite = await s.get(GroupInviteLink, token)
            assert int(invite.slots_filled or 0) == 0
    asyncio.run(_check())


def test_peer_invitation_decline_unknown_404(client):
    res = client.post(
        "/peer/group/invitation_decline", json={"token": "nope"}
    )
    assert res.status_code == 404


# ---- /resend ------------------------------------------------------------


def test_resend_re_pushes_same_token_and_flips_pending(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")
    sink: list = []
    _stub_push(monkeypatch, sink, ok=True)

    out = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    ).json()
    token = out["results"][0]["token"]
    # Recipient declines.
    client.post("/peer/group/invitation_decline", json={"token": token})

    sink.clear()
    res = client.post(
        f"/local/groups/{g['id']}/invitations/{token}/resend"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    # Push helper called again with the SAME token.
    assert sink[0]["token"] == token

    async def _check():
        async with database.get_session() as s:
            row = (
                await s.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.token == token)
                        & (GroupInvitationOffer.role == "sender")
                    )
                )
            ).scalar_one()
            assert row.status == "pending"
            assert row.responded_at == ""
    asyncio.run(_check())


def test_resend_404_for_unknown_token(client):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    res = client.post(
        f"/local/groups/{g['id']}/invitations/nope/resend"
    )
    assert res.status_code == 404


def test_resend_reports_peer_no_longer_trusted(client, monkeypatch):
    g = client.post("/local/groups", json={"name": "G1"}).json()
    _seed_trusted_peer("10.0.0.5:8001", display_name="alice")
    _stub_push(monkeypatch, [], ok=True)
    out = client.post(
        f"/local/groups/{g['id']}/invite_friends",
        json={"peer_ips": ["10.0.0.5:8001"]},
    ).json()
    token = out["results"][0]["token"]

    # Drop the peer's trusted status.
    async def _untrust():
        async with database.get_session() as s:
            p = await s.get(Peer, "10.0.0.5:8001")
            p.status = "pending_out"
            await s.commit()
    asyncio.run(_untrust())

    res = client.post(
        f"/local/groups/{g['id']}/invitations/{token}/resend"
    )
    assert res.status_code == 200
    assert res.json()["ok"] is False
