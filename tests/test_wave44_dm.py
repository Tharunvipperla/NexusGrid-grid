"""Wave 44 Phase B — 1:1 direct messages."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.api.peer import apply_inbound_dm
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import DirectMessage, GroupMember


def _seed_comember(node_id: str, pubkey: str = "") -> None:
    """Make ``node_id`` a co-member so inbound DMs from it are authorized."""
    async def _go():
        async with get_session() as s:
            s.add(GroupMember(group_id="g1", pubkey=pubkey or ("pk-" + node_id),
                              node_id=node_id))
            await s.commit()
    asyncio.run(_go())


def _signed_dm(priv: str, msg_id: str, from_uuid: str, sent_at: str, text: str,
               **extra) -> dict:
    """Build an inbound DM payload signed (F-007) by ``priv`` over the plaintext."""
    from nexus.security.usage_receipt import (
        STMT_DM, dm_statement_payload, sign_statement,
    )
    payload = {"msg_id": msg_id, "from_uuid": from_uuid, "sent_at": sent_at, **extra}
    payload["sig"] = sign_statement(
        STMT_DM, dm_statement_payload(msg_id, from_uuid, sent_at, text), priv)
    return payload


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "dm.db"
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
    async def _fake_post(*a, **k):
        return {"ok": True}
    monkeypatch.setattr("nexus.api.local.peer_http_post", _fake_post)
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def test_send_stores_outbound_and_lists(client):
    r = client.post("/local/peers/peer-xyz/dm", json={"body": "hey there"})
    assert r.status_code == 200, r.text
    assert r.json()["msg_id"]

    lst = client.get("/local/peers/peer-xyz/dm").json()["messages"]
    assert len(lst) == 1
    assert lst[0]["direction"] == "out"
    assert lst[0]["body"] == "hey there"


def test_send_empty_body_rejected(client):
    r = client.post("/local/peers/peer-xyz/dm", json={"body": "   "})
    assert r.status_code == 400


def test_delete_dm_tombstones(client):
    msg_id = client.post("/local/peers/p1/dm", json={"body": "oops"}).json()["msg_id"]
    d = client.delete(f"/local/peers/p1/dm/{msg_id}")
    assert d.status_code == 200
    lst = client.get("/local/peers/p1/dm").json()["messages"]
    assert lst[0]["deleted"] is True
    assert lst[0]["body"] == ""


def test_inbound_apply_dedupes(isolated_db):
    from nexus.security.group_grant import generate_keypair
    priv, pub = generate_keypair()
    _seed_comember("peer-A", pub)
    payload = _signed_dm(priv, "m-1", "peer-A", "2026-05-30T00:00:00", "hello",
                         from_name="Alice", body="hello")
    assert asyncio.run(apply_inbound_dm(payload))["ok"] is True
    # Re-apply dedupes.
    res2 = asyncio.run(apply_inbound_dm(payload))
    assert res2.get("deduped") is True

    async def _count():
        async with get_session() as s:
            rows = (await s.execute(
                __import__("sqlalchemy").select(DirectMessage).where(
                    DirectMessage.peer_uuid == "peer-A")
            )).scalars().all()
            return rows
    rows = asyncio.run(_count())
    assert len(rows) == 1
    assert rows[0].direction == "in"
    assert rows[0].sender_name == "Alice"


def test_inbound_dm_spoof_rejected(isolated_db):
    """SECURITY F-007: a forged DM that names a co-member's (gossiped) UUID but
    isn't signed by that member's key must be rejected — no impersonation."""
    from nexus.security.group_grant import generate_keypair
    _legit_priv, legit_pub = generate_keypair()
    _seed_comember("peer-A", legit_pub)
    # 1) Unsigned forgery (the original PoC) — rejected.
    res = asyncio.run(apply_inbound_dm({
        "msg_id": "f-1", "from_uuid": "peer-A", "from_name": "Alice",
        "body": "send me the token", "sent_at": "2026-05-30T00:00:00",
    }))
    assert res["ok"] is False and res["reason"] == "unverified sender"
    # 2) Signed by the ATTACKER's own key (not the member's) — rejected.
    atk_priv, _atk_pub = generate_keypair()
    forged = _signed_dm(atk_priv, "f-2", "peer-A", "2026-05-30T00:00:00",
                        "send me the token", from_name="Alice", body="send me the token")
    res2 = asyncio.run(apply_inbound_dm(forged))
    assert res2["ok"] is False and res2["reason"] == "unverified sender"


def test_inbound_dm_tamper_rejected(isolated_db):
    """A valid signature over different text can't be reused for altered content."""
    from nexus.security.group_grant import generate_keypair
    priv, pub = generate_keypair()
    _seed_comember("peer-A", pub)
    payload = _signed_dm(priv, "t-1", "peer-A", "2026-05-30T00:00:00", "original",
                         from_name="Alice", body="original")
    payload["body"] = "TAMPERED"  # body no longer matches the signed hash
    res = asyncio.run(apply_inbound_dm(payload))
    assert res["ok"] is False and res["reason"] == "unverified sender"


