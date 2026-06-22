"""Wave 67 — group relay-code copy (channel-published canonical source)."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as groups_router
from nexus.runtime import relay_codeprint as cp
from nexus.runtime.group_inbox import (
    FRAME_RELAY_CODE,
    dispatch_inbound_frame,
    relay_inbound_frame,
)
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_frame import seal_frame
from nexus.security.group_grant import (
    generate_keypair,
    sign_challenge,
    sign_grant,
)
from nexus.security.group_keys import (
    get_local_group_privkey,
    get_local_group_pubkey,
)
from nexus.storage import database, get_session
from nexus.storage.models import (
    Group,
    GroupMember,
    GroupMemberRole,
    GroupRelayCode,
    GroupRole,
)
from nexus.utils.time import iso_now

_RELAY_SRC = (
    "from fastapi import FastAPI\n"
    "GRID_KEY = ''\n"
    "app = FastAPI()\n"
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
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


# ---- helpers -----------------------------------------------------------


def _future_iso(hours: int = 24) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _src_fingerprint(source: str) -> str:
    norm = source.replace("\r\n", "\n").replace("\r", "\n")
    return cp.fingerprint_for_bytes(norm.encode("utf-8"))


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


def _freeze_fingerprint(group_id: str, fp: str) -> None:
    async def _set():
        async with get_session() as s:
            g = await s.get(Group, group_id)
            g.relay_code_fingerprint = fp
            await s.commit()

    asyncio.run(_set())


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


def _sealed_relay_code(group_id: str, source: str, symkey: bytes):
    payload = {"group_id": group_id, "source": source, "published_at": iso_now()}
    return seal_frame(
        channel=group_id,
        frame_type=FRAME_RELAY_CODE,
        payload=json.dumps(payload).encode("utf-8"),
        symkey=symkey,
        sender_grant_blob=_founder_grant(group_id),
        sender_privkey_hex=get_local_group_privkey(),
    )


def _add_member(group_id: str, pubkey: str, role: str, addr: str) -> None:
    async def _ins():
        async with get_session() as s:
            s.add(GroupMember(
                group_id=group_id, pubkey=pubkey, joined_at=iso_now(),
                last_heartbeat_at="", display_name="", peer_address=addr,
            ))
            s.add(GroupMemberRole(
                group_id=group_id, member_pubkey=pubkey, role_name=role,
                assigned_by_pubkey="", assigned_at=iso_now(),
            ))
            await s.commit()

    asyncio.run(_ins())


def _get_code(group_id: str):
    async def _q():
        async with get_session() as s:
            return await s.get(GroupRelayCode, group_id)

    return asyncio.run(_q())


class _RecordingPoster:
    def __init__(self):
        self.calls = []

    async def __call__(self, peer_address, node_id, path, body, **_kw):
        self.calls.append({"address": peer_address, "path": path})
        return 200, {"ok": True}


# ---- apply_relay_code --------------------------------------------------


def test_apply_relay_code_stores_when_fingerprint_matches(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    fp = _src_fingerprint(_RELAY_SRC)
    _freeze_fingerprint(gid, fp)
    frame = _sealed_relay_code(gid, _RELAY_SRC, symkey)
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is True
    row = _get_code(gid)
    assert row is not None
    assert row.fingerprint == fp
    assert row.source.replace("\r\n", "\n") == _RELAY_SRC


def test_apply_relay_code_rejects_fingerprint_mismatch(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    # Freeze a DIFFERENT fingerprint than the source produces.
    _freeze_fingerprint(gid, "0" * 32)
    frame = _sealed_relay_code(gid, _RELAY_SRC, symkey)
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is False
    assert _get_code(gid) is None


def test_apply_relay_code_rejects_when_no_frozen_fingerprint(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    # No freeze at all — nothing to validate against.
    frame = _sealed_relay_code(gid, _RELAY_SRC, symkey)
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["applied"] is False
    assert _get_code(gid) is None


def test_relay_code_from_non_admin_rejected(client):
    """A relay.code sealed by a plain member (no role:assign) is dropped."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    symkey = _mint_symkey(gid)
    fp = _src_fingerprint(_RELAY_SRC)
    _freeze_fingerprint(gid, fp)
    member_priv, member_pub = generate_keypair()
    _add_member(gid, member_pub, "member", "m.example:8443")
    member_grant = sign_grant(
        group_id=gid, member_pubkey=member_pub, roles=("member",),
        admin_privkey=get_local_group_privkey(), issued_at=iso_now(),
        expires_at=_future_iso(), nonce=secrets.token_hex(16),
    )
    frame = seal_frame(
        channel=gid, frame_type=FRAME_RELAY_CODE,
        payload=json.dumps(
            {"group_id": gid, "source": _RELAY_SRC, "published_at": iso_now()}
        ).encode("utf-8"),
        symkey=symkey, sender_grant_blob=member_grant,
        sender_privkey_hex=member_priv,
    )
    result = asyncio.run(dispatch_inbound_frame(frame.to_dict()))
    assert result["ok"] is True
    assert result["applied"] is False
    assert "role:assign" in result["reason"]
    assert _get_code(gid) is None


