"""Wave 16.4 — admin → joiner decision delivery + idempotent inbound handler.

Three units exercised here:

1. The inbound /peer/group/join_decision handler on the joiner side
   (materializes membership on 'approved', writes audit on 'rejected',
   idempotent against repeated deliveries).
2. The admin-side delivery helper (stamps delivered_at on success;
   leaves it empty + increments the in-memory counter on failure).
3. The sweep_pending_decisions retry primitive (skips rows past the
   30-min window, skips rows past the 5-attempt cap).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as local_router
from nexus.runtime import group_decisions
from nexus.runtime.group_decisions import (
    GROUP_DECISION_MAX_ATTEMPTS,
    attempt_deliver_one,
    sweep_pending_decisions,
)
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_grant import generate_keypair
from nexus.storage import database
from nexus.storage.models import (
    GroupGrant,
    GroupMember,
    GroupPendingJoinRequest,
)


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    # Reset the module-level attempt counter for each test.
    group_decisions._attempts.clear()

    db_path = tmp_path / "decisions.db"
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
    group_decisions._attempts.clear()


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


# ---- inbound /peer/group/join_decision ---------------------------------


def _mint_grant_for(group_id: str, member_pubkey: str) -> tuple[bytes, str, str]:
    """Mint a grant signed by a fresh keypair, return blob + issued/expires."""
    from nexus.security.group_grant import sign_grant
    priv, _pub = generate_keypair()
    issued_at = "2026-05-19T00:00:00+00:00"
    expires_at = "2026-05-20T00:00:00+00:00"
    blob = sign_grant(
        group_id=group_id,
        member_pubkey=member_pubkey,
        roles=("member",),
        admin_privkey=priv,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce="aabbccdd",
    )
    return blob, issued_at, expires_at


def test_inbound_approved_materializes_membership(client):
    me = group_keys.get_local_group_pubkey()
    blob, issued, expires = _mint_grant_for("g-remote", me)
    res = client.post(
        "/peer/group/join_decision",
        json={
            "request_id": "abc1234567",
            "group_id": "g-remote",
            "group_name": "RemoteGroup",
            "founder_pubkey": "f" * 64,
            "decision": "approved",
            "grant_blob_b64": base64.b64encode(blob).decode("ascii"),
            "default_role": "member",
            "issued_at": issued,
            "expires_at": expires,
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "applied": True, "decision": "approved"}

    async def _check():
        async with database.get_session() as s:
            # Joiner materializes two members: self + a founder stub.
            mems = (await s.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == "g-remote")
                    & (GroupMember.pubkey == me)
                )
            )).scalars().all()
            grant = (await s.execute(
                select(GroupGrant).where(GroupGrant.group_id == "g-remote")
            )).scalar_one()
            return mems[0].pubkey, bytes(grant.signature)

    member_pubkey, stored_blob = asyncio.run(_check())
    assert member_pubkey == me
    assert stored_blob == blob


def test_inbound_approved_is_idempotent_on_repeat(client):
    me = group_keys.get_local_group_pubkey()
    blob, issued, expires = _mint_grant_for("g-idem", me)
    payload = {
        "request_id": "idem1",
        "group_id": "g-idem",
        "group_name": "Idem",
        "founder_pubkey": "f" * 64,
        "decision": "approved",
        "grant_blob_b64": base64.b64encode(blob).decode("ascii"),
        "default_role": "member",
        "issued_at": issued,
        "expires_at": expires,
    }
    r1 = client.post("/peer/group/join_decision", json=payload).json()
    r2 = client.post("/peer/group/join_decision", json=payload).json()
    assert r1["applied"] is True
    assert r2["applied"] is False  # second call short-circuits

    async def _count():
        async with database.get_session() as s:
            rows = (await s.execute(
                select(GroupGrant).where(GroupGrant.group_id == "g-idem")
            )).scalars().all()
            return len(rows)

    assert asyncio.run(_count()) == 1


def test_inbound_rejected_writes_audit_no_membership(client):
    res = client.post(
        "/peer/group/join_decision",
        json={
            "request_id": "rej1",
            "group_id": "g-reject",
            "group_name": "Closed",
            "founder_pubkey": "f" * 64,
            "decision": "rejected",
            "reason": "not now",
        },
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True, "applied": True, "decision": "rejected"}

    async def _check():
        async with database.get_session() as s:
            rows = (await s.execute(
                select(GroupGrant).where(GroupGrant.group_id == "g-reject")
            )).scalars().all()
            return len(rows)

    assert asyncio.run(_check()) == 0


def test_inbound_rejects_unknown_decision_value(client):
    res = client.post(
        "/peer/group/join_decision",
        json={
            "request_id": "x",
            "group_id": "g",
            "founder_pubkey": "f" * 64,
            "decision": "hmm",
        },
    )
    assert res.status_code == 400


def test_inbound_approved_requires_grant_blob(client):
    res = client.post(
        "/peer/group/join_decision",
        json={
            "request_id": "x",
            "group_id": "g",
            "founder_pubkey": "f" * 64,
            "decision": "approved",
        },
    )
    assert res.status_code == 400


# ---- admin-side delivery helper ----------------------------------------


def test_attempt_deliver_one_stamps_delivered_at_on_success(isolated_db, monkeypatch):
    async def fake_post(addr, node_id, body, **_kw):
        return 200, {"ok": True}
    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _exercise():
        async with database.get_session() as s:
            row = GroupPendingJoinRequest(
                id="r1",
                group_id="g1",
                joiner_pubkey="p" * 64,
                joiner_address="10.0.0.5:8001",
                invite_token="t1",
                status="approved",
                created_at=datetime.now(timezone.utc).isoformat(),
                decided_at=datetime.now(timezone.utc).isoformat(),
            )
            s.add(row)
            await s.flush()
            ok = await attempt_deliver_one(
                s, row,
                group_name="G",
                founder_pubkey="f" * 64,
                grant_blob_b64="aGVsbG8=",
                default_role="member",
                issued_at="2026-05-19",
                expires_at="2026-05-20",
            )
            await s.commit()
            return ok, row.delivered_at

    ok, delivered_at = asyncio.run(_exercise())
    assert ok is True
    assert delivered_at  # non-empty timestamp


def test_attempt_deliver_one_failure_increments_attempts(isolated_db, monkeypatch):
    async def fake_post(addr, node_id, body, **_kw):
        return 503, {"error": "down"}
    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _exercise():
        async with database.get_session() as s:
            row = GroupPendingJoinRequest(
                id="r2",
                group_id="g2",
                joiner_pubkey="p" * 64,
                joiner_address="10.0.0.5:8001",
                invite_token="t2",
                status="approved",
                created_at=datetime.now(timezone.utc).isoformat(),
                decided_at=datetime.now(timezone.utc).isoformat(),
            )
            s.add(row)
            await s.flush()
            ok = await attempt_deliver_one(
                s, row,
                group_name="G",
                founder_pubkey="f" * 64,
            )
            return ok, row.delivered_at, group_decisions._attempts["r2"]

    ok, delivered_at, attempts = asyncio.run(_exercise())
    assert ok is False
    assert delivered_at == ""
    assert attempts == 1


def test_attempt_deliver_one_skips_when_no_joiner_address(isolated_db, monkeypatch):
    async def fake_post(addr, node_id, body, **_kw):
        raise AssertionError("should not be called")
    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _exercise():
        async with database.get_session() as s:
            row = GroupPendingJoinRequest(
                id="r3",
                group_id="g3",
                joiner_pubkey="p" * 64,
                joiner_address="",  # explicit empty
                invite_token="t3",
                status="rejected",
                created_at=datetime.now(timezone.utc).isoformat(),
                decided_at=datetime.now(timezone.utc).isoformat(),
            )
            s.add(row)
            await s.flush()
            return await attempt_deliver_one(
                s, row, group_name="G", founder_pubkey="f" * 64
            )

    assert asyncio.run(_exercise()) is False
    assert "r3" not in group_decisions._attempts  # not counted


# ---- sweep retry behavior ----------------------------------------------


def test_sweep_skips_rows_past_30_min_window(isolated_db, monkeypatch):
    async def fake_post(addr, node_id, body, **_kw):
        raise AssertionError("should not be called")
    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _seed_and_sweep():
        async with database.get_session() as s:
            stale = (
                datetime.now(timezone.utc) - timedelta(minutes=45)
            ).isoformat()
            s.add(
                GroupPendingJoinRequest(
                    id="stale",
                    group_id="g",
                    joiner_pubkey="p" * 64,
                    joiner_address="10.0.0.5:8001",
                    invite_token="t",
                    status="approved",
                    created_at=stale,
                    decided_at=stale,
                )
            )
            await s.flush()
            delivered = await sweep_pending_decisions(
                s,
                lookup_group_meta=lambda gid: ("G", "f" * 64),
                lookup_grant_meta=lambda r: ("aGk=", "member", "", ""),
            )
            return delivered

    assert asyncio.run(_seed_and_sweep()) == 0


def test_sweep_skips_rows_past_max_attempts(isolated_db, monkeypatch):
    async def fake_post(addr, node_id, body, **_kw):
        raise AssertionError("should not be called when attempts maxed")
    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _seed_and_sweep():
        async with database.get_session() as s:
            s.add(
                GroupPendingJoinRequest(
                    id="maxed",
                    group_id="g",
                    joiner_pubkey="p" * 64,
                    joiner_address="10.0.0.5:8001",
                    invite_token="t",
                    status="approved",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    decided_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            await s.flush()
            group_decisions._attempts["maxed"] = GROUP_DECISION_MAX_ATTEMPTS
            return await sweep_pending_decisions(
                s,
                lookup_group_meta=lambda gid: ("G", "f" * 64),
                lookup_grant_meta=lambda r: ("aGk=", "member", "", ""),
            )

    assert asyncio.run(_seed_and_sweep()) == 0


def test_sweep_delivers_eligible_row(isolated_db, monkeypatch):
    calls: list[str] = []

    async def fake_post(addr, node_id, body, **_kw):
        calls.append(addr)
        return 200, {"ok": True}

    monkeypatch.setattr(group_decisions, "_post_to_joiner", fake_post)

    async def _seed_and_sweep():
        async with database.get_session() as s:
            s.add(
                GroupPendingJoinRequest(
                    id="live",
                    group_id="g",
                    joiner_pubkey="p" * 64,
                    joiner_address="10.0.0.7:9000",
                    invite_token="t",
                    status="rejected",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    decided_at=datetime.now(timezone.utc).isoformat(),
                    decision_reason="no",
                )
            )
            await s.flush()
            return await sweep_pending_decisions(
                s,
                lookup_group_meta=lambda gid: ("G", "f" * 64),
                lookup_grant_meta=lambda r: ("", "member", "", ""),
            )

    assert asyncio.run(_seed_and_sweep()) == 1
    assert calls == ["10.0.0.7:9000"]
