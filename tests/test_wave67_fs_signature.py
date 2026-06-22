"""Wave 67 — cross-node depositor-terms signature.

The depositor-terms HMAC used the node-local ``.nexus_secret`` on both
ends, so an offer signed on node A could never verify on node B unless
the operators shared the secret file — every cross-node deposit died with
``storage.deposit_unsigned_terms``. The fix signs the wire frame with the
per-pair ``Peer.signing_key`` (like task bundles/results) and verifies
host-side with the sender's pair key, keeping the default-secret check as
a fallback for shared-secret deployments.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import foreign_storage_quota
from nexus.runtime.foreign_storage_workflow import (
    _handle_offer,
    peer_signing_key,
)
from nexus.security import tokens
from nexus.security.crypto import sign_bytes
from nexus.security.foreign_storage_terms import DEFAULT_DEPOSITOR_TERMS
from nexus.storage import ForeignStorageDeposit, Peer, database, get_session

from sqlalchemy import select


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    monkeypatch.setattr(foreign_storage_quota, "effective_free_gb", lambda: 100.0)
    db_path = tmp_path / "test.db"
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
    LOCAL_SETTINGS.pop("foreign_storage_accept_offers", None)
    tokens._reset_for_testing()


@pytest.fixture
def captured_frames(monkeypatch):
    out: list[tuple[str, dict]] = []

    async def _fake_send(peer_id, frame):
        out.append((peer_id, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", _fake_send)
    return out


PAIR_KEY = "pair-key-negotiated-at-handshake"
TC_BYTES = DEFAULT_DEPOSITOR_TERMS.encode("utf-8")


async def _add_peer(peer_id: str, skey: str) -> None:
    async with get_session() as db:
        db.add(Peer(ip=peer_id, status="trusted", signing_key=skey))
        await db.commit()


async def _host_row(deposit_id: str):
    async with get_session() as db:
        return (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()


def _offer(deposit_id: str, sig: str) -> dict:
    return {
        "deposit_id": deposit_id,
        "total_bytes": 1024,
        "chunk_count": 1,
        "ttl_days": 1,
        "depositor_signature": sig,
    }


def test_peer_signing_key_lookup(isolated_db):
    asyncio.run(_add_peer("peer-a", PAIR_KEY))
    assert asyncio.run(peer_signing_key("peer-a")) == PAIR_KEY
    assert asyncio.run(peer_signing_key("missing-peer")) == ""
    assert asyncio.run(peer_signing_key("")) == ""


def test_offer_accepted_with_pair_key_signature(isolated_db, captured_frames):
    asyncio.run(_add_peer("peer-a", PAIR_KEY))
    sig = sign_bytes("foreign_storage_terms", "dep-pair", TC_BYTES, key=PAIR_KEY)
    asyncio.run(_handle_offer("peer-a", _offer("dep-pair", sig)))
    row = asyncio.run(_host_row("dep-pair"))
    assert row is not None and row.status == "offered"


def test_offer_rejected_when_signed_with_foreign_node_secret(
    isolated_db, captured_frames
):
    """The pre-fix failure mode: the depositor signed with ITS node-local
    secret, which neither the pair key nor our local secret matches."""
    asyncio.run(_add_peer("peer-a", PAIR_KEY))
    sig = sign_bytes(
        "foreign_storage_terms", "dep-foreign", TC_BYTES,
        key="some-other-nodes-.nexus_secret",
    )
    asyncio.run(_handle_offer("peer-a", _offer("dep-foreign", sig)))
    assert asyncio.run(_host_row("dep-foreign")) is None


def test_offer_accepted_via_shared_secret_fallback(isolated_db, captured_frames):
    """Deployments that share .nexus_secret (or same-dir dev setups) keep
    working: a default-key signature still verifies even when the sender
    has no Peer row / pair key."""
    sig = sign_bytes("foreign_storage_terms", "dep-shared", TC_BYTES)
    asyncio.run(_handle_offer("peer-unknown", _offer("dep-shared", sig)))
    row = asyncio.run(_host_row("dep-shared"))
    assert row is not None and row.status == "offered"


def test_offer_rejected_with_empty_signature(isolated_db, captured_frames):
    asyncio.run(_add_peer("peer-a", PAIR_KEY))
    asyncio.run(_handle_offer("peer-a", _offer("dep-empty", "")))
    assert asyncio.run(_host_row("dep-empty")) is None
