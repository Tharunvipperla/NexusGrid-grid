"""Wave 7.3 — streaming preview endpoint + chunk pump."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import _parse_range_header, router as local_router
from nexus.networking.storage_pump import CHUNK_PLAINTEXT_BYTES
from nexus.runtime import foreign_storage_keys, preview_pump
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


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    foreign_storage_keys.reset_for_testing()
    preview_pump.reset_for_testing()
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
    preview_pump.reset_for_testing()
    tokens._reset_for_testing()


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_with_chunks(
    deposit_id: str,
    plaintext: bytes,
    *,
    filename: str = "trip.bin",
    password: str = PASSWORD,
) -> tuple[bytes, list[bytes]]:
    """Seed depositor row + return (key, encrypted_chunks).

    The encrypted chunks are NOT stored in the cache here — tests stash
    them into ``preview_pump._pending`` Futures themselves to simulate
    host replies, or pre-populate ``preview_pump._cache`` with already
    decrypted plaintext so the endpoint never tries to send a frame.
    """
    from nexus.storage import ForeignStorageDeposit, get_session

    salt = os.urandom(SALT_BYTES)
    key = derive_key(password, salt)
    chunks: list[bytes] = []
    for idx in range(0, len(plaintext), CHUNK_PLAINTEXT_BYTES):
        slice_ = plaintext[idx : idx + CHUNK_PLAINTEXT_BYTES]
        chunk_idx = idx // CHUNK_PLAINTEXT_BYTES
        chunks.append(encrypt_chunk(key, slice_, chunk_idx))

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
                    role="depositor",
                    depositor_uuid="self",
                    host_uuid="peer",
                    status="stored",
                    total_bytes=len(plaintext),
                    chunk_count=len(chunks),
                    transport="stream",
                    salt=salt,
                    encrypted_manifest=sealed,
                )
            )
            await db.commit()

    asyncio.run(_go())
    return key, chunks


def _prefill_cache(deposit_id: str, plaintext: bytes) -> None:
    """Skip the host round-trip by pre-populating the plaintext cache."""
    for idx in range(0, len(plaintext), CHUNK_PLAINTEXT_BYTES):
        chunk_idx = idx // CHUNK_PLAINTEXT_BYTES
        slice_ = plaintext[idx : idx + CHUNK_PLAINTEXT_BYTES]
        preview_pump._store_in_cache(deposit_id, chunk_idx, slice_)


# ---------------------------------------------------------------------------
# _parse_range_header — pure, no fixtures.
# ---------------------------------------------------------------------------


def test_parse_range_open_ended():
    assert _parse_range_header("bytes=100-", 1000) == (100, 999)


def test_parse_range_explicit():
    assert _parse_range_header("bytes=100-199", 1000) == (100, 199)


def test_parse_range_suffix():
    # last 50 bytes
    assert _parse_range_header("bytes=-50", 1000) == (950, 999)


def test_parse_range_clamps_end_to_total():
    assert _parse_range_header("bytes=900-9999", 1000) == (900, 999)


def test_parse_range_unsatisfiable_returns_none():
    assert _parse_range_header("bytes=2000-3000", 1000) is None
    assert _parse_range_header("bytes=abc-def", 1000) is None
    assert _parse_range_header("notbytes=0-100", 1000) is None
    assert _parse_range_header("", 1000) is None


# ---------------------------------------------------------------------------
# preview endpoint
# ---------------------------------------------------------------------------


def test_preview_requires_unlocked_deposit(client):
    _seed_with_chunks("dep-locked", b"x" * 100)
    res = client.get("/local/foreign_storage/preview/dep-locked")
    assert res.status_code == 401


def test_preview_unknown_deposit_returns_404(client):
    foreign_storage_keys.store("ghost", b"\x00" * 32)
    res = client.get("/local/foreign_storage/preview/ghost")
    assert res.status_code == 404


def test_preview_full_get_returns_200_with_full_payload(client):
    payload = b"hello world " * 1000  # ~12 KB → 2 chunks
    _seed_with_chunks("dep-full", payload, filename="hello.txt")
    client.post(
        "/local/foreign_storage/unlock/dep-full", json={"password": PASSWORD}
    )
    _prefill_cache("dep-full", payload)

    res = client.get("/local/foreign_storage/preview/dep-full")
    assert res.status_code == 200
    assert res.headers["accept-ranges"] == "bytes"
    assert res.headers["content-type"].startswith("text/plain")
    assert res.content == payload


def test_preview_range_returns_206_with_exact_slice(client):
    payload = bytes(range(256)) * 100  # 25 600 bytes → 4 chunks
    _seed_with_chunks("dep-range", payload, filename="bytes.bin")
    client.post(
        "/local/foreign_storage/unlock/dep-range", json={"password": PASSWORD}
    )
    _prefill_cache("dep-range", payload)

    res = client.get(
        "/local/foreign_storage/preview/dep-range",
        headers={"Range": "bytes=0-99"},
    )
    assert res.status_code == 206
    assert res.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    assert res.headers["content-length"] == "100"
    assert res.content == payload[:100]


def test_preview_range_straddling_chunk_boundary(client):
    payload = bytes((i & 0xFF) for i in range(20_000))  # ~3 chunks
    _seed_with_chunks("dep-straddle", payload, filename="img.dat")
    client.post(
        "/local/foreign_storage/unlock/dep-straddle",
        json={"password": PASSWORD},
    )
    _prefill_cache("dep-straddle", payload)

    # Pick a window that crosses the 8 KB boundary.
    start, end = 8000, 9000
    res = client.get(
        "/local/foreign_storage/preview/dep-straddle",
        headers={"Range": f"bytes={start}-{end}"},
    )
    assert res.status_code == 206
    assert res.content == payload[start : end + 1]
    assert res.headers["content-length"] == str(end - start + 1)


def test_preview_unsatisfiable_range_returns_416(client):
    payload = b"x" * 100
    _seed_with_chunks("dep-416", payload)
    client.post(
        "/local/foreign_storage/unlock/dep-416", json={"password": PASSWORD}
    )
    _prefill_cache("dep-416", payload)

    res = client.get(
        "/local/foreign_storage/preview/dep-416",
        headers={"Range": "bytes=500-600"},
    )
    assert res.status_code == 416
    assert res.headers["content-range"] == f"bytes */{len(payload)}"


# ---------------------------------------------------------------------------
# preview_pump direct unit tests
# ---------------------------------------------------------------------------


def test_pump_fetch_resolves_via_in_flight_future():
    preview_pump.reset_for_testing()
    key = b"\x11" * 32
    plaintext = b"abc" * 1000
    cipher = encrypt_chunk(key, plaintext, 0)

    sent: list[tuple[str, int]] = []

    async def _fake_open(host, idx):
        sent.append((host, idx))
        # Simulate the host reply landing on the workflow handler.
        await asyncio.sleep(0)
        preview_pump.resolve_chunk("dep-x", idx, cipher)

    async def _go():
        out = await preview_pump.fetch_plaintext(
            "dep-x", key, "host-1", 0, request_open=_fake_open
        )
        return out

    result = asyncio.run(_go())
    assert result == plaintext
    assert sent == [("host-1", 0)]


def test_pump_cache_hit_skips_request_open():
    preview_pump.reset_for_testing()
    key = b"\x22" * 32
    plaintext = b"cached"
    preview_pump._store_in_cache("dep-y", 5, plaintext)

    called = False

    async def _fake_open(host, idx):
        nonlocal called
        called = True

    async def _go():
        return await preview_pump.fetch_plaintext(
            "dep-y", key, "host-1", 5, request_open=_fake_open
        )

    assert asyncio.run(_go()) == plaintext
    assert called is False


def test_preview_lock_drops_pending_and_cache(client):
    payload = b"y" * 8192
    _seed_with_chunks("dep-lock-mid", payload)
    client.post(
        "/local/foreign_storage/unlock/dep-lock-mid",
        json={"password": PASSWORD},
    )
    _prefill_cache("dep-lock-mid", payload)
    assert preview_pump.cache_stats()["entries"] >= 1

    client.post("/local/foreign_storage/lock/dep-lock-mid")
    # All cache entries for this deposit are wiped.
    remaining = [
        k for k in preview_pump._cache if k[0] == "dep-lock-mid"
    ]
    assert remaining == []
