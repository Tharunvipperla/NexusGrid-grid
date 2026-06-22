"""Wave 66 — consensual per-group relay content-share (E2E-blind by default)."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import group_inbox as gi
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_permissions import (
    DEFAULT_ROLES,
    PERM_RELAY_HOST,
    PERM_RELAY_SHARE_CONTENT,
)
from nexus.storage import database


# --- the core invariant: relay:host can NOT self-authorize content -----------


def test_share_content_perm_is_founder_admin_only():
    assert PERM_RELAY_SHARE_CONTENT in DEFAULT_ROLES["founder"]
    assert PERM_RELAY_SHARE_CONTENT in DEFAULT_ROLES["admin"]
    # A plain member, and notably anyone whose only relay grant is relay:host,
    # must NOT get content-share by default — that's the whole point.
    assert PERM_RELAY_SHARE_CONTENT not in DEFAULT_ROLES["member"]
    assert PERM_RELAY_HOST != PERM_RELAY_SHARE_CONTENT


# --- endpoint behaviour ------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}"
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
    async def _noop_publish(*a, **k):
        return {"via": "test-stub"}
    monkeypatch.setattr("nexus.runtime.group_inbox.publish_relay_update", _noop_publish)
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _group_with_relay(client, url="ws://h:9000") -> str:
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    assert client.post(f"/local/groups/{gid}/relays", json={"relay_url": url}).status_code == 200
    return gid


def _relay(client, gid, url):
    relays = client.get(f"/local/groups/{gid}").json()["relays"]
    return next(r for r in relays if r["relay_url"] == url)


def test_default_relay_is_e2e_blind(client):
    gid = _group_with_relay(client)
    r = _relay(client, gid, "ws://h:9000")
    assert r["content_share"] is False
    assert r["content_share_by"] == ""


def test_authorize_then_revoke_visible_in_detail(client):
    gid = _group_with_relay(client)
    me = client.get(f"/local/groups/{gid}").json()["my_pubkey"]

    assert client.post(f"/local/groups/{gid}/relays/content_share",
                       json={"relay_url": "ws://h:9000"}).status_code == 200
    r = _relay(client, gid, "ws://h:9000")
    assert r["content_share"] is True and r["content_share_by"] == me and r["content_share_at"]

    assert client.post(f"/local/groups/{gid}/relays/content_revoke",
                       json={"relay_url": "ws://h:9000"}).status_code == 200
    assert _relay(client, gid, "ws://h:9000")["content_share"] is False


def test_content_share_unknown_relay_404(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    res = client.post(f"/local/groups/{gid}/relays/content_share",
                      json={"relay_url": "ws://nope:1"})
    assert res.status_code == 404


def test_content_key_released_only_when_authorized(client, monkeypatch):
    gid = _group_with_relay(client)
    # Stand in a symkey on this node (real mint is lazy; not needed here).
    async def _symkey(session, group_id):
        return b"\x11" * 32
    monkeypatch.setattr("nexus.runtime.group_inbox._local_symkey", _symkey)

    # Unauthorized -> 403, key withheld.
    res = client.get(f"/local/groups/{gid}/relay_content_key", params={"relay_url": "ws://h:9000"})
    assert res.status_code == 403

    # Authorize -> the gated path releases the symkey.
    client.post(f"/local/groups/{gid}/relays/content_share", json={"relay_url": "ws://h:9000"})
    res = client.get(f"/local/groups/{gid}/relay_content_key", params={"relay_url": "ws://h:9000"})
    assert res.status_code == 200
    assert base64.b64decode(res.json()["symkey_b64"]) == b"\x11" * 32

    # Revoke -> withheld again.
    client.post(f"/local/groups/{gid}/relays/content_revoke", json={"relay_url": "ws://h:9000"})
    assert client.get(f"/local/groups/{gid}/relay_content_key",
                      params={"relay_url": "ws://h:9000"}).status_code == 403


def test_content_key_409_when_no_symkey(client, monkeypatch):
    gid = _group_with_relay(client)
    async def _symkey(session, group_id):
        return None
    monkeypatch.setattr("nexus.runtime.group_inbox._local_symkey", _symkey)
    client.post(f"/local/groups/{gid}/relays/content_share", json={"relay_url": "ws://h:9000"})
    res = client.get(f"/local/groups/{gid}/relay_content_key", params={"relay_url": "ws://h:9000"})
    assert res.status_code == 409


# --- frame gate: content actions require relay:share_content -----------------


def _opened(action):
    return types.SimpleNamespace(
        payload=json.dumps({"action": action}).encode("utf-8"),
        channel="g1", sender_pubkey="sender",
    )


def test_content_frame_requires_share_perm(isolated_db, monkeypatch):
    seen = {}

    async def _has_perm(session, gid, who, perm):
        seen["perm"] = perm
        return False  # sender lacks whatever is required

    monkeypatch.setattr(gi, "has_permission", _has_perm)
    # content_share is gated on relay:share_content, and the sender lacks it.
    assert asyncio.run(gi._relay_update_sender_authorized(_opened("content_share"))) is False
    assert seen["perm"] == PERM_RELAY_SHARE_CONTENT


def test_noncontent_frame_uses_relay_host_gate(isolated_db, monkeypatch):
    async def _can_host(group_id, sender_pubkey):
        return True
    monkeypatch.setattr(gi, "_sender_can_host_relay", _can_host)
    # add/remove/config keep the relay:host gate (not share_content).
    assert asyncio.run(gi._relay_update_sender_authorized(_opened("add"))) is True
