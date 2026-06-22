"""Wave 7.1 — unlock / lock / unlocked endpoints."""

from __future__ import annotations

import asyncio

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
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_deposit(deposit_id: str, password: str = PASSWORD) -> bytes:
    """Insert a depositor row with a sealed manifest. Returns the salt."""
    import os

    from nexus.storage import ForeignStorageDeposit, get_session

    salt = os.urandom(SALT_BYTES)
    key = derive_key(password, salt)
    sealed = seal_manifest(
        key, {"deposit_id": deposit_id, "filename": "x.bin", "size": 100}
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
    return salt


def test_unlock_with_correct_password(client):
    _seed_deposit("dep-1")
    res = client.post(
        "/local/foreign_storage/unlock/dep-1", json={"password": PASSWORD}
    )
    assert res.status_code == 200
    assert foreign_storage_keys.is_unlocked("dep-1")


def test_unlock_with_wrong_password_returns_401_and_caches_nothing(client):
    _seed_deposit("dep-2")
    res = client.post(
        "/local/foreign_storage/unlock/dep-2", json={"password": "wrong"}
    )
    assert res.status_code == 401
    assert not foreign_storage_keys.is_unlocked("dep-2")


def test_unlock_unknown_deposit_returns_404(client):
    res = client.post(
        "/local/foreign_storage/unlock/nope", json={"password": PASSWORD}
    )
    assert res.status_code == 404


def test_lock_drops_the_key(client):
    _seed_deposit("dep-3")
    client.post(
        "/local/foreign_storage/unlock/dep-3", json={"password": PASSWORD}
    )
    assert foreign_storage_keys.is_unlocked("dep-3")
    res = client.post("/local/foreign_storage/lock/dep-3")
    assert res.status_code == 200
    assert res.json()["was_unlocked"] is True
    assert not foreign_storage_keys.is_unlocked("dep-3")


def test_lock_when_not_unlocked_is_idempotent(client):
    res = client.post("/local/foreign_storage/lock/never")
    assert res.status_code == 200
    assert res.json()["was_unlocked"] is False


def test_unlocked_listing_excludes_locked_and_omits_key_bytes(client):
    _seed_deposit("dep-4")
    _seed_deposit("dep-5")
    client.post(
        "/local/foreign_storage/unlock/dep-4", json={"password": PASSWORD}
    )
    res = client.get("/local/foreign_storage/unlocked")
    assert res.status_code == 200
    rows = res.json()["unlocked"]
    assert len(rows) == 1
    assert rows[0]["deposit_id"] == "dep-4"
    # Critical: must never leak key material through this endpoint.
    assert "key" not in rows[0]
    assert "password" not in rows[0]
