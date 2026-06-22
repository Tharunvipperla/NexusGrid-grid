"""Wave 21 — per-group relay channel + relay:host enforcement.

Covers:

* the ``/peer/group/publish`` endpoint's ``relay:host`` gate,
* :func:`relay_inbound_frame` — verify, apply-if-audience, fan out,
* :func:`publish_frame` — route via relay hosts, direct-fanout fallback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as groups_router
from nexus.runtime.group_inbox import (
    FRAME_PENDING_REQUEST,
    FRAME_USAGE_RECEIPT,
    FRAME_PRESENCE_BEACON,
    FRAME_RELAY_UPDATE,
    FRAME_ROSTER_UPDATE,
    FRAME_SYMKEY_ROTATE,
    dispatch_inbound_frame,
    publish_frame,
    publish_pending_request,
    publish_relay_update,
    publish_roster_update,
    publish_symkey_rotate,
    relay_inbound_frame,
)
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_open,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_frame import (
    FrameVerificationError,
    GroupFrame,
    open_frame,
    seal_frame,
)
from nexus.security.group_grant import generate_keypair, sign_grant
from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.storage import database, get_session
from nexus.storage.models import (
    Group,
    GroupMember,
    GroupMemberRole,
    GroupPendingJoinRequest,
    GroupRelayBinding,
    GroupRole,
)
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


class _RecordingPoster:
    """Stub for the ``PosterFn`` seam — records calls, returns a status
    keyed by path (default 200)."""

    def __init__(self, status_by_path: dict[str, int] | None = None):
        self.status_by_path = status_by_path or {}
        self.calls: list[dict] = []

    async def __call__(
        self, peer_address: str, node_id: str, path: str, body: dict
    ):
        self.calls.append(
            {
                "address": peer_address,
                "node_id": node_id,
                "path": path,
                "body": body,
            }
        )
        return self.status_by_path.get(path, 200), {"ok": True}

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]


def _request_payload(group_id: str, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "group_id": group_id,
        "joiner_pubkey": "aa" * 32,
        "joiner_address": "joiner.example:8443",
        "joiner_x25519_pub": "",
        "invite_token": "tok_" + secrets.token_hex(4),
        "message": "",
        "display_name": "Joiner",
        "created_at": iso_now(),
    }


def _mint_symkey(group_id: str) -> bytes:
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


def _add_member(
    group_id: str,
    pubkey: str,
    role_name: str,
    peer_address: str,
    node_id: str = "",
    member_x25519_pub: str = "",
) -> None:
    """Insert a second group member holding *role_name* (a default role
    installed by create_group: founder / admin / member)."""

    async def _ins():
        async with get_session() as s:
            s.add(
                GroupMember(
                    group_id=group_id,
                    pubkey=pubkey,
                    joined_at=iso_now(),
                    last_heartbeat_at="",
                    display_name="",
                    peer_address=peer_address,
                    node_id=node_id,
                    member_x25519_pub=member_x25519_pub,
                )
            )
            s.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=pubkey,
                    role_name=role_name,
                    assigned_by_pubkey="",
                    assigned_at=iso_now(),
                )
            )
            await s.commit()

    asyncio.run(_ins())


def _get_pending(request_id: str):
    async def _q():
        async with get_session() as s:
            return await s.get(GroupPendingJoinRequest, request_id)

    return asyncio.run(_q())


def _envelope_dict(group_id: str) -> dict:
    """A shape-valid but cryptographically junk envelope."""
    return {
        "frame_id": secrets.token_hex(8),
        "channel": group_id,
        "frame_type": FRAME_PENDING_REQUEST,
        "sender_grant_b64": "AAAA",
        "nonce_b64": "AAAA",
        "ciphertext_b64": "AAAA",
        "signature_b64": "AAAA",
    }


# ---- /peer/group/publish endpoint gate ---------------------------------


def test_publish_endpoint_403_for_non_relay_host(client):
    # A channel this node has no role in → no relay:host → 403.
    res = client.post(
        "/peer/group/publish", json=_envelope_dict("grp_" + secrets.token_hex(6))
    )
    assert res.status_code == 403, res.text


def test_publish_endpoint_accepts_relay_host(client):
    # The founder holds relay:host (founder role) for their own group.
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    res = client.post("/peer/group/publish", json=frame.to_dict())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    # Founder is the audience for pending.* → applied locally.
    assert body["applied"] is True
    assert _get_pending(rid) is not None


# ---- relay_inbound_frame -----------------------------------------------


def test_relay_inbound_applies_locally_when_audience(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    result = asyncio.run(relay_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["relayed"] == 0  # single-node group, nobody to fan to
    assert _get_pending(rid) is not None


def test_relay_inbound_fans_out_to_other_admins(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "admin", "admin2.example:8443")
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_PENDING_REQUEST, _request_payload(gid, secrets.token_hex(8)), symkey
    )
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.paths() == ["/peer/group/event"]
    assert stub.calls[0]["address"] == "admin2.example:8443"


def test_relay_inbound_fans_out_presence_beacon(client):
    # Wave 47 regression: presence beacons must be in the relay fan-out
    # allowlist, else the relay drops them as "unknown frame_type" and no
    # member ever marks the sender online.
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "member", "m2.example:8443")
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_PRESENCE_BEACON, {"group_id": gid, "ts": iso_now()}, symkey
    )
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.paths() == ["/peer/group/event"]


def test_relay_inbound_fans_out_usage_receipt(client):
    # Wave 49 regression: usage.receipt frames must be in the relay fan-out
    # allowlist + member audience, else the shared "Pool usage" view never
    # converges for members behind a relay.
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "member", "m2.example:8443")
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_USAGE_RECEIPT,
        {"receipt": {"receipt_id": "r1"}, "sig": ""},
        symkey,
    )
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.paths() == ["/peer/group/event"]


def test_relay_inbound_dedupes_replay(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_PENDING_REQUEST, _request_payload(gid, secrets.token_hex(8)), symkey
    )
    envelope = frame.to_dict()
    first = asyncio.run(relay_inbound_frame(envelope))
    second = asyncio.run(relay_inbound_frame(envelope))
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["reason"] == "duplicate"


def test_relay_inbound_rejects_unverifiable_frame(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    # Grant signed by a stranger — not in the group's admin set.
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
        payload=b"{}",
        symkey=symkey,
        sender_grant_blob=bad_grant,
        sender_privkey_hex=stranger_priv,
    )
    result = asyncio.run(relay_inbound_frame(frame.to_dict()))
    assert result["ok"] is False
    assert result["reason"].startswith("verify")


def test_relay_inbound_rejects_unknown_frame_type(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    frame = _sealed(gid, "bogus.type", {"x": 1}, symkey)
    result = asyncio.run(relay_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["relayed"] == 0
    assert result["reason"].startswith("unknown frame_type")


# ---- publish_frame routing ---------------------------------------------


def _run_publish(group_id: str, poster, exclude: set[str]) -> dict:
    async def _go():
        async with get_session() as s:
            return await publish_frame(
                session=s,
                group_id=group_id,
                frame_type=FRAME_PENDING_REQUEST,
                payload_dict=_request_payload(group_id, secrets.token_hex(8)),
                exclude_pubkeys=exclude,
                poster=poster,
            )

    return asyncio.run(_go())


def test_publish_frame_routes_via_relay_host(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "admin", "relay1.example:8443")
    _mint_symkey(gid)
    stub = _RecordingPoster()
    summary = _run_publish(gid, stub, {get_local_group_pubkey()})
    assert summary["via"] == "relay"
    assert summary["delivered"] == 1
    assert stub.paths() == ["/peer/group/publish"]
    assert stub.calls[0]["address"] == "relay1.example:8443"


def test_publish_frame_falls_back_to_direct_when_relays_down(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    # Admin role holds both relay:host and group:approve.
    _add_member(gid, secrets.token_hex(32), "admin", "admin2.example:8443")
    _mint_symkey(gid)
    stub = _RecordingPoster(status_by_path={"/peer/group/publish": 503})
    summary = _run_publish(gid, stub, {get_local_group_pubkey()})
    assert summary["via"] == "direct-fallback"
    assert summary["delivered"] == 1
    # Tried the relay first, then fell back to direct fan-out.
    assert stub.paths() == ["/peer/group/publish", "/peer/group/event"]


def test_publish_frame_skips_without_symkey(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "admin", "admin2.example:8443")
    # No _mint_symkey — the channel has no key on this node.
    stub = _RecordingPoster()
    summary = _run_publish(gid, stub, {get_local_group_pubkey()})
    assert summary["skipped_no_symkey"] is True
    assert summary["published"] == 0
    assert stub.calls == []


def test_publish_pending_request_routes_through_relay(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "admin", "relay1.example:8443")
    _mint_symkey(gid)
    stub = _RecordingPoster()
    row = GroupPendingJoinRequest(
        id=secrets.token_hex(8),
        group_id=gid,
        joiner_pubkey="aa" * 32,
        joiner_address="joiner.example:8443",
        invite_token="tok_" + secrets.token_hex(4),
        message="",
        display_name="Joiner",
        joiner_x25519_pub="",
        status="pending",
        created_at=iso_now(),
    )

    async def _go():
        async with get_session() as s:
            return await publish_pending_request(s, row, poster=stub)

    summary = asyncio.run(_go())
    assert summary["via"] == "relay"
    assert stub.paths() == ["/peer/group/publish"]


# ---- Wave 22: relay-aware transport ------------------------------------


def test_poster_relay_fallback_when_no_address(monkeypatch):
    """A member reachable only by node_id → poster goes straight to the
    generic WS relay."""
    from nexus.runtime.group_inbox import _default_poster

    calls = []

    async def _fake_relay(node_id, method, path, body, **_kw):
        calls.append((node_id, method, path))
        return {"status": 200, "body": {"ok": True}}

    monkeypatch.setattr(
        "nexus.networking.relay_client.relay_http_request", _fake_relay
    )
    status, _body = asyncio.run(
        _default_poster("", "node-xyz", "/peer/group/event", {"k": 1})
    )
    assert status == 200
    assert calls == [("node-xyz", "POST", "/peer/group/event")]


def test_poster_relay_fallback_when_direct_fails(monkeypatch):
    """Direct HTTP to an unreachable address → fail over to the relay."""
    from nexus.runtime.group_inbox import _default_poster

    calls = []

    async def _fake_relay(node_id, method, path, body, **_kw):
        calls.append(node_id)
        return {"status": 202, "body": {}}

    monkeypatch.setattr(
        "nexus.networking.relay_client.relay_http_request", _fake_relay
    )
    # 127.0.0.1:1 — connection refused, fails fast on both schemes.
    status, _body = asyncio.run(
        _default_poster("127.0.0.1:1", "node-abc", "/peer/group/event", {})
    )
    assert status == 202
    assert calls == ["node-abc"]


def test_poster_unreachable_with_no_address_and_no_node_id():
    from nexus.runtime.group_inbox import _default_poster

    status, _body = asyncio.run(
        _default_poster("", "", "/peer/group/event", {})
    )
    assert status == 503


def test_roster_includes_node_id(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    res = client.post("/peer/group/roster", json={"group_id": gid})
    assert res.status_code == 200, res.text
    members = res.json()["members"]
    assert len(members) == 1
    # The founder's own row carries its node UUID for WS-relay routing.
    assert members[0]["node_id"]


def test_join_request_stores_joiner_node_id(client):
    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    _, joiner_pub = generate_keypair()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_node_id": "joiner-node-uuid-1",
        },
    )
    assert res.status_code == 200, res.text

    async def _member():
        async with get_session() as s:
            return await s.get(GroupMember, (group["id"], joiner_pub))

    m = asyncio.run(_member())
    assert m is not None
    assert m.node_id == "joiner-node-uuid-1"


def test_private_join_threads_node_id_to_approved_member(client):
    group = client.post(
        "/local/groups", json={"name": "g", "privacy_mode": "private"}
    ).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    joiner_priv, joiner_pub = generate_keypair()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_x25519_pub": derive_x25519_pubkey_hex(joiner_priv),
            "joiner_node_id": "joiner-node-uuid-2",
        },
    )
    assert res.status_code == 200, res.text
    request_id = res.json()["request_id"]

    async def _pending():
        async with get_session() as s:
            return await s.get(GroupPendingJoinRequest, request_id)

    assert asyncio.run(_pending()).joiner_node_id == "joiner-node-uuid-2"

    approve = client.post(
        f"/local/groups/{group['id']}/pending_requests/{request_id}/approve"
    )
    assert approve.status_code == 200, approve.text

    async def _member():
        async with get_session() as s:
            return await s.get(GroupMember, (group["id"], joiner_pub))

    m = asyncio.run(_member())
    assert m is not None
    assert m.node_id == "joiner-node-uuid-2"


def test_publish_frame_routes_to_node_id_only_relay_host(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    # A relay host reachable ONLY over the WS relay — no peer_address.
    _add_member(
        gid, secrets.token_hex(32), "admin", "", node_id="relay-node-1"
    )
    _mint_symkey(gid)
    stub = _RecordingPoster()
    summary = _run_publish(gid, stub, {get_local_group_pubkey()})
    assert summary["via"] == "relay"
    assert summary["delivered"] == 1
    assert stub.calls[0]["path"] == "/peer/group/publish"
    assert stub.calls[0]["node_id"] == "relay-node-1"
    assert stub.calls[0]["address"] == ""


def test_relayed_event_dispatch_applies_frame(client):
    """A group frame arriving over the WS relay is dispatched + applied."""
    from nexus.networking.relay_client import _handle_relayed_http_request

    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    payload = {
        "type": "http_request",
        "method": "POST",
        "path": "/peer/group/event",
        "body": frame.to_dict(),
        "request_id": "req-1",
    }
    asyncio.run(_handle_relayed_http_request("from-node-uuid", payload))
    assert _get_pending(rid) is not None


def test_relayed_publish_dispatch_through_relay_host(client):
    """A /peer/group/publish arriving over the WS relay passes the
    relay:host gate (the local founder holds it) and is fanned out."""
    from nexus.networking.relay_client import _handle_relayed_http_request

    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    rid = secrets.token_hex(8)
    frame = _sealed(gid, FRAME_PENDING_REQUEST, _request_payload(gid, rid), symkey)
    payload = {
        "type": "http_request",
        "method": "POST",
        "path": "/peer/group/publish",
        "body": frame.to_dict(),
        "request_id": "req-2",
    }
    asyncio.run(_handle_relayed_http_request("from-node-uuid", payload))
    assert _get_pending(rid) is not None


# ---- Wave 22.5: roster.update add-delta ---------------------------------


def _get_member(group_id: str, pubkey: str):
    async def _q():
        async with get_session() as s:
            return await s.get(GroupMember, (group_id, pubkey))

    return asyncio.run(_q())


def _member_roles(group_id: str, pubkey: str) -> list[str]:
    async def _q():
        async with get_session() as s:
            rows = (
                await s.execute(
                    select(GroupMemberRole.role_name).where(
                        (GroupMemberRole.group_id == group_id)
                        & (GroupMemberRole.member_pubkey == pubkey)
                    )
                )
            ).fetchall()
            return sorted(r[0] for r in rows)

    return asyncio.run(_q())


def _roster_payload(group_id: str, pubkey: str, **member) -> dict:
    return {
        "group_id": group_id,
        "action": "add",
        "member": {"pubkey": pubkey, **member},
    }


def test_dispatch_roster_update_upserts_member(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    new_pub = secrets.token_hex(32)
    frame = _sealed(
        gid,
        FRAME_ROSTER_UPDATE,
        _roster_payload(
            gid,
            new_pub,
            display_name="Newbie",
            joined_at=iso_now(),
            peer_address="newbie.example:8443",
            node_id="newbie-node-1",
        ),
        symkey,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True

    m = _get_member(gid, new_pub)
    assert m is not None
    assert m.node_id == "newbie-node-1"
    assert m.display_name == "Newbie"
    assert m.peer_address == "newbie.example:8443"


def test_roster_update_only_grants_member_role(client):
    """A roster.update can never escalate: the new pubkey always lands
    with exactly the default ``member`` role, never admin/founder."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    new_pub = secrets.token_hex(32)
    frame = _sealed(
        gid, FRAME_ROSTER_UPDATE, _roster_payload(gid, new_pub), symkey
    )
    asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert _member_roles(gid, new_pub) == ["member"]


