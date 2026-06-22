"""Wave 6.4 — host-side cloud-eviction pipeline tests.

Drives ``_evict_to_cloud`` end-to-end with a mocked provider: real DB,
real cred-crypto, real on-disk ciphertext chunks, mocked
``_send_to_peer`` / mocked provider so we can assert chunk-by-chunk
behaviour without a network or a real Drive account.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import AsyncIterator

import pytest

from nexus.core import STATE
from nexus.security import tokens
from nexus.security.cred_crypto import (
    EVICTION_NONCE_BYTES,
    wrap_for_transit,
)
from nexus.storage import database
from nexus.storage.cloud import PROVIDERS
from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Bind the engine to an in-process SQLite file; auto-dispose at teardown."""
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    async def _setup():
        await database.init_db(0, url=url)

    asyncio.run(_setup())
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()


@pytest.fixture
def state_reset():
    """Reset STATE.foreign_storage_pumps + per-test pump dirs."""
    prior_pumps = dict(STATE.foreign_storage_pumps)
    prior_throttle = getattr(STATE, "foreign_storage_throttle", None)
    STATE.foreign_storage_pumps.clear()
    setattr(STATE, "foreign_storage_throttle", None)
    yield
    STATE.foreign_storage_pumps.clear()
    STATE.foreign_storage_pumps.update(prior_pumps)
    setattr(STATE, "foreign_storage_throttle", prior_throttle)


@pytest.fixture
def captured_frames(monkeypatch):
    """Patch ``_send_to_peer`` to record every outgoing frame."""
    sent: list[tuple[str, dict]] = []

    async def _fake_send(peer_id: str, frame: dict) -> bool:
        sent.append((peer_id, frame))
        return True

    monkeypatch.setattr(
        "nexus.networking.tunnel._send_to_peer", _fake_send
    )
    return sent


# ---------------------------------------------------------------------------
# Mock provider — registers under "mock" in PROVIDERS
# ---------------------------------------------------------------------------

class _MockProvider(CloudProvider):
    name = "mock-test"

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.throttle_calls: list[int] = []

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "_MockProvider":
        if raw != b"good-creds":
            raise ValueError("mock: bad creds")
        inst = cls()
        _MockProvider.last_instance = inst
        return inst

    async def upload_stream(
        self,
        dest: str,
        object_name: str,
        chunks: AsyncIterator[bytes],
        total_bytes: int,
        throttle_acquire: ThrottleAcquire,
    ) -> str:
        async for c in chunks:
            await throttle_acquire(len(c))
            self.throttle_calls.append(len(c))
            self.received.append(c)
        return f"mock-object-id::{dest}::{object_name}"


class _ExplodingProvider(CloudProvider):
    name = "mock-boom"

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "_ExplodingProvider":
        return cls()

    async def upload_stream(
        self,
        dest: str,
        object_name: str,
        chunks: AsyncIterator[bytes],
        total_bytes: int,
        throttle_acquire: ThrottleAcquire,
    ) -> str:
        # Drain one chunk then explode.
        async for _c in chunks:
            raise RuntimeError("upload exploded mid-stream")
        return ""


