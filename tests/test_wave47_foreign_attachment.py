"""Wave 47 — sender-hosted (>5 MB) foreign attachment path."""

from __future__ import annotations

import asyncio
import base64

import pytest

from nexus.runtime import chat_attachments as ca
from nexus.security import group_ecies, group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import DirectMessage
from nexus.utils.time import iso_now


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    # Keep blob storage inside the tmp dir.
    monkeypatch.setattr(ca, "_dir", lambda: tmp_path / "blobs")
    (tmp_path / "blobs").mkdir(exist_ok=True)
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


def test_symkey_seal_roundtrip():
    symkey = b"\x01" * 32
    data = b"hello world" * 1000
    sealed = ca.seal_with_symkey(symkey, data)
    assert sealed != data
    assert ca.open_with_symkey(symkey, sealed) == data


def test_store_load_has_delete(isolated_db):
    ca.store_blob("abc123", b"payload")
    assert ca.has_blob("abc123")
    assert ca.load_blob("abc123") == b"payload"
    ca.delete_blob("abc123")
    assert not ca.has_blob("abc123")
    assert ca.load_blob("abc123") is None


def test_load_missing_returns_none(isolated_db):
    assert ca.load_blob("nope") is None
    assert not ca.has_blob("nope")


def test_dm_attachment_pull_seals_to_requester(isolated_db, monkeypatch):
    """The /peer/attachment_pull endpoint seals the hosted blob to the
    requester's ECIES key; only they can open it."""
    from nexus.api import peer as peer_api

    # Requester (recipient) keypair.
    recip_priv = group_keys.get_local_group_privkey()
    recip_pub = group_ecies.derive_x25519_pubkey_hex(recip_priv)

    raw = b"BIGFILE" * 100000  # ~700 KB
    ca.store_blob("m-out", raw)

    async def _go():
        async with get_session() as s:
            s.add(DirectMessage(
                msg_id="m-out", peer_uuid="recipient-uuid", direction="out",
                body="", sent_at=iso_now(), received_at=iso_now(),
                attach_kind="foreign", attach_size=len(raw),
            ))
            await s.commit()

    asyncio.run(_go())

    # Stub the recipient-address + enc-pub resolution used by the endpoint.
    from nexus.api import local as local_api
    monkeypatch.setattr(local_api, "_resolve_dm_target",
                        lambda u: _coro("1.2.3.4"))
    monkeypatch.setattr(local_api, "_get_or_fetch_peer_enc_pub",
                        lambda ip: _coro(recip_pub))

    class _Req:
        async def json(self):
            return {"msg_id": "m-out", "from_uuid": "recipient-uuid"}

    res = asyncio.run(peer_api.peer_attachment_pull(_Req()))
    sealed = base64.b64decode(res["sealed_b64"])
    assert group_ecies.ecies_open(sealed, recip_priv) == raw


def test_dm_attachment_pull_rejects_wrong_requester(isolated_db, monkeypatch):
    from fastapi import HTTPException

    from nexus.api import peer as peer_api

    ca.store_blob("m-out", b"data")

    async def _go():
        async with get_session() as s:
            s.add(DirectMessage(
                msg_id="m-out", peer_uuid="recipient-uuid", direction="out",
                body="", sent_at=iso_now(), received_at=iso_now(),
                attach_kind="foreign",
            ))
            await s.commit()

    asyncio.run(_go())

    class _Req:
        async def json(self):
            return {"msg_id": "m-out", "from_uuid": "someone-else"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(peer_api.peer_attachment_pull(_Req()))
    assert exc.value.status_code == 404


async def _coro(v):
    return v