def test_roster_update_audience_is_all_members(client):
    """``roster.update`` fans out to every member — including a plain
    ``member`` that a ``pending.*`` frame would skip."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "member", "member2.example:8443")
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid,
        FRAME_ROSTER_UPDATE,
        _roster_payload(gid, secrets.token_hex(32)),
        symkey,
    )
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.paths() == ["/peer/group/event"]
    assert stub.calls[0]["address"] == "member2.example:8443"


def test_publish_roster_update_builds_add_delta(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    target_pub = secrets.token_hex(32)
    _add_member(
        gid, target_pub, "member", "joined.example:8443", node_id="joined-node"
    )
    # An admin recipient so publish_frame has a relay host to hand to.
    _add_member(gid, secrets.token_hex(32), "admin", "admin2.example:8443")
    stub = _RecordingPoster()

    async def _go():
        async with get_session() as s:
            return await publish_roster_update(s, gid, target_pub, poster=stub)

    summary = asyncio.run(_go())
    assert summary["published"] == 1

    envelope = stub.calls[0]["body"]
    assert envelope["frame_type"] == FRAME_ROSTER_UPDATE
    opened = open_frame(
        GroupFrame.from_dict(envelope),
        symkey=symkey,
        group_admin_pubkeys=[get_local_group_pubkey()],
    )
    decoded = json.loads(opened.payload.decode("utf-8"))
    assert decoded["action"] == "add"
    assert decoded["member"]["pubkey"] == target_pub
    assert decoded["member"]["node_id"] == "joined-node"


def test_publish_roster_update_skips_missing_member(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)

    async def _go():
        async with get_session() as s:
            return await publish_roster_update(
                s, gid, secrets.token_hex(32), poster=_RecordingPoster()
            )

    summary = asyncio.run(_go())
    assert summary["published"] == 0


def test_open_join_triggers_roster_update_broadcast(client, monkeypatch):
    """An open-mode join publishes a roster.update for the new member."""
    calls: list[tuple[str, str]] = []

    async def _spy(session, group_id, member_pubkey, **kw):
        calls.append((group_id, member_pubkey))
        return {"published": 1}

    monkeypatch.setattr(
        "nexus.runtime.group_inbox.publish_roster_update", _spy
    )
    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    _, joiner_pub = generate_keypair()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_node_id": "joiner-node-9",
        },
    )
    assert res.status_code == 200, res.text
    assert calls == [(group["id"], joiner_pub)]


def test_private_approve_triggers_roster_update_broadcast(client, monkeypatch):
    """A private-mode approval publishes a roster.update so peer admins —
    who only mirror the pending row — also materialize the GroupMember."""
    calls: list[tuple[str, str]] = []

    async def _spy(session, group_id, member_pubkey, **kw):
        calls.append((group_id, member_pubkey))
        return {"published": 1}

    monkeypatch.setattr(
        "nexus.runtime.group_inbox.publish_roster_update", _spy
    )
    group = client.post(
        "/local/groups", json={"name": "g", "privacy_mode": "private"}
    ).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    joiner_priv, joiner_pub = generate_keypair()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_x25519_pub": derive_x25519_pubkey_hex(joiner_priv),
            "joiner_node_id": "joiner-node-10",
        },
    )
    request_id = res.json()["request_id"]
    approve = client.post(
        f"/local/groups/{group['id']}/pending_requests/{request_id}/approve"
    )
    assert approve.status_code == 200, approve.text
    # Wave 41 follow-up: the delivery + fan-out moved to a background
    # task so the API returns immediately. Give the event loop a beat
    # to drain that task before asserting on the spy.
    import time as _time
    for _ in range(40):
        if (group["id"], joiner_pub) in calls:
            break
        _time.sleep(0.05)
    assert (group["id"], joiner_pub) in calls


# ---- Wave 23: symkey rotation on kick -----------------------------------


def _read_local_symkey(group_id: str) -> bytes | None:
    """Open this node's stored ``Group.group_symkey_enc``."""

    async def _q():
        async with get_session() as s:
            g = await s.get(Group, group_id)
            if g is None or not g.group_symkey_enc:
                return None
            return ecies_open(
                bytes(g.group_symkey_enc), get_local_group_privkey()
            )

    return asyncio.run(_q())


