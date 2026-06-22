"""P8 — pause/resume protocol + transit retry + abandoned-chunk purge."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime import foreign_storage_keys
from nexus.runtime.foreign_storage_workflow import (
    _handle_pause,
    _handle_resume_reply,
    _handle_resume_request,
    _scan_received_chunks,
)
from nexus.scheduler.dag import (
    _foreign_storage_abandoned_chunk_purge_pass,
    _foreign_storage_transit_retry_pass,
)
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session


DEP_ID = "dep-transit-1"
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
    STATE.peer_presence.clear()
    yield
    foreign_storage_keys.reset_for_testing()
    STATE.peer_presence.clear()


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
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at=overrides.get(
                        "created_at",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                    depositor_signature="sig-x",
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
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at=overrides.get(
                        "created_at",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                    depositor_signature="sig-x",
                    last_progress_at=overrides.get("last_progress_at", ""),
                )
            )
            await db.commit()
    asyncio.run(_go())


def _row_status() -> str:
    async def _go():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one().status
    return asyncio.run(_go())


def _row_retry_count() -> int:
    async def _go():
        async with get_session() as db:
            return int((
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one().retry_count or 0)
    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Pause/resume protocol handlers
# ---------------------------------------------------------------------------

def test_handle_pause_flips_row_to_paused_reason(isolated_db):
    _seed_depositor("transferring")
    asyncio.run(_handle_pause(
        HOST,
        {"type": "storage_pause", "deposit_id": DEP_ID, "reason": "host_shutdown"},
    ))
    assert _row_status() == "paused_host_shutdown"


def test_handle_pause_rejects_unauthorized_sender(isolated_db):
    _seed_depositor("transferring")
    asyncio.run(_handle_pause(
        "stranger:9000",
        {"type": "storage_pause", "deposit_id": DEP_ID, "reason": "send_failed"},
    ))
    assert _row_status() == "transferring"


def test_handle_pause_noop_on_terminal_row(isolated_db):
    _seed_depositor("stored")
    asyncio.run(_handle_pause(
        HOST,
        {"type": "storage_pause", "deposit_id": DEP_ID, "reason": "host_shutdown"},
    ))
    assert _row_status() == "stored"


def test_resume_request_unauthorized_sender_drops(isolated_db, monkeypatch):
    _seed_host("transferring")
    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_handle_resume_request(
        "stranger:9000",
        {"type": "storage_resume_request", "deposit_id": DEP_ID},
    ))
    assert sent == []


def test_scan_received_chunks_returns_sorted_idx_list(tmp_path, monkeypatch):
    # Stub deposit_dir to return our tmp_path
    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: tmp_path,
    )
    (tmp_path / "chunk_00000003.enc").write_bytes(b"x")
    (tmp_path / "chunk_00000000.enc").write_bytes(b"y")
    (tmp_path / "chunk_00000007.enc").write_bytes(b"z")
    (tmp_path / "not-a-chunk.txt").write_bytes(b"ignore")
    assert _scan_received_chunks(DEP_ID, DEPOSITOR) == [0, 3, 7]


def test_resume_reply_with_all_chunks_present_flips_to_stored(isolated_db, monkeypatch):
    _seed_depositor("paused_send_failed", chunk_count=3)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    asyncio.run(_handle_resume_reply(
        HOST,
        {
            "type": "storage_resume_reply",
            "deposit_id": DEP_ID,
            "received_chunks": [0, 1, 2],
        },
    ))
    assert _row_status() == "stored"


def test_resume_reply_unauthorized_sender_drops(isolated_db):
    _seed_depositor("paused_send_failed", chunk_count=3)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    asyncio.run(_handle_resume_reply(
        "stranger:9000",
        {"type": "storage_resume_reply", "deposit_id": DEP_ID,
         "received_chunks": [0, 1, 2]},
    ))
    assert _row_status() == "paused_send_failed"


# ---------------------------------------------------------------------------
# Lifecycle retry pass
# ---------------------------------------------------------------------------

def test_retry_pass_skips_when_key_not_cached(isolated_db, monkeypatch):
    # Simulate post-restart: paused row, no key in cache.
    _seed_depositor(
        "paused_send_failed",
        last_progress_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )

    async def fake_send(target, frame):  # pragma: no cover - shouldn't be called
        raise AssertionError("retry must wait for the user to Resume")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_foreign_storage_transit_retry_pass())
    assert _row_status() == "paused_send_failed"
    assert _row_retry_count() == 0


def test_retry_pass_sends_resume_request_when_eligible(isolated_db, monkeypatch):
    _seed_depositor(
        "paused_send_failed",
        last_progress_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_foreign_storage_transit_retry_pass())
    assert any(f.get("type") == "storage_resume_request" for _, f in sent)
    assert _row_retry_count() == 1


def test_retry_pass_caps_at_max_retries_and_fails_transit(isolated_db, monkeypatch):
    LOCAL_SETTINGS["fs_transit_max_retries"] = 3
    _seed_depositor(
        "paused_send_failed",
        last_progress_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        retry_count=3,
    )
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    sent: list = []

    async def fake_send(target, frame):
        sent.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    try:
        asyncio.run(_foreign_storage_transit_retry_pass())
    finally:
        LOCAL_SETTINGS["fs_transit_max_retries"] = 5

    assert _row_status() == "failed_in_transit"
    # delete_now broadcast to wipe host-side partial chunks
    assert any(f.get("type") == "storage_delete_now" for _, f in sent)
    # Cached key dropped — user must redo from scratch.
    assert foreign_storage_keys.get(DEP_ID) is None


def test_retry_pass_respects_backoff_window(isolated_db, monkeypatch):
    # last_progress_at is now → backoff hasn't elapsed.
    _seed_depositor(
        "paused_send_failed",
        last_progress_at=datetime.now(timezone.utc).isoformat(),
    )
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("backoff must block this retry")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    asyncio.run(_foreign_storage_transit_retry_pass())
    assert _row_retry_count() == 0


# ---------------------------------------------------------------------------
# Host-side abandoned-chunk purge
# ---------------------------------------------------------------------------

def test_purge_pass_clears_chunks_past_ttl(isolated_db, monkeypatch, tmp_path):
    LOCAL_SETTINGS["fs_transit_abandoned_chunk_ttl_hours"] = 1
    _seed_host(
        "paused_depositor_down",
        last_progress_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    )
    # Stub deposit_dir → tmp_path with some chunks.
    dep_dir = tmp_path / "fs_dep"
    dep_dir.mkdir()
    (dep_dir / "chunk_00000000.enc").write_bytes(b"a")
    (dep_dir / "chunk_00000001.enc").write_bytes(b"b")
    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: dep_dir,
    )

    try:
        asyncio.run(_foreign_storage_abandoned_chunk_purge_pass())
    finally:
        LOCAL_SETTINGS["fs_transit_abandoned_chunk_ttl_hours"] = 24

    assert _row_status() == "withdrawn"
    remaining = list(dep_dir.glob("chunk_*.enc"))
    assert remaining == []


def test_purge_pass_leaves_fresh_rows_alone(isolated_db, monkeypatch, tmp_path):
    LOCAL_SETTINGS["fs_transit_abandoned_chunk_ttl_hours"] = 1
    _seed_host(
        "paused_depositor_down",
        last_progress_at=datetime.now(timezone.utc).isoformat(),
    )
    dep_dir = tmp_path / "fs_dep"
    dep_dir.mkdir()
    (dep_dir / "chunk_00000000.enc").write_bytes(b"a")
    monkeypatch.setattr(
        "nexus.networking.storage_pump.deposit_dir",
        lambda dep, dep_uuid: dep_dir,
    )

    try:
        asyncio.run(_foreign_storage_abandoned_chunk_purge_pass())
    finally:
        LOCAL_SETTINGS["fs_transit_abandoned_chunk_ttl_hours"] = 24

    assert _row_status() == "paused_depositor_down"
    assert (dep_dir / "chunk_00000000.enc").exists()


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------

def test_persist_acked_progress_only_advances(isolated_db):
    from nexus.networking.storage_pump import _persist_acked_progress

    _seed_depositor("transferring", transferred_chunks=10)
    # Going backwards is a no-op.
    asyncio.run(_persist_acked_progress(DEP_ID, 5))

    async def _go():
        async with get_session() as db:
            return int((
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one().transferred_chunks or 0)

    assert asyncio.run(_go()) == 10

    # Going forward advances + writes a last_progress_at.
    asyncio.run(_persist_acked_progress(DEP_ID, 20))

    async def _go2():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()
            return row.transferred_chunks, row.last_progress_at

    n, ts = asyncio.run(_go2())
    assert n == 21
    assert ts  # non-empty ISO string
