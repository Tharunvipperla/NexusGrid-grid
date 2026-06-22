"""Wave 44 — group chat: send / list / delete / mute + frame apply."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import group_inbox
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import GroupMember, GroupMessage
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
def client(isolated_db, monkeypatch):
    # Don't actually fan out chat frames in these unit tests.
    async def _noop(*a, **k):
        return {"via": "test-stub"}

    for fn in ("publish_chat_message", "publish_chat_mute", "publish_chat_delete"):
        monkeypatch.setattr(f"nexus.runtime.group_inbox.{fn}", _noop)
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _create_group(client) -> str:
    return client.post("/local/groups", json={"name": "g"}).json()["id"]


def _local_pubkey() -> str:
    from nexus.security.group_keys import get_local_group_pubkey
    return get_local_group_pubkey()


def test_send_and_list_messages(client):
    gid = _create_group(client)
    r = client.post(f"/local/groups/{gid}/messages", json={"body": "hello"})
    assert r.status_code == 200, r.text
    msg_id = r.json()["msg_id"]

    lst = client.get(f"/local/groups/{gid}/messages").json()["messages"]
    assert len(lst) == 1
    assert lst[0]["msg_id"] == msg_id
    assert lst[0]["body"] == "hello"
    assert lst[0]["deleted"] is False


def test_empty_body_rejected(client):
    gid = _create_group(client)
    r = client.post(f"/local/groups/{gid}/messages", json={"body": ""})
    assert r.status_code == 422


def test_delete_own_message_tombstones(client):
    gid = _create_group(client)
    msg_id = client.post(f"/local/groups/{gid}/messages", json={"body": "oops"}).json()["msg_id"]
    d = client.delete(f"/local/groups/{gid}/messages/{msg_id}")
    assert d.status_code == 200, d.text
    lst = client.get(f"/local/groups/{gid}/messages").json()["messages"]
    assert lst[0]["deleted"] is True
    assert lst[0]["body"] == ""


def test_muted_member_cannot_send(client):
    gid = _create_group(client)
    me = _local_pubkey()

    # Manually mute the founder's own row (founder holds member:mute, but
    # the endpoint blocks muting the founder — so set the flag directly to
    # exercise the send-side block).
    async def _mute():
        async with get_session() as s:
            row = await s.get(GroupMember, (gid, me))
            row.muted = 1
            await s.commit()
    asyncio.run(_mute())

    r = client.post(f"/local/groups/{gid}/messages", json={"body": "blocked"})
    assert r.status_code == 403


def test_mute_endpoint_requires_target_member(client):
    gid = _create_group(client)
    r = client.post(f"/local/groups/{gid}/members/nope/mute", json={"muted": True})
    assert r.status_code == 404


def test_cannot_mute_founder(client):
    gid = _create_group(client)
    me = _local_pubkey()
    r = client.post(f"/local/groups/{gid}/members/{me}/mute", json={"muted": True})
    assert r.status_code == 403


# ---- frame apply side --------------------------------------------------


def _opened(channel: str, sender: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        channel=channel, sender_pubkey=sender,
        payload=json.dumps(payload).encode("utf-8"),
    )


def test_apply_chat_message_dedupes(isolated_db):
    gid, sender = "g1", "peer-1"
    payload = {"group_id": gid, "msg_id": "m1", "body": "hi", "sent_at": iso_now()}
    op = _opened(gid, sender, payload)
    assert asyncio.run(group_inbox.apply_chat_message(op)) is True
    # Re-apply is a no-op (dedupe), still returns True.
    assert asyncio.run(group_inbox.apply_chat_message(op)) is True

    async def _count():
        async with get_session() as s:
            rows = (await s.execute(
                __import__("sqlalchemy").select(GroupMessage).where(
                    GroupMessage.group_id == gid)
            )).scalars().all()
            return len(rows)
    assert asyncio.run(_count()) == 1


def test_apply_chat_message_drops_muted_sender(isolated_db):
    gid, sender = "g1", "peer-muted"

    async def _seed():
        async with get_session() as s:
            s.add(GroupMember(group_id=gid, pubkey=sender, muted=1))
            await s.commit()
    asyncio.run(_seed())

    op = _opened(gid, sender, {"group_id": gid, "msg_id": "m2", "body": "x", "sent_at": iso_now()})
    assert asyncio.run(group_inbox.apply_chat_message(op)) is False


def test_group_list_reports_message_count(client):
    gid = _create_group(client)
    client.post(f"/local/groups/{gid}/messages", json={"body": "one"})
    client.post(f"/local/groups/{gid}/messages", json={"body": "two"})
    groups = client.get("/local/groups").json()["groups"]
    g = next(x for x in groups if x["id"] == gid)
    assert g["message_count"] == 2


def test_group_list_reports_best_relay_rtt(client):
    # Wave 47: My Groups shows the fastest reachable relay's RTT (or None when
    # no relay answered the last probe).
    from nexus.storage.models import GroupRelayBinding

    gid = _create_group(client)

    async def _seed():
        async with get_session() as s:
            s.add(GroupRelayBinding(
                group_id=gid, relay_url="ws://r1:9001", status="active",
                last_rtt_ms=120,
            ))
            s.add(GroupRelayBinding(
                group_id=gid, relay_url="ws://r2:9001", status="active",
                last_rtt_ms=30,
            ))
            s.add(GroupRelayBinding(
                group_id=gid, relay_url="ws://r3:9001", status="active",
                last_rtt_ms=None,  # didn't answer last probe
            ))
            await s.commit()
    asyncio.run(_seed())

    g = next(x for x in client.get("/local/groups").json()["groups"] if x["id"] == gid)
    assert g["relay_best_rtt_ms"] == 30  # fastest reachable
    assert g["relay_count"] == 3


def test_group_list_best_rtt_none_when_all_offline(client):
    from nexus.storage.models import GroupRelayBinding

    gid = _create_group(client)

    async def _seed():
        async with get_session() as s:
            s.add(GroupRelayBinding(
                group_id=gid, relay_url="ws://r1:9001", status="active",
                last_rtt_ms=None,
            ))
            await s.commit()
    asyncio.run(_seed())

    g = next(x for x in client.get("/local/groups").json()["groups"] if x["id"] == gid)
    assert g["relay_best_rtt_ms"] is None  # UI renders this as "offline"
    assert g["relay_count"] == 1


def test_group_message_reply_fields_persist(client):
    gid = _create_group(client)
    a = client.post(f"/local/groups/{gid}/messages", json={"body": "original"}).json()["msg_id"]
    client.post(f"/local/groups/{gid}/messages", json={
        "body": "a reply", "reply_to": a,
        "reply_snippet": "original", "reply_sender": "Alice",
    })
    msgs = client.get(f"/local/groups/{gid}/messages").json()["messages"]
    reply = next(m for m in msgs if m["body"] == "a reply")
    assert reply["reply_to"] == a
    assert reply["reply_snippet"] == "original"
    assert reply["reply_sender"] == "Alice"


def test_group_inline_attachment(client):
    import base64
    gid = _create_group(client)
    data = base64.b64encode(b"hello file").decode()
    r = client.post(f"/local/groups/{gid}/messages", json={
        "body": "see file", "attach_name": "a.txt", "attach_mime": "text/plain", "attach_data": data,
    })
    assert r.status_code == 200, r.text
    mid = r.json()["msg_id"]
    msgs = client.get(f"/local/groups/{gid}/messages").json()["messages"]
    m = next(x for x in msgs if x["msg_id"] == mid)
    assert m["attach_kind"] == "inline" and m["attach_name"] == "a.txt" and m["attach_size"] == 10
    # download returns the raw bytes
    d = client.get(f"/local/groups/{gid}/messages/{mid}/attachment")
    assert d.status_code == 200 and d.content == b"hello file"


def test_group_attachment_over_5mb_goes_foreign(client, tmp_path, monkeypatch):
    # Wave 47: >5 MB is sender-hosted (attach_kind="foreign"), not rejected.
    import base64

    from nexus.runtime import chat_attachments as ca
    monkeypatch.setattr(ca, "_dir", lambda: tmp_path / "blobs")
    (tmp_path / "blobs").mkdir(exist_ok=True)
    gid = _create_group(client)
    big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
    r = client.post(f"/local/groups/{gid}/messages", json={"attach_name": "big", "attach_data": big})
    assert r.status_code == 200
    mid = r.json()["msg_id"]
    msgs = client.get(f"/local/groups/{gid}/messages").json()["messages"]
    row = next(m for m in msgs if m["msg_id"] == mid)
    assert row["attach_kind"] == "foreign"
    # The sender hosts it, so it serves locally from disk.
    d = client.get(f"/local/groups/{gid}/messages/{mid}/attachment")
    assert d.status_code == 200 and len(d.content) == 5 * 1024 * 1024 + 1


def test_group_attachment_too_large_rejected(client):
    import base64
    gid = _create_group(client)
    big = base64.b64encode(b"x" * (100 * 1024 * 1024 + 1)).decode()
    r = client.post(f"/local/groups/{gid}/messages", json={"attach_name": "big", "attach_data": big})
    assert r.status_code == 413


def test_message_or_attachment_required(client):
    gid = _create_group(client)
    r = client.post(f"/local/groups/{gid}/messages", json={"body": ""})
    assert r.status_code == 422