def _rotate_frame(
    group_id: str,
    old_symkey: bytes,
    kicked_pubkey: str,
    new_symkey: bytes,
    envelope_for: list[tuple[str, str]] | None = None,
):
    """Build a founder-sealed ``symkey.rotate`` frame.

    ``envelope_for`` is a list of ``(member_pubkey, member_x25519_pub)``
    the new symkey is ECIES-sealed to."""
    envelopes = {
        pub: base64.b64encode(ecies_seal(new_symkey, x25519)).decode("ascii")
        for pub, x25519 in (envelope_for or [])
    }
    payload = {
        "group_id": group_id,
        "kicked_pubkey": kicked_pubkey,
        "envelopes": envelopes,
    }
    return _sealed(group_id, FRAME_SYMKEY_ROTATE, payload, old_symkey)


def _make_group_local_is_member(group_id: str) -> None:
    """Insert a group founded by someone else where the local node holds
    only the ``member`` role — used to exercise permission gates."""
    from nexus.security.group_permissions import (
        DEFAULT_ROLES,
        encode_role_permissions,
    )

    async def _ins():
        async with get_session() as s:
            now = iso_now()
            other_founder = secrets.token_hex(32)
            s.add(
                Group(
                    id=group_id,
                    name="g",
                    founder_pubkey=other_founder,
                    created_at=now,
                    deleted_at="",
                    privacy_mode="open",
                )
            )
            for rname, perms in DEFAULT_ROLES.items():
                s.add(
                    GroupRole(
                        group_id=group_id,
                        name=rname,
                        permissions_json=encode_role_permissions(perms),
                        created_at=now,
                        updated_at=now,
                    )
                )
            s.add(
                GroupMember(
                    group_id=group_id,
                    pubkey=get_local_group_pubkey(),
                    joined_at=now,
                    last_heartbeat_at="",
                    display_name="",
                    peer_address="",
                )
            )
            s.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=get_local_group_pubkey(),
                    role_name="member",
                    assigned_by_pubkey=other_founder,
                    assigned_at=now,
                )
            )
            await s.commit()

    asyncio.run(_ins())