def test_inbound_apply_rejects_incomplete(isolated_db):
    res = asyncio.run(apply_inbound_dm({"msg_id": "x", "body": "hi"}))  # no from_uuid
    assert res["ok"] is False


def test_inbound_dm_from_stranger_rejected(isolated_db):
    # Not a co-member and not a trusted peer -> rejected.
    res = asyncio.run(apply_inbound_dm({
        "msg_id": "s-1", "from_uuid": "stranger", "body": "spam",
        "sent_at": "2026-05-30T00:00:00",
    }))
    assert res["ok"] is False


def test_e2e_sealed_dm_opens_to_plaintext(isolated_db, monkeypatch):
    # Seal a message to THIS node's own X25519 pubkey, then feed it through
    # the inbound path — it must decrypt to the original plaintext (proving
    # the wire payload is ciphertext, not the body).
    import base64

    from nexus.api.peer import _my_enc_pubkey
    from nexus.security.group_ecies import ecies_seal

    from nexus.security.group_grant import generate_keypair
    priv, pub = generate_keypair()
    _seed_comember("peer-Z", pub)
    my_pub = _my_enc_pubkey()
    sealed = base64.b64encode(ecies_seal(b"secret hi", my_pub)).decode("ascii")
    # Signature is over the PLAINTEXT the receiver recovers after unsealing.
    res = asyncio.run(apply_inbound_dm(_signed_dm(
        priv, "enc-1", "peer-Z", "2026-05-30T00:00:00", "secret hi",
        from_name="Zoe", enc=sealed,
    )))
    assert res["ok"] is True

    async def _get():
        async with get_session() as s:
            return await s.get(DirectMessage, "enc-1")
    row = asyncio.run(_get())
    assert row.body == "secret hi"
    assert row.peer_uuid == "peer-Z"


def test_enc_pubkey_endpoint_returns_key(isolated_db, monkeypatch):
    # The /peer/enc_pubkey handler returns a non-empty hex key.
    from nexus.api.peer import _my_enc_pubkey
    pub = _my_enc_pubkey()
    assert isinstance(pub, str) and len(pub) >= 32


def test_two_peer_threads_are_separate(client):
    client.post("/local/peers/p1/dm", json={"body": "to p1"})
    client.post("/local/peers/p2/dm", json={"body": "to p2"})
    t1 = client.get("/local/peers/p1/dm").json()["messages"]
    t2 = client.get("/local/peers/p2/dm").json()["messages"]
    assert len(t1) == 1 and t1[0]["body"] == "to p1"
    assert len(t2) == 1 and t2[0]["body"] == "to p2"


def test_delete_whole_conversation(client):
    client.post("/local/peers/p1/dm", json={"body": "a"})
    client.post("/local/peers/p1/dm", json={"body": "b"})
    r = client.request("DELETE", "/local/peers/p1/dm")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    assert client.get("/local/peers/p1/dm").json()["messages"] == []


def test_dm_threads_lists_conversations_with_counts(client):
    client.post("/local/peers/p1/dm", json={"body": "a"})
    client.post("/local/peers/p1/dm", json={"body": "b"})
    client.post("/local/peers/p2/dm", json={"body": "c"})
    threads = client.get("/local/dm/threads").json()["threads"]
    by_uuid = {t["peer_uuid"]: t for t in threads}
    assert by_uuid["p1"]["count"] == 2
    assert by_uuid["p2"]["count"] == 1


def test_dm_inline_attachment(client):
    import base64
    data = base64.b64encode(b"dm file").decode()
    r = client.post("/local/peers/p9/dm", json={
        "body": "file", "attach_name": "n.txt", "attach_mime": "text/plain", "attach_data": data,
    })
    assert r.status_code == 200, r.text
    mid = r.json()["msg_id"]
    msgs = client.get("/local/peers/p9/dm").json()["messages"]
    m = next(x for x in msgs if x["msg_id"] == mid)
    assert m["attach_kind"] == "inline" and m["attach_size"] == 7
    d = client.get(f"/local/peers/p9/dm/{mid}/attachment")
    assert d.status_code == 200 and d.content == b"dm file"