def test_relay_code_fans_out_to_members(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _add_member(gid, secrets.token_hex(32), "member", "m2.example:8443")
    symkey = _mint_symkey(gid)
    fp = _src_fingerprint(_RELAY_SRC)
    _freeze_fingerprint(gid, fp)
    frame = _sealed_relay_code(gid, _RELAY_SRC, symkey)
    stub = _RecordingPoster()
    result = asyncio.run(relay_inbound_frame(frame.to_dict(), poster=stub))
    assert result["ok"] is True
    assert result["relayed"] == 1
    assert stub.calls[0]["path"] == "/peer/group/event"


# ---- publish endpoint --------------------------------------------------


def test_publish_endpoint_requires_frozen_fingerprint(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)
    res = client.post(
        f"/local/groups/{gid}/relay_code/publish", json={"module": "default"}
    )
    assert res.status_code == 409, res.text
    assert "frozen" in res.text


def test_publish_endpoint_rejects_mismatched_module(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)
    _freeze_fingerprint(gid, "0" * 32)  # not the default's fingerprint
    res = client.post(
        f"/local/groups/{gid}/relay_code/publish", json={"module": "default"}
    )
    assert res.status_code == 409, res.text
    assert "does not match" in res.text


def test_publish_endpoint_happy_path_stores_local_copy(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _mint_symkey(gid)
    # Freeze the bundled default's fingerprint, then publish it.
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    res = client.post(
        f"/local/groups/{gid}/relay_code/publish", json={"module": "default"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["fingerprint"] == cp.CURRENT_FINGERPRINT
    row = _get_code(gid)
    assert row is not None
    assert row.fingerprint == cp.CURRENT_FINGERPRINT
    assert "app" in row.source


# ---- live-host pull peer endpoint --------------------------------------


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _challenge_body(gid: str, grant_blob: bytes, member_priv: str) -> dict:
    nonce = secrets.token_bytes(16)
    sig = sign_challenge(
        grant_blob=grant_blob, nonce=nonce, member_privkey=member_priv
    )
    return {
        "group_id": gid,
        "grant_blob_b64": _b64(grant_blob),
        "nonce_b64": _b64(nonce),
        "signature_b64": _b64(sig),
    }


def test_peer_relay_code_happy_path(client):
    """The founder (holds relay:host) pulls the default relay source."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    body = _challenge_body(
        gid, _founder_grant(gid), get_local_group_privkey()
    )
    res = client.post("/peer/group/relay_code", json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["fingerprint"] == cp.CURRENT_FINGERPRINT
    assert "app" in out["source"]


def test_peer_relay_code_403_without_relay_host(client):
    """A plain member (member role, no relay:host) is refused."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    member_priv, member_pub = generate_keypair()
    member_grant = sign_grant(
        group_id=gid, member_pubkey=member_pub, roles=("member",),
        admin_privkey=get_local_group_privkey(), issued_at=iso_now(),
        expires_at=_future_iso(), nonce=secrets.token_hex(16),
    )
    body = _challenge_body(gid, member_grant, member_priv)
    res = client.post("/peer/group/relay_code", json=body)
    assert res.status_code == 403, res.text
    assert "relay:host" in res.text


def test_peer_relay_code_409_without_frozen_fingerprint(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    body = _challenge_body(
        gid, _founder_grant(gid), get_local_group_privkey()
    )
    res = client.post("/peer/group/relay_code", json=body)
    assert res.status_code == 409, res.text
    assert "frozen" in res.text


def test_peer_relay_code_409_when_host_lacks_matching_module(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, "abc123" + "0" * 26)  # matches no local module
    body = _challenge_body(
        gid, _founder_grant(gid), get_local_group_privkey()
    )
    res = client.post("/peer/group/relay_code", json=body)
    assert res.status_code == 409, res.text
    assert "no relay code matching" in res.text


def test_peer_relay_code_403_on_bad_signature(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    body = _challenge_body(
        gid, _founder_grant(gid), get_local_group_privkey()
    )
    body["signature_b64"] = _b64(b"\x00" * 64)  # wrong signature
    res = client.post("/peer/group/relay_code", json=body)
    assert res.status_code == 403, res.text


def test_peer_relay_code_authorizes_by_live_roster_not_stale_grant(client):
    """A member promoted to relay:host AFTER their grant was issued (grant
    still says 'member') is served — authz uses this host's authoritative
    roster, not the grant's embedded roles."""
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    mpriv, mpub = generate_keypair()
    _add_member(gid, mpub, "admin", "m.example:8443")  # DB: admin => relay:host
    # ...but the grant they present was minted with the old 'member' role.
    stale_grant = sign_grant(
        group_id=gid, member_pubkey=mpub, roles=("member",),
        admin_privkey=get_local_group_privkey(), issued_at=iso_now(),
        expires_at=_future_iso(), nonce=secrets.token_hex(16),
    )
    res = client.post("/peer/group/relay_code", json=_challenge_body(gid, stale_grant, mpriv))
    assert res.status_code == 200, res.text
    assert res.json()["fingerprint"] == cp.CURRENT_FINGERPRINT


# ---- status + obtain ---------------------------------------------------


_CUSTOM_SRC = "# custom group relay\nfrom fastapi import FastAPI\nGRID_KEY=''\napp=FastAPI()\n"


def _put_code(group_id: str, source: str, fp: str) -> None:
    async def _ins():
        async with get_session() as s:
            s.add(GroupRelayCode(
                group_id=group_id, source=source, fingerprint=fp,
                published_by="founder", published_at=iso_now(),
            ))
            await s.commit()

    asyncio.run(_ins())


def _make_group_local_is_member(group_id: str, founder_address: str = "") -> None:
    from nexus.security.group_permissions import (
        DEFAULT_ROLES,
        encode_role_permissions,
    )

    async def _ins():
        async with get_session() as s:
            now = iso_now()
            other = secrets.token_hex(32)
            s.add(Group(
                id=group_id, name="g", founder_pubkey=other, created_at=now,
                deleted_at="", privacy_mode="open", founder_address=founder_address,
            ))
            for rname, perms in DEFAULT_ROLES.items():
                s.add(GroupRole(
                    group_id=group_id, name=rname,
                    permissions_json=encode_role_permissions(perms),
                    created_at=now, updated_at=now,
                ))
            s.add(GroupMember(
                group_id=group_id, pubkey=get_local_group_pubkey(),
                joined_at=now, last_heartbeat_at="", display_name="",
                peer_address="",
            ))
            s.add(GroupMemberRole(
                group_id=group_id, member_pubkey=get_local_group_pubkey(),
                role_name="member", assigned_by_pubkey=other, assigned_at=now,
            ))
            await s.commit()

    asyncio.run(_ins())


def test_status_reports_no_frozen_and_can_host(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    res = client.get(f"/local/groups/{gid}/relay_code/status")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["frozen_fingerprint"] == ""
    assert out["have_local_module"] == ""
    assert out["channel_copy_available"] is False
    assert out["can_host"] is True  # founder holds relay:host


def test_status_reports_have_local_module(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    out = client.get(f"/local/groups/{gid}/relay_code/status").json()
    assert out["have_local_module"] == "default"


def test_status_reports_channel_copy_available(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    fp = _src_fingerprint(_CUSTOM_SRC)
    _freeze_fingerprint(gid, fp)
    _put_code(gid, _CUSTOM_SRC, fp)
    out = client.get(f"/local/groups/{gid}/relay_code/status").json()
    assert out["channel_copy_available"] is True
    assert out["have_local_module"] == ""  # custom fp not present locally


def test_obtain_already_local(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, cp.CURRENT_FINGERPRINT)
    res = client.post(f"/local/groups/{gid}/relay_code/obtain")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["already"] is True
    assert out["name"] == "default"
    assert out["origin"] == "local"


def test_obtain_from_channel_copy_imports_plugin(client):
    from nexus.runtime import local_relay

    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    fp = _src_fingerprint(_CUSTOM_SRC)
    _freeze_fingerprint(gid, fp)
    _put_code(gid, _CUSTOM_SRC, fp)
    res = client.post(f"/local/groups/{gid}/relay_code/obtain")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["already"] is False
    assert out["origin"] == "channel"
    assert out["fingerprint"] == fp
    # The imported plugin now appears among local modules with that fingerprint.
    mods = {m["name"]: m for m in local_relay.available_relay_modules()}
    assert out["name"] in mods
    assert mods[out["name"]]["fingerprint"] == fp


def test_obtain_403_without_relay_host(client):
    gid = "grp_" + secrets.token_hex(6)
    _make_group_local_is_member(gid)
    _freeze_fingerprint(gid, _src_fingerprint(_CUSTOM_SRC))
    res = client.post(f"/local/groups/{gid}/relay_code/obtain")
    assert res.status_code == 403, res.text


def test_obtain_404_when_nothing_available(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    _freeze_fingerprint(gid, _src_fingerprint(_CUSTOM_SRC))
    # No channel copy, no relay hosts reachable.
    res = client.post(f"/local/groups/{gid}/relay_code/obtain")
    assert res.status_code == 404, res.text


def test_status_auto_pulls_frozen_fingerprint(client, monkeypatch):
    """W67 smoothing: a member who hasn't synced the frozen fingerprint gets it
    auto-pulled from the founder when they open the copy status."""
    gid = "grp_" + secrets.token_hex(6)
    _make_group_local_is_member(gid, founder_address="127.0.0.1:9999")
    FP = _src_fingerprint(_CUSTOM_SRC)

    async def fake_post(*_a, **_k):
        return 200, {"bindings": [], "relay_code_fingerprint": FP}

    monkeypatch.setattr("nexus.api.groups._post_to_admin", fake_post)
    # Before: the local member row has no frozen fingerprint.
    out = client.get(f"/local/groups/{gid}/relay_code/status").json()
    assert out["frozen_fingerprint"] == FP  # auto-pulled, no manual sync needed
