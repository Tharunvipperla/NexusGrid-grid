"""P8.8 — file-change + chunk-loss detection.

Layers covered here:

1. SHA-256 of the source file is sealed into the depositor's manifest
   at deposit time.
2. ``transfer_deposit`` aborts mid-pump on size/mtime drift.
3. ``foreign_storage_resume`` rejects a re-hashed file that doesn't
   match the sealed SHA.
4. Host-side ``_handle_complete`` scans the chunk dir, emits
   ``storage_missing_chunks`` for gaps (or for "host wiped everything"),
   flips to ``stored`` only when complete.
5. Depositor-side ``_handle_missing_chunks`` re-launches the pump for
   only the gap indices, bounded by ``fs_transit_max_retries``.
6. Host-restart pump rebuild: ``_handle_resume_request`` re-creates the
   in-RAM pump entry from the DB row + on-disk dir.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime import foreign_storage_keys
from nexus.runtime.foreign_storage_workflow import (
    _handle_complete,
    _handle_missing_chunks,
    _handle_resume_request,
)
from nexus.security import tokens
from nexus.security.deposit_crypto import (
    derive_key,
    seal_manifest,
    unseal_manifest,
)
from nexus.storage import ForeignStorageDeposit, database, get_session


DEP_ID = "dep-p88-1"
DEPOSITOR = "10.0.0.1:9000"
HOST = "10.0.0.2:9000"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
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
    tokens._reset_for_testing()


@pytest.fixture(autouse=True)
def _reset_state():
    foreign_storage_keys.reset_for_testing()
    STATE.foreign_storage_pumps.clear()
    STATE.foreign_storage_missing_rounds.clear()
    yield
    foreign_storage_keys.reset_for_testing()
    STATE.foreign_storage_pumps.clear()
    STATE.foreign_storage_missing_rounds.clear()


def _seed_depositor(status: str, **overrides) -> None:
    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=HOST,
                    status=status,
                    total_bytes=overrides.get("total_bytes", 1024),
                    chunk_count=overrides.get("chunk_count", 4),
                    transport="stream",
                    salt=overrides.get("salt", b"\x00" * 16),
                    password_hint="",
                    ttl_days=30,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    depositor_signature="sig-x",
                    encrypted_manifest=overrides.get("encrypted_manifest", b""),
                    transferred_chunks=overrides.get("transferred_chunks", 0),
                    retry_count=overrides.get("retry_count", 0),
                    last_progress_at=overrides.get("last_progress_at", ""),
                    pause_reason=overrides.get("pause_reason", ""),
                )
            )
            await db.commit()
    asyncio.run(_go())


def _seed_host(status: str, **overrides) -> None:
    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="host",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=HOST,
                    status=status,
                    total_bytes=overrides.get("total_bytes", 1024),
                    chunk_count=overrides.get("chunk_count", 4),
                    transport="stream",
                    salt=overrides.get("salt", b"\x00" * 16),
                    password_hint="",
                    ttl_days=30,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    depositor_signature="sig-x",
                    last_progress_at=overrides.get("last_progress_at", ""),
                )
            )
            await db.commit()
    asyncio.run(_go())


def _row_status(role: str = "depositor") -> str:
    async def _go():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID,
                        ForeignStorageDeposit.role == role,
                    )
                )
            ).scalar_one().status
    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Layer 1: SHA-256 sealed into manifest at deposit time
# ---------------------------------------------------------------------------

def test_manifest_carries_sha256_field():
    salt = os.urandom(16)
    key = derive_key("hunter2", salt)
    payload = b"hello-p88" * 100
    sha = hashlib.sha256(payload).hexdigest()
    sealed = seal_manifest(
        key,
        {
            "deposit_id": "x",
            "filename": "x.bin",
            "size": len(payload),
            "sha256": sha,
            "mtime_ns": 1234567890,
        },
    )
    out = unseal_manifest(key, sealed)
    assert out["sha256"] == sha
    assert out["mtime_ns"] == 1234567890


# ---------------------------------------------------------------------------
# Layer 2: mid-pump stat-check (pure-function test of _stat_file/_fstat_file)
# ---------------------------------------------------------------------------

def test_fstat_detects_size_drift(tmp_path):
    from nexus.networking.storage_pump import _fstat_file, _stat_file

    f = tmp_path / "drift.bin"
    f.write_bytes(b"hello")
    before = _stat_file(f)
    with f.open("rb") as fh:
        live_pre = _fstat_file(fh)
        # Truncate the file under us via a second handle (simulates user edit).
        f.write_bytes(b"hello-extra-bytes")
        live_post = _fstat_file(fh)
    assert before["size"] == 5
    assert live_pre["size"] == 5
    assert live_post["size"] != before["size"]


# ---------------------------------------------------------------------------
# Layer 4: host-side complete scan-and-detect
# ---------------------------------------------------------------------------

def test_complete_with_full_dir_flips_to_stored(isolated_db, monkeypatch, tmp_path):
    _seed_host("transferring", chunk_count=3)
    dep_dir = tmp_path / "fs"
    dep_dir.mkdir()
    for i in range(3):
        (dep_dir / f"chunk_{i:08d}.enc").write_bytes(b"x")

    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: dep_dir,
    )
    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_handle_complete(
        DEPOSITOR,
        {"type": "storage_complete", "deposit_id": DEP_ID,
         "depositor_signature_final": "final-sig"},
    ))
    assert _row_status("host") == "stored"
    # No missing_chunks frame should have been sent.
    assert not any(
        f.get("type") == "storage_missing_chunks" for _, f in sent
    )


def test_complete_with_gaps_emits_missing_chunks(isolated_db, monkeypatch, tmp_path):
    _seed_host("transferring", chunk_count=5)
    dep_dir = tmp_path / "fs"
    dep_dir.mkdir()
    # Drop chunks 1 and 3 — they're "missing" from the host.
    for i in [0, 2, 4]:
        (dep_dir / f"chunk_{i:08d}.enc").write_bytes(b"x")

    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: dep_dir,
    )
    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_handle_complete(
        DEPOSITOR,
        {"type": "storage_complete", "deposit_id": DEP_ID,
         "depositor_signature_final": "final-sig"},
    ))
    # Row stayed at transferring — not stored.
    assert _row_status("host") == "transferring"
    missing_frames = [
        f for _, f in sent if f.get("type") == "storage_missing_chunks"
    ]
    assert len(missing_frames) == 1
    assert missing_frames[0]["missing"] == [1, 3]


def test_complete_with_host_wiped_everything_signals_full_resend(
    isolated_db, monkeypatch, tmp_path,
):
    _seed_host("transferring", chunk_count=3)
    empty_dir = tmp_path / "fs-empty"
    empty_dir.mkdir()

    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: empty_dir,
    )
    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_handle_complete(
        DEPOSITOR,
        {"type": "storage_complete", "deposit_id": DEP_ID},
    ))
    assert _row_status("host") == "transferring"
    missing_frames = [
        f for _, f in sent if f.get("type") == "storage_missing_chunks"
    ]
    assert len(missing_frames) == 1
    assert missing_frames[0]["missing"] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Layer 5: depositor-side missing-chunks handler
# ---------------------------------------------------------------------------

def test_missing_chunks_unauthorized_sender_drops(isolated_db, monkeypatch):
    _seed_depositor("transferring", chunk_count=5)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    async def fake_send(target, frame):  # pragma: no cover - should not be called
        raise AssertionError("missing_chunks must reject stranger")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_handle_missing_chunks(
        "stranger:9000",
        {"type": "storage_missing_chunks", "deposit_id": DEP_ID,
         "missing": [1, 3]},
    ))
    assert _row_status("depositor") == "transferring"


def test_missing_chunks_empty_list_flips_to_stored(isolated_db):
    _seed_depositor("transferring", chunk_count=5)
    asyncio.run(_handle_missing_chunks(
        HOST,
        {"type": "storage_missing_chunks", "deposit_id": DEP_ID,
         "missing": []},
    ))
    assert _row_status("depositor") == "stored"


def test_missing_chunks_caps_at_max_retries_and_fails(isolated_db, monkeypatch):
    LOCAL_SETTINGS["fs_transit_max_retries"] = 2
    _seed_depositor("transferring", chunk_count=5)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")
    STATE.foreign_storage_missing_rounds[DEP_ID] = 2  # next call is round 3

    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    try:
        asyncio.run(_handle_missing_chunks(
            HOST,
            {"type": "storage_missing_chunks", "deposit_id": DEP_ID,
             "missing": [1, 3]},
        ))
    finally:
        LOCAL_SETTINGS["fs_transit_max_retries"] = 5

    assert _row_status("depositor") == "failed_in_transit"
    assert any(f.get("type") == "storage_delete_now" for _, f in sent)
    assert foreign_storage_keys.get(DEP_ID) is None


def test_missing_chunks_under_cap_launches_resend(isolated_db, monkeypatch, tmp_path):
    LOCAL_SETTINGS["fs_transit_max_retries"] = 5
    _seed_depositor("transferring", chunk_count=5)
    src = tmp_path / "src.bin"
    src.write_bytes(b"a" * 1024)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path=str(src))

    launched: list = []

    async def fake_launch(deposit_id, peer, file_path, key, missing):
        launched.append((deposit_id, peer, file_path, missing))

    monkeypatch.setattr(
        "nexus.runtime.foreign_storage_workflow._launch_missing_chunks_pump",
        fake_launch,
    )
    asyncio.run(_handle_missing_chunks(
        HOST,
        {"type": "storage_missing_chunks", "deposit_id": DEP_ID,
         "missing": [1, 3]},
    ))
    assert len(launched) == 1
    assert launched[0][3] == [1, 3]
    assert STATE.foreign_storage_missing_rounds[DEP_ID] == 1


# ---------------------------------------------------------------------------
# Layer 6: host-restart pump rebuild on resume_request
# ---------------------------------------------------------------------------

def test_resume_request_rebuilds_pump_after_host_restart(
    isolated_db, monkeypatch, tmp_path,
):
    _seed_host("paused_host_shutdown", chunk_count=5)
    dep_dir = tmp_path / "fs"
    dep_dir.mkdir()
    # Two chunks already on disk from before the restart.
    (dep_dir / "chunk_00000000.enc").write_bytes(b"a")
    (dep_dir / "chunk_00000001.enc").write_bytes(b"b")

    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: dep_dir,
    )

    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    # Sanity: pump entry is absent (simulates fresh process).
    assert DEP_ID not in STATE.foreign_storage_pumps

    asyncio.run(_handle_resume_request(
        DEPOSITOR,
        {"type": "storage_resume_request", "deposit_id": DEP_ID},
    ))

    pump = STATE.foreign_storage_pumps.get(DEP_ID)
    assert pump is not None
    assert pump["role"] == "host"
    assert pump["chunk_count"] == 5
    assert pump["dir"] == str(dep_dir)
    assert pump["received_idx"] == 1
    reply_frames = [
        f for _, f in sent if f.get("type") == "storage_resume_reply"
    ]
    assert len(reply_frames) == 1
    assert sorted(reply_frames[0]["received_chunks"]) == [0, 1]