@pytest.fixture
def register_mock_providers(monkeypatch):
    PROVIDERS["mock-test"] = _MockProvider
    PROVIDERS["mock-boom"] = _ExplodingProvider
    yield
    PROVIDERS.pop("mock-test", None)
    PROVIDERS.pop("mock-boom", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PEER_UUID = "192.0.2.7:9000"
SIGNING_KEY = "shared-trusted-peer-secret-abc"


async def _seed_peer_and_deposit(
    deposit_id: str,
    chunk_count: int,
    pump_dir: Path,
) -> None:
    from nexus.storage import (
        ForeignStorageDeposit,
        Peer,
        get_session,
    )

    async with get_session() as db:
        db.add(
            Peer(
                ip=PEER_UUID,
                status="trusted",
                role="worker",
                signing_key=SIGNING_KEY,
            )
        )
        db.add(
            ForeignStorageDeposit(
                deposit_id=deposit_id,
                role="host",
                depositor_uuid=PEER_UUID,
                host_uuid="self",
                status="stored",
                total_bytes=sum(_chunk_payload(i).__len__() for i in range(chunk_count)),
                chunk_count=chunk_count,
                transport="stream",
            )
        )
        await db.commit()

    STATE.foreign_storage_pumps[deposit_id] = {"dir": str(pump_dir)}


def _chunk_payload(idx: int) -> bytes:
    return f"chunk-{idx:08d}-payload".encode() * 4


def _write_chunks(pump_dir: Path, chunk_count: int) -> bytes:
    pump_dir.mkdir(parents=True, exist_ok=True)
    full = bytearray()
    for i in range(chunk_count):
        blob = _chunk_payload(i)
        (pump_dir / f"chunk_{i:08d}.enc").write_bytes(blob)
        full.extend(blob)
    return bytes(full)


def _build_eviction_frame(
    deposit_id: str,
    *,
    provider_name: str,
    creds_plain: bytes,
    nonce: bytes | None = None,
    signing_key: str = SIGNING_KEY,
) -> dict:
    nonce = nonce if nonce is not None else os.urandom(EVICTION_NONCE_BYTES)
    wrapped = wrap_for_transit(signing_key, nonce, creds_plain)
    return {
        "type": "storage_eviction_response",
        "deposit_id": deposit_id,
        "action": "cloud",
        "cloud_provider": provider_name,
        "cloud_dest": "folder-xyz",
        "cloud_eviction_nonce_b64": base64.b64encode(nonce).decode(),
        "cloud_credential_blob_b64": base64.b64encode(wrapped).decode(),
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_uploads_ciphertext_and_marks_purged(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response
    from nexus.storage import ForeignStorageDeposit, get_session
    from sqlalchemy import select

    deposit_id = "dep-happy"
    pump_dir = tmp_path / "pump"
    expected_ciphertext = _write_chunks(pump_dir, chunk_count=3)

    async def main():
        await _seed_peer_and_deposit(deposit_id, 3, pump_dir)
        frame = _build_eviction_frame(
            deposit_id, provider_name="mock-test", creds_plain=b"good-creds"
        )
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    inst = _MockProvider.last_instance
    assert b"".join(inst.received) == expected_ciphertext
    assert inst.throttle_calls == [len(_chunk_payload(i)) for i in range(3)]

    # ciphertext wiped on disk
    assert list(pump_dir.glob("chunk_*.enc")) == []

    async def fetch():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id
                    )
                )
            ).scalar_one()

    row = asyncio.run(fetch())
    assert row.status == "purged"
    assert row.cloud_provider == "mock-test"
    assert row.cloud_dest == "folder-xyz"
    assert row.cloud_object_id.startswith("mock-object-id::folder-xyz::")
    assert row.cloud_uploaded_at > 0

    # Depositor saw a complete frame
    completes = [f for _p, f in captured_frames
                 if f.get("type") == "storage_cloud_upload_complete"]
    assert len(completes) == 1
    assert completes[0]["deposit_id"] == deposit_id


# ---------------------------------------------------------------------------
# Failure paths fall back to db_grace and emit failed frame
# ---------------------------------------------------------------------------

def test_unknown_provider_fails_fast(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response
    from nexus.storage import ForeignStorageDeposit, get_session
    from sqlalchemy import select

    deposit_id = "dep-unknown"
    pump_dir = tmp_path / "pump"
    _write_chunks(pump_dir, chunk_count=1)

    async def main():
        await _seed_peer_and_deposit(deposit_id, 1, pump_dir)
        frame = _build_eviction_frame(
            deposit_id, provider_name="not-a-real-provider", creds_plain=b"x"
        )
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    failed = [f for _p, f in captured_frames
              if f.get("type") == "storage_cloud_upload_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "provider_unknown"

    # ciphertext still on disk (no purge on failure)
    assert (pump_dir / "chunk_00000000.enc").exists()

    async def fetch():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id
                    )
                )
            ).scalar_one()

    row = asyncio.run(fetch())
    assert row.status == "in_db_grace"