def test_apply_symkey_rotate_adopts_new_key_and_drops_kicked(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    kicked = secrets.token_hex(32)
    _add_member(gid, kicked, "member", "kicked.example:8443")
    new_symkey = mint_group_symkey()
    me = get_local_group_pubkey()
    frame = _rotate_frame(
        gid,
        old_symkey,
        kicked,
        new_symkey,
        envelope_for=[(me, derive_x25519_pubkey_hex(get_local_group_privkey()))],
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True
    # This node adopted the new symkey...
    assert _read_local_symkey(gid) == new_symkey
    assert _read_local_symkey(gid) != old_symkey
    # ...and dropped the kicked member.
    assert _get_member(gid, kicked) is None


def test_apply_symkey_rotate_kicked_node_keeps_own_rows(client):
    """A node receiving a rotate frame that names *itself* as kicked
    must keep its own rows — pre-kick history stays readable."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    me = get_local_group_pubkey()
    frame = _rotate_frame(gid, old_symkey, me, mint_group_symkey())
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    # The local node's own membership row survives.
    assert _get_member(gid, me) is not None


def test_publish_symkey_rotate_seals_with_old_key(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    kicked = secrets.token_hex(32)
    remaining_priv, remaining_pub = generate_keypair()
    _add_member(
        gid,
        remaining_pub,
        "admin",
        "admin2.example:8443",
        member_x25519_pub=derive_x25519_pubkey_hex(remaining_priv),
    )
    stub = _RecordingPoster()

    async def _go():
        async with get_session() as s:
            summary = await publish_symkey_rotate(s, gid, kicked, poster=stub)
            await s.commit()
            return summary

    summary = asyncio.run(_go())
    assert summary["published"] == 1

    # The frame on the wire decodes with the OLD symkey.
    envelope = stub.calls[0]["body"]
    assert envelope["frame_type"] == FRAME_SYMKEY_ROTATE
    opened = open_frame(
        GroupFrame.from_dict(envelope),
        symkey=old_symkey,
        group_admin_pubkeys=[get_local_group_pubkey()],
    )
    decoded = json.loads(opened.payload.decode("utf-8"))
    assert decoded["kicked_pubkey"] == kicked
    # The remaining member can open their envelope -> the new symkey.
    new_symkey = ecies_open(
        base64.b64decode(decoded["envelopes"][remaining_pub]), remaining_priv
    )
    assert len(new_symkey) == 32
    # The publisher rotated its own copy to that same new key.
    assert _read_local_symkey(gid) == new_symkey
    assert _read_local_symkey(gid) != old_symkey


def test_symkey_rotate_from_non_kicker_rejected(client):
    """A symkey.rotate signed by a plain member (no member:kick) is
    dropped — open_frame proves membership, but not the right to kick."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    member_priv, member_pub = generate_keypair()
    _add_member(gid, member_pub, "member", "m.example:8443")
    member_grant = sign_grant(
        group_id=gid,
        member_pubkey=member_pub,
        roles=("member",),
        admin_privkey=get_local_group_privkey(),
        issued_at=iso_now(),
        expires_at=_future_iso(),
        nonce=secrets.token_hex(16),
    )
    frame = seal_frame(
        channel=gid,
        frame_type=FRAME_SYMKEY_ROTATE,
        payload=json.dumps(
            {"group_id": gid, "kicked_pubkey": "ab" * 32, "envelopes": {}}
        ).encode("utf-8"),
        symkey=old_symkey,
        sender_grant_blob=member_grant,
        sender_privkey_hex=member_priv,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is False
    assert "member:kick" in result["reason"]


def test_symkey_rotate_self_leave_allowed(client):
    """A member rotating themselves OUT (voluntary leave, sender == kicked)
    is authorized even without member:kick — otherwise remaining members
    never drop the leaver."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    member_priv, member_pub = generate_keypair()
    _add_member(gid, member_pub, "member", "m.example:8443")
    member_grant = sign_grant(
        group_id=gid,
        member_pubkey=member_pub,
        roles=("member",),
        admin_privkey=get_local_group_privkey(),
        issued_at=iso_now(),
        expires_at=_future_iso(),
        nonce=secrets.token_hex(16),
    )
    frame = seal_frame(
        channel=gid,
        frame_type=FRAME_SYMKEY_ROTATE,
        payload=json.dumps(
            {"group_id": gid, "kicked_pubkey": member_pub, "envelopes": {}}
        ).encode("utf-8"),
        symkey=old_symkey,
        sender_grant_blob=member_grant,
        sender_privkey_hex=member_priv,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True


def test_symkey_rotate_audience_is_all_members(client):
    """A symkey.rotate fans out to every member, plain members included."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "member", "member2.example:8443")
    old_symkey = _mint_symkey(gid)
    frame = _rotate_frame(
        gid, old_symkey, secrets.token_hex(32), mint_group_symkey()
    )
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.calls[0]["address"] == "member2.example:8443"


def test_kick_member_happy_path(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    _, kicked_pub = generate_keypair()
    _add_member(gid, kicked_pub, "member", "kicked.example:8443")
    res = client.post(f"/local/groups/{gid}/members/{kicked_pub}/kick")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kicked_pubkey"] == kicked_pub
    assert body["symkey_rotated"] is True
    # Member removed locally, symkey rotated.
    assert _get_member(gid, kicked_pub) is None
    assert _read_local_symkey(gid) != old_symkey


def test_kick_member_404_for_non_member(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    res = client.post(
        f"/local/groups/{gid}/members/{secrets.token_hex(32)}/kick"
    )
    assert res.status_code == 404, res.text


def test_kick_member_cannot_kick_founder(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    me = get_local_group_pubkey()
    res = client.post(f"/local/groups/{gid}/members/{me}/kick")
    assert res.status_code == 409, res.text


def test_kick_member_403_without_perm(client):
    gid = "grp_" + secrets.token_hex(6)
    _make_group_local_is_member(gid)
    res = client.post(
        f"/local/groups/{gid}/members/{secrets.token_hex(32)}/kick"
    )
    assert res.status_code == 403, res.text


def test_kicked_member_cannot_open_post_kick_frame(client):
    """The plan's verify criterion: post-kick frames are sealed with the
    new symkey, which the kicked member (holding only the old one) can
    no longer open."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    old_symkey = _mint_symkey(gid)
    _, kicked_pub = generate_keypair()
    _add_member(gid, kicked_pub, "member", "kicked.example:8443")
    assert client.post(
        f"/local/groups/{gid}/members/{kicked_pub}/kick"
    ).status_code == 200

    new_symkey = _read_local_symkey(gid)
    assert new_symkey != old_symkey

    # A frame published after the kick is sealed with the new symkey.
    frame = _sealed(
        gid,
        FRAME_ROSTER_UPDATE,
        {"group_id": gid, "action": "add", "member": {"pubkey": "cd" * 32}},
        new_symkey,
    )
    # The kicked member, holding only the OLD key, cannot open it.
    with pytest.raises(FrameVerificationError):
        open_frame(
            GroupFrame.from_dict(frame.to_dict()),
            symkey=old_symkey,
            group_admin_pubkeys=[get_local_group_pubkey()],
        )
    # A remaining member with the new key still can.
    opened = open_frame(
        GroupFrame.from_dict(frame.to_dict()),
        symkey=new_symkey,
        group_admin_pubkeys=[get_local_group_pubkey()],
    )
    assert opened.frame_type == FRAME_ROSTER_UPDATE


# ---- Wave 28: relay.update sync + self-heal -----------------------------


def _relay_payload(group_id: str, relay_url: str, action: str = "add") -> dict:
    return {
        "group_id": group_id,
        "action": action,
        "relay_url": relay_url,
        "operator_pubkey": get_local_group_pubkey(),
    }


def _add_relay_binding(group_id: str, relay_url: str, status: str = "active"):
    async def _ins():
        async with get_session() as s:
            s.add(
                GroupRelayBinding(
                    group_id=group_id,
                    relay_url=relay_url,
                    operator_pubkey=get_local_group_pubkey(),
                    registered_at=iso_now(),
                    last_seen_at="",
                    status=status,
                )
            )
            await s.commit()

    asyncio.run(_ins())


def _get_relay_binding(group_id: str, relay_url: str):
    async def _q():
        async with get_session() as s:
            return await s.get(GroupRelayBinding, (group_id, relay_url))

    return asyncio.run(_q())


def test_dispatch_relay_update_adds_binding(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    frame = _sealed(
        gid, FRAME_RELAY_UPDATE,
        _relay_payload(gid, "wss://r.example.com"), symkey,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True
    row = _get_relay_binding(gid, "wss://r.example.com")
    assert row is not None
    assert row.status == "active"


def test_dispatch_relay_update_remove_retires_binding(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    _add_relay_binding(gid, "wss://r.example.com")
    frame = _sealed(
        gid, FRAME_RELAY_UPDATE,
        _relay_payload(gid, "wss://r.example.com", "remove"), symkey,
    )
    asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert _get_relay_binding(gid, "wss://r.example.com").status == "retired"


def test_relay_update_from_non_relay_host_rejected(client):
    """relay.update from a plain member (no relay:host) is dropped."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    member_priv, member_pub = generate_keypair()
    _add_member(gid, member_pub, "member", "m.example:8443")
    member_grant = sign_grant(
        group_id=gid,
        member_pubkey=member_pub,
        roles=("member",),
        admin_privkey=get_local_group_privkey(),
        issued_at=iso_now(),
        expires_at=_future_iso(),
        nonce=secrets.token_hex(16),
    )
    frame = seal_frame(
        channel=gid,
        frame_type=FRAME_RELAY_UPDATE,
        payload=json.dumps(
            _relay_payload(gid, "wss://bad.example.com")
        ).encode("utf-8"),
        symkey=symkey,
        sender_grant_blob=member_grant,
        sender_privkey_hex=member_priv,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is False
    # Wave 66: relay.update gate message now names both possible perms.
    assert result["reason"] == "sender lacks relay:host / relay:share_content"


def test_publish_relay_update_builds_delta(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    _add_member(gid, secrets.token_hex(32), "admin", "admin2.example:8443")
    stub = _RecordingPoster()

    async def _go():
        async with get_session() as s:
            return await publish_relay_update(
                s, gid, "wss://r.example.com", "add",
                get_local_group_pubkey(), poster=stub,
            )

    summary = asyncio.run(_go())
    assert summary["published"] == 1

    envelope = stub.calls[0]["body"]
    assert envelope["frame_type"] == FRAME_RELAY_UPDATE
    opened = open_frame(
        GroupFrame.from_dict(envelope),
        symkey=symkey,
        group_admin_pubkeys=[get_local_group_pubkey()],
    )
    decoded = json.loads(opened.payload.decode("utf-8"))
    assert decoded["action"] == "add"
    assert decoded["relay_url"] == "wss://r.example.com"


def test_reconcile_relay_url_swaps_bindings(client):
    """Wave 28 self-heal: a tunnel URL rotation re-points every group
    binding from the old URL to the new one."""
    from nexus.runtime import relay_selfheal

    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)
    old_url = "wss://old.trycloudflare.com"
    new_url = "wss://new.trycloudflare.com"
    _add_relay_binding(gid, old_url)

    moved = asyncio.run(relay_selfheal.reconcile_relay_url(old_url, new_url))
    assert moved == 1
    assert _get_relay_binding(gid, old_url).status == "retired"
    new_row = _get_relay_binding(gid, new_url)
    assert new_row is not None
    assert new_row.status == "active"
