"""Wave 7.2 — manifest endpoint."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.networking.storage_pump import CHUNK_PLAINTEXT_BYTES
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


def _seed_deposit(
    deposit_id: str,
    *,
    filename: str = "vacation.jpg",
    size: int = 24_000,
    chunk_count: int = 3,
    password: str = PASSWORD,
) -> bytes:
    """Insert a depositor row with a sealed manifest. Returns the salt."""
    from nexus.storage import ForeignStorageDeposit, get_session

    salt = os.urandom(SALT_BYTES)
    key = derive_key(password, salt)
    sealed = seal_manifest(
        key, {"deposit_id": deposit_id, "filename": filename, "size": size}
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
                    total_bytes=size,
                    chunk_count=chunk_count,
                    transport="stream",
                    salt=salt,
                    encrypted_manifest=sealed,
                )
            )
            await db.commit()

    asyncio.run(_go())
    return salt


def test_manifest_requires_unlocked_deposit(client):
    _seed_deposit("dep-locked")
    res = client.get("/local/foreign_storage/manifest/dep-locked")
    assert res.status_code == 401


def test_manifest_unknown_deposit_returns_404(client):
    # Stash a key for a deposit that doesn't exist in the DB so we get past
    # the unlock check but fail the row lookup.
    foreign_storage_keys.store("ghost", b"\x00" * 32)
    res = client.get("/local/foreign_storage/manifest/ghost")
    assert res.status_code == 404


def test_manifest_returns_unsealed_fields_with_mime(client):
    _seed_deposit(
        "dep-jpg", filename="trip.jpg", size=12_345, chunk_count=2
    )
    client.post(
        "/local/foreign_storage/unlock/dep-jpg", json={"password": PASSWORD}
    )
    res = client.get("/local/foreign_storage/manifest/dep-jpg")
    assert res.status_code == 200
    body = res.json()
    assert body["filename"] == "trip.jpg"
    assert body["size"] == 12_345
    assert body["mime"] == "image/jpeg"
    assert body["chunk_count"] == 2
    assert body["chunk_size"] == CHUNK_PLAINTEXT_BYTES


def test_manifest_unknown_extension_falls_back_to_octet_stream(client):
    _seed_deposit("dep-bin", filename="random.weirdext", size=100)
    client.post(
        "/local/foreign_storage/unlock/dep-bin", json={"password": PASSWORD}
    )
    res = client.get("/local/foreign_storage/manifest/dep-bin")
    assert res.status_code == 200
    assert res.json()["mime"] == "application/octet-stream"


def test_manifest_with_corrupted_sealed_blob_returns_401(client):
    """Defensive — unlock would normally have caught this, but if the cached
    key is somehow wrong for the manifest, the endpoint must not crash."""
    from nexus.storage import ForeignStorageDeposit, get_session

    _seed_deposit("dep-corrupt")
    # Cache a key that won't unseal the stored manifest.
    foreign_storage_keys.store("dep-corrupt", b"\x00" * 32)

    res = client.get("/local/foreign_storage/manifest/dep-corrupt")
    assert res.status_code == 401
