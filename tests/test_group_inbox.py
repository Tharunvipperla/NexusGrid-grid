"""Wave 20 — replicated pending-join-request inbox.

Covers the three seams added this wave:

* :func:`apply_pending_request` / :func:`apply_pending_decision` —
  the local-state mutators, including idempotency + first-approver-wins.
* :func:`dispatch_inbound_frame` — the full crypto/auth path.
* the ``/peer/group/event`` endpoint that wraps it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as groups_router
from nexus.runtime.group_inbox import (
    FRAME_PENDING_DECISION,
    FRAME_PENDING_REQUEST,
    apply_pending_decision,
    apply_pending_request,
    dispatch_inbound_frame,
)
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_frame import OpenedFrame, seal_frame
from nexus.security.group_grant import Grant, generate_keypair, sign_grant
from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.storage import database, get_session
from nexus.storage.models import Group, GroupPendingJoinRequest
from nexus.utils.time import iso_now


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
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


# ---- helpers ------------------------------------------------------------


def _future_iso(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _dummy_grant(group_id: str) -> Grant:
    """A Grant for hand-built OpenedFrames. ``apply_*`` only reads
    ``opened.payload`` + ``opened.channel``, so the grant is inert."""
    return Grant(
        group_id=group_id,
        member_pubkey="",
        roles=(),
        issued_by_pubkey="",
        issued_at="",
        expires_at="",
        nonce="",
    )


def _opened(channel: str, frame_type: str, payload: dict) -> OpenedFrame:
    return OpenedFrame(
        frame_id=secrets.token_hex(8),
        channel=channel,
        frame_type=frame_type,
        sender_pubkey="",
        sender_grant=_dummy_grant(channel),
        payload=json.dumps(payload).encode("utf-8"),
    )


def _request_payload(group_id: str, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "group_id": group_id,
        "joiner_pubkey": "aa" * 32,
        "joiner_address": "joiner.example:8443",
        "joiner_x25519_pub": "",
        "invite_token": "tok_" + secrets.token_hex(4),
        "message": "let me in",
        "display_name": "Joiner",
        "created_at": iso_now(),
    }


def _decision_payload(group_id: str, request_id: str, status: str, by: str) -> dict:
    return {
        "request_id": request_id,
        "group_id": group_id,
        "joiner_pubkey": "aa" * 32,
        "status": status,
        "decided_at": iso_now(),
        "decided_by_pubkey": by,
        "decision_reason": "spam" if status == "rejected" else "",
    }


def _mint_symkey(group_id: str) -> bytes:
    """Mint + self-seal a symkey onto the group row; return the plaintext."""
    symkey = mint_group_symkey()

    async def _store():
        async with get_session() as s:
            g = await s.get(Group, group_id)
            g.group_symkey_enc = ecies_seal(
                symkey, derive_x25519_pubkey_hex(get_local_group_privkey())
            )
            await s.commit()

    asyncio.run(_store())
    return symkey


def _founder_grant(group_id: str) -> bytes:
    """A grant the founder signs for themselves — verifies against the
    founder/admin set that ``create_group`` populates."""
    return sign_grant(
        group_id=group_id,
        member_pubkey=get_local_group_pubkey(),
        roles=("founder",),
        admin_privkey=get_local_group_privkey(),
        issued_at=iso_now(),
        expires_at=_future_iso(),
        nonce=secrets.token_hex(16),
    )


def _sealed(group_id: str, frame_type: str, payload: dict, symkey: bytes):
    return seal_frame(
        channel=group_id,
        frame_type=frame_type,
        payload=json.dumps(payload).encode("utf-8"),
        symkey=symkey,
        sender_grant_blob=_founder_grant(group_id),
        sender_privkey_hex=get_local_group_privkey(),
    )


def _get_pending(request_id: str):
    async def _q():
        async with get_session() as s:
            return await s.get(GroupPendingJoinRequest, request_id)

    return asyncio.run(_q())


# ---- apply_pending_request ---------------------------------------------


def test_apply_pending_request_inserts_row(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    rid = secrets.token_hex(8)
    payload = _request_payload(gid, rid)
    applied = asyncio.run(
        apply_pending_request(_opened(gid, FRAME_PENDING_REQUEST, payload))
    )
    assert applied is True
    row = _get_pending(rid)
    assert row is not None
    assert row.status == "pending"
    assert row.joiner_pubkey == payload["joiner_pubkey"]
    assert row.invite_token == payload["invite_token"]


def test_apply_pending_request_is_idempotent(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    rid = secrets.token_hex(8)
    payload = _request_payload(gid, rid)
    first = asyncio.run(
        apply_pending_request(_opened(gid, FRAME_PENDING_REQUEST, payload))
    )
    second = asyncio.run(
        apply_pending_request(_opened(gid, FRAME_PENDING_REQUEST, payload))
    )
    assert first is True
    assert second is False  # already mirrored


def test_apply_pending_request_rejects_group_id_mismatch(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    rid = secrets.token_hex(8)
    # Inner payload group_id deliberately disagrees with the channel.
    payload = _request_payload("grp_other", rid)
    applied = asyncio.run(
        apply_pending_request(_opened(gid, FRAME_PENDING_REQUEST, payload))
    )
    assert applied is False
    assert _get_pending(rid) is None


# ---- apply_pending_decision --------------------------------------------


def _seed_pending(gid: str, rid: str) -> None:
    asyncio.run(
        apply_pending_request(
            _opened(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid))
        )
    )


def test_apply_pending_decision_flips_status(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    rid = secrets.token_hex(8)
    _seed_pending(gid, rid)
    applied = asyncio.run(
        apply_pending_decision(
            _opened(
                gid,
                FRAME_PENDING_DECISION,
                _decision_payload(gid, rid, "approved", "admin-a"),
            )
        )
    )
    assert applied is True
    assert _get_pending(rid).status == "approved"


def test_apply_pending_decision_first_approver_wins(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    rid = secrets.token_hex(8)
    _seed_pending(gid, rid)
    first = asyncio.run(
        apply_pending_decision(
            _opened(
                gid,
                FRAME_PENDING_DECISION,
                _decision_payload(gid, rid, "approved", "admin-a"),
            )
        )
    )
    # A second, conflicting decision arrives — the row is already
    # terminal, so it is a no-op. First approver wins.
    second = asyncio.run(
        apply_pending_decision(
            _opened(
                gid,
                FRAME_PENDING_DECISION,
                _decision_payload(gid, rid, "rejected", "admin-b"),
            )
        )
    )
    assert first is True
    assert second is False
    row = _get_pending(rid)
    assert row.status == "approved"
    assert row.decided_by_pubkey == "admin-a"


def test_apply_pending_decision_drops_unknown_request(isolated_db):
    gid = "grp_" + secrets.token_hex(6)
    applied = asyncio.run(
        apply_pending_decision(
            _opened(
                gid,
                FRAME_PENDING_DECISION,
                _decision_payload(gid, "never-seen", "approved", ""),
            )
        )
    )
    assert applied is False


# ---- dispatch_inbound_frame (end-to-end crypto) ------------------------


def test_dispatch_mirrors_a_sealed_request(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True
    assert _get_pending(rid) is not None


def test_dispatch_dedupes_replayed_frame(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_PENDING_REQUEST, _request_payload(gid, secrets.token_hex(8)), symkey
    )
    envelope = frame.to_dict()
    first = asyncio.run(dispatch_inbound_frame(envelope))
    second = asyncio.run(dispatch_inbound_frame(envelope))
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["reason"] == "duplicate"


def test_dispatch_rejects_frame_from_non_admin(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    # Grant signed by a stranger keypair — not in the group's admin set.
    stranger_priv, stranger_pub = generate_keypair()
    bad_grant = sign_grant(
        group_id=gid,
        member_pubkey=stranger_pub,
        roles=("member",),
        admin_privkey=stranger_priv,
        issued_at=iso_now(),
        expires_at=_future_iso(),
        nonce=secrets.token_hex(16),
    )
    frame = seal_frame(
        channel=gid,
        frame_type=FRAME_PENDING_REQUEST,
        payload=json.dumps(_request_payload(gid, secrets.token_hex(8))).encode("utf-8"),
        symkey=symkey,
        sender_grant_blob=bad_grant,
        sender_privkey_hex=stranger_priv,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is False
    assert result["reason"].startswith("verify")


def test_dispatch_without_symkey_is_rejected(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    # Channel exists but no symkey has been minted on this node.
    envelope = {
        "frame_id": secrets.token_hex(8),
        "channel": gid,
        "frame_type": FRAME_PENDING_REQUEST,
        "sender_grant_b64": "AAAA",
        "nonce_b64": "AAAA",
        "ciphertext_b64": "AAAA",
        "signature_b64": "AAAA",
    }
    result = asyncio.run(dispatch_inbound_frame(envelope))
    assert result["ok"] is False
    assert result["reason"] == "no symkey for channel"


def test_dispatch_decision_flips_mirrored_row(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)

    req_frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    asyncio.run(dispatch_inbound_frame(req_frame.to_dict()))

    dec_frame = _sealed(
        gid,
        FRAME_PENDING_DECISION,
        _decision_payload(gid, rid, "rejected", get_local_group_pubkey()),
        symkey,
    )
    result = asyncio.run(dispatch_inbound_frame(dec_frame.to_dict()))
    assert result["applied"] is True
    row = _get_pending(rid)
    assert row.status == "rejected"
    assert row.decision_reason == "spam"


# ---- /peer/group/event endpoint ----------------------------------------


def test_event_endpoint_applies_a_sealed_request(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    res = client.post("/peer/group/event", json=frame.to_dict())
    assert res.status_code == 200, res.text
    assert res.json()["applied"] is True
    assert _get_pending(rid) is not None


def test_event_endpoint_drops_unverifiable_frame(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)
    res = client.post(
        "/peer/group/event",
        json={
            "frame_id": secrets.token_hex(8),
            "channel": gid,
            "frame_type": FRAME_PENDING_REQUEST,
            "sender_grant_b64": "not-valid-base64!!!",
            "nonce_b64": "AAAA",
            "ciphertext_b64": "AAAA",
            "signature_b64": "AAAA",
        },
    )
    # A bad frame must never 500 — it is dropped, reported in the body.
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is False
