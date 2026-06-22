"""Wave 10 follow-up: host-side materialize-to-disk for view-granted deposits.

The view-grant flow now decrypts the deposit's chunks to plaintext on the
host's disk on demand. Once written, the plaintext persists across the
depositor's "revoke" (which is now strictly a RAM-key drop). The host can
explicitly delete the plaintext via the delete endpoint to reclaim disk.

Covered:
* materialize → file appears at expected path with original bytes
* materialize is idempotent (second call returns same path, no re-decrypt)
* materialize blocked when grant not set (403)
* materialize blocked when key not cached (409)
* delete removes the plaintext but leaves the cached key + ciphertext alone
* delete is idempotent (clearing the column even when path is empty)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.core import identity, paths
from nexus.runtime import foreign_storage_keys
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.security.deposit_crypto import (
    SALT_BYTES,
    derive_key,
    encrypt_chunk,
    seal_manifest,
)
from nexus.storage import database

PASSWORD = "correct horse battery staple"
NODE_PORT = 18900


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    foreign_storage_keys.reset_for_testing()
    identity.set_node_port(NODE_PORT)
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


def _seed_host_deposit(
    deposit_id: str,
    plaintext: bytes,
    *,
    filename: str = "hello.txt",
    depositor_uuid: str = "depositor-uuid",
    grant: bool = True,
) -> bytes:
    """Seed a host-side deposit row + write encrypted chunks to disk.

    Returns the AES key. Caller decides whether to cache it.
    """
    from nexus.storage import ForeignStorageDeposit, get_session

    salt = os.urandom(SALT_BYTES)
    key = derive_key(PASSWORD, salt)

    # Write encrypted chunks under the on-disk layout the materialize
    # endpoint expects.
    chunks_dir = (
        paths.cache_dir(NODE_PORT)
        / "foreign_storage"
        / depositor_uuid
        / deposit_id
    )
    chunks_dir.mkdir(parents=True, exist_ok=True)

    # Small fixed chunk size so several chunks exercise the loop.
    CHUNK = 1024
    chunk_count = 0
    for idx in range(0, len(plaintext), CHUNK):
        slice_ = plaintext[idx : idx + CHUNK]
        chunk_idx = idx // CHUNK
        (chunks_dir / f"chunk_{chunk_idx:08d}.enc").write_bytes(
            encrypt_chunk(key, slice_, chunk_idx)
        )
        chunk_count += 1

    sealed = seal_manifest(
        key,
        {
            "deposit_id": deposit_id,
            "filename": filename,
            "size": len(plaintext),
        },
    )

    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="host",
                    depositor_uuid=depositor_uuid,
                    host_uuid="self",
                    status="stored",
                    total_bytes=len(plaintext),
                    chunk_count=chunk_count,
                    transport="stream",
                    salt=salt,
                    encrypted_manifest=sealed,
                    host_view_granted_at=(1234567 if grant else 0),
                )
            )
            await db.commit()

    asyncio.run(_go())
    return key


def test_materialize_writes_decrypted_file_to_disk(client, tmp_path):
    plaintext = b"hello world! " * 200  # ~2.6 KB → 3 chunks at CHUNK=1024
    key = _seed_host_deposit("dep-mat-1", plaintext, filename="greeting.txt")
    foreign_storage_keys.store("dep-mat-1", key)

    res = client.post("/local/foreign_storage/materialize_view/dep-mat-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["filename"] == "greeting.txt"
    assert body["already_materialized"] is False

    out_dir = Path(body["path"])
    out_file = out_dir / "greeting.txt"
    assert out_file.exists()
    assert out_file.read_bytes() == plaintext


def test_materialize_is_idempotent(client):
    plaintext = b"x" * 500
    key = _seed_host_deposit("dep-mat-2", plaintext)
    foreign_storage_keys.store("dep-mat-2", key)

    first = client.post("/local/foreign_storage/materialize_view/dep-mat-2").json()
    second = client.post("/local/foreign_storage/materialize_view/dep-mat-2").json()

    assert second["path"] == first["path"]
    assert second["already_materialized"] is True


def test_materialize_blocked_without_grant(client):
    plaintext = b"secret"
    key = _seed_host_deposit("dep-mat-3", plaintext, grant=False)
    foreign_storage_keys.store("dep-mat-3", key)

    res = client.post("/local/foreign_storage/materialize_view/dep-mat-3")
    assert res.status_code == 403


def test_materialize_blocked_without_cached_key(client):
    plaintext = b"key-not-cached"
    _seed_host_deposit("dep-mat-4", plaintext, grant=True)
    # Intentionally do NOT cache the key.

    res = client.post("/local/foreign_storage/materialize_view/dep-mat-4")
    assert res.status_code == 409


def test_delete_removes_plaintext_only(client):
    plaintext = b"to-be-deleted" * 100
    key = _seed_host_deposit("dep-mat-5", plaintext)
    foreign_storage_keys.store("dep-mat-5", key)

    mat = client.post("/local/foreign_storage/materialize_view/dep-mat-5").json()
    out_dir = Path(mat["path"])
    assert out_dir.exists()

    res = client.post("/local/foreign_storage/delete_view_decrypted/dep-mat-5")
    assert res.status_code == 200
    assert not out_dir.exists()

    # Cached key and ciphertext untouched — host can re-materialize.
    assert foreign_storage_keys.get("dep-mat-5") == key
    again = client.post("/local/foreign_storage/materialize_view/dep-mat-5").json()
    assert again["already_materialized"] is False
    assert Path(again["path"]).exists()


def test_delete_is_idempotent(client):
    plaintext = b"x"
    key = _seed_host_deposit("dep-mat-6", plaintext)
    foreign_storage_keys.store("dep-mat-6", key)

    # First delete (never materialized) — column is already empty.
    res1 = client.post("/local/foreign_storage/delete_view_decrypted/dep-mat-6")
    assert res1.status_code == 200
    # Second delete — also fine.
    res2 = client.post("/local/foreign_storage/delete_view_decrypted/dep-mat-6")
    assert res2.status_code == 200
