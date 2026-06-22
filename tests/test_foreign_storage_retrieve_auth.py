"""Regression: ``/foreign_storage/retrieve/{id}`` must verify the password.

The previous implementation derived a key from any password and cached
it without checking — wrong passwords silently kicked off downloads
that decrypted to garbage, and the bad key poisoned the in-RAM cache
so a subsequent Share View would ship a useless key to the host.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.runtime import foreign_storage_keys
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.security.deposit_crypto import (
    SALT_BYTES,
    derive_key,
    seal_manifest,
)
from nexus.storage import database


PASSWORD = "correct horse battery staple"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    foreign_storage_keys.reset_for_testing()
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
    foreign_storage_keys.reset_for_testing()
    tokens._reset_for_testing()


@pytest.fixture
def client(isolated_db, monkeypatch):
    # Stub the wire send so the endpoint can return without a real peer.
    async def _fake_send(peer_id, frame):
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", _fake_send)

    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_deposit(deposit_id: str, password: str = PASSWORD,
                  *, with_sealed_manifest: bool = True) -> None:
    from nexus.storage import ForeignStorageDeposit, get_session

    salt = os.urandom(SALT_BYTES)
    key = derive_key(password, salt)
    sealed = (
        seal_manifest(
            key, {"deposit_id": deposit_id, "filename": "x.bin", "size": 100}
        )
        if with_sealed_manifest
        else b""
    )

    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="depositor",
                    depositor_uuid="self",
                    host_uuid="peer",
                    status="stored",
                    total_bytes=100,
                    chunk_count=1,
                    transport="stream",
                    salt=salt,
                    encrypted_manifest=sealed,
                )
            )
            await db.commit()

    asyncio.run(_go())


def test_retrieve_with_wrong_password_returns_401(client):
    _seed_deposit("dep-bad")
    res = client.post(
        "/local/foreign_storage/retrieve/dep-bad",
        json={"password": "nope", "save_to_path": "/tmp/out.bin"},
    )
    assert res.status_code == 401
    # Nothing cached.
    assert not foreign_storage_keys.is_unlocked("dep-bad")


def test_retrieve_with_correct_password_caches_key(client):
    _seed_deposit("dep-ok")
    res = client.post(
        "/local/foreign_storage/retrieve/dep-ok",
        json={"password": PASSWORD, "save_to_path": "/tmp/out.bin"},
    )
    assert res.status_code == 200
    assert foreign_storage_keys.is_unlocked("dep-ok")


def test_retrieve_wrong_password_does_not_poison_existing_cache(client):
    """If a *correct* key is already cached (e.g. user previously
    unlocked), a wrong-password retrieve must NOT replace it with
    garbage. With the security fix it raises 401 and drops the cache;
    the user can re-unlock to restore a known-good key.
    """
    _seed_deposit("dep-poison")
    # First: legitimate unlock caches the correct key.
    res_unlock = client.post(
        "/local/foreign_storage/unlock/dep-poison", json={"password": PASSWORD}
    )
    assert res_unlock.status_code == 200
    good_key = foreign_storage_keys.get("dep-poison")
    assert good_key is not None

    # Second: a wrong-password retrieve attempt.
    res_bad = client.post(
        "/local/foreign_storage/retrieve/dep-poison",
        json={"password": "wrong", "save_to_path": "/tmp/out.bin"},
    )
    assert res_bad.status_code == 401
    # Critical: the cached entry must not be the wrong-password key.
    # Either the entry was dropped, or it still holds the original
    # good key.
    new_cached = foreign_storage_keys.get("dep-poison")
    if new_cached is not None:
        assert new_cached == good_key


def test_retrieve_refuses_when_no_sealed_manifest(client):
    """Defensive: without a sealed manifest we have no way to verify
    the password. Old rows that pre-date manifest sealing should not
    silently shovel bytes through with whatever key derives.
    """
    _seed_deposit("dep-nosealed", with_sealed_manifest=False)
    res = client.post(
        "/local/foreign_storage/retrieve/dep-nosealed",
        json={"password": PASSWORD, "save_to_path": "/tmp/out.bin"},
    )
    assert res.status_code == 409