def test_provider_explosion_falls_back_to_db_grace(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response
    from nexus.storage import ForeignStorageDeposit, get_session
    from sqlalchemy import select

    deposit_id = "dep-boom"
    pump_dir = tmp_path / "pump"
    _write_chunks(pump_dir, chunk_count=2)

    async def main():
        await _seed_peer_and_deposit(deposit_id, 2, pump_dir)
        frame = _build_eviction_frame(
            deposit_id, provider_name="mock-boom", creds_plain=b"any"
        )
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    failed = [f for _p, f in captured_frames
              if f.get("type") == "storage_cloud_upload_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"].startswith("upload_failed:RuntimeError")

    # ciphertext NOT deleted
    assert (pump_dir / "chunk_00000000.enc").exists()
    assert (pump_dir / "chunk_00000001.enc").exists()

    async def fetch():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id
                    )
                )
            ).scalar_one()

    row = asyncio.run(fetch())
    assert row.status == "in_db_grace"


def test_corrupted_credential_blob_fails(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response

    deposit_id = "dep-bad-creds"
    pump_dir = tmp_path / "pump"
    _write_chunks(pump_dir, chunk_count=1)

    async def main():
        await _seed_peer_and_deposit(deposit_id, 1, pump_dir)
        frame = _build_eviction_frame(
            deposit_id, provider_name="mock-test", creds_plain=b"good-creds"
        )
        # Corrupt the wrapped blob.
        bad = bytearray(base64.b64decode(frame["cloud_credential_blob_b64"]))
        bad[-1] ^= 0xFF
        frame["cloud_credential_blob_b64"] = base64.b64encode(bytes(bad)).decode()
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    failed = [f for _p, f in captured_frames
              if f.get("type") == "storage_cloud_upload_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "creds_decrypt_failed"


def test_missing_peer_signing_key_fails(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    """No peer row → no signing_key → fail fast, never decrypt creds."""
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response
    from nexus.storage import ForeignStorageDeposit, get_session

    deposit_id = "dep-no-peer"
    pump_dir = tmp_path / "pump"
    _write_chunks(pump_dir, chunk_count=1)

    async def main():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="host",
                    depositor_uuid=PEER_UUID,
                    host_uuid="self",
                    status="stored",
                    total_bytes=10,
                    chunk_count=1,
                    transport="stream",
                )
            )
            await db.commit()
        STATE.foreign_storage_pumps[deposit_id] = {"dir": str(pump_dir)}
        frame = _build_eviction_frame(
            deposit_id, provider_name="mock-test", creds_plain=b"good-creds"
        )
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    failed = [f for _p, f in captured_frames
              if f.get("type") == "storage_cloud_upload_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "no_peer_signing_key"


def test_missing_cloud_fields_fails(
    isolated_db, state_reset, captured_frames, register_mock_providers, tmp_path
):
    from nexus.runtime.foreign_storage_workflow import _handle_eviction_response

    deposit_id = "dep-missing-fields"
    pump_dir = tmp_path / "pump"
    _write_chunks(pump_dir, chunk_count=1)

    async def main():
        await _seed_peer_and_deposit(deposit_id, 1, pump_dir)
        # Missing cloud_eviction_nonce_b64.
        frame = {
            "type": "storage_eviction_response",
            "deposit_id": deposit_id,
            "action": "cloud",
            "cloud_provider": "mock-test",
            "cloud_dest": "folder-xyz",
            "cloud_eviction_nonce_b64": "",
            "cloud_credential_blob_b64": base64.b64encode(b"ignored").decode(),
        }
        await _handle_eviction_response(PEER_UUID, frame)

    asyncio.run(main())

    failed = [f for _p, f in captured_frames
              if f.get("type") == "storage_cloud_upload_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "missing_cloud_fields"
