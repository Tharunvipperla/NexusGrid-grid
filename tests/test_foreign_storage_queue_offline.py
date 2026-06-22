"""P1 — send-while-offline FS deposit: lifecycle retry pass."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from nexus.core import STATE
from nexus.runtime import foreign_storage_keys
from nexus.scheduler.dag import _foreign_storage_queue_retry_pass
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session
from nexus.telemetry import presence


DEP_ID = "dep-queued-1"
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
def _reset_in_memory():
    foreign_storage_keys.reset_for_testing()
    STATE.peer_presence.clear()
    yield
    foreign_storage_keys.reset_for_testing()
    STATE.peer_presence.clear()


def _seed_queued_deposit(created_at: str | None = None) -> None:
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()

    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=HOST,
                    status="queued_offline",
                    total_bytes=1024,
                    chunk_count=1,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at=created_at,
                    depositor_signature="sig-x",
                )
            )
            await db.commit()

    asyncio.run(_go())


def _row_status() -> str:
    async def _go():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()
            return row.status

    return asyncio.run(_go())


def test_target_reachable_retry_flips_to_offered(isolated_db, monkeypatch):
    _seed_queued_deposit()
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")
    # Target presence: online (no entry == online).

    sent_payloads: list[dict] = []

    async def fake_send(target, frame):
        sent_payloads.append(frame)
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_queue_retry_pass())

    assert _row_status() == "offered"
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["deposit_id"] == DEP_ID


def test_target_still_offline_leaves_row_queued(isolated_db, monkeypatch):
    _seed_queued_deposit()
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")
    presence.mark_offline(HOST, "test")

    async def fake_send(target, frame):  # pragma: no cover - shouldn't be called
        raise AssertionError("must not attempt send while offline")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_queue_retry_pass())

    assert _row_status() == "queued_offline"


def test_key_lost_across_restart_purges_row(isolated_db):
    _seed_queued_deposit()
    # Note: no foreign_storage_keys.store — simulates a process restart that
    # nuked the in-RAM key cache while the row survived.

    asyncio.run(_foreign_storage_queue_retry_pass())

    assert _row_status() == "withdrawn"


def test_ttl_expired_marks_withdrawn(isolated_db):
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _seed_queued_deposit(created_at=stale)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    asyncio.run(_foreign_storage_queue_retry_pass())

    assert _row_status() == "withdrawn"
    # Key should be dropped too.
    assert foreign_storage_keys.get(DEP_ID) is None


def test_retry_send_failure_leaves_queued(isolated_db, monkeypatch):
    _seed_queued_deposit()
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")

    async def fake_send(target, frame):
        return False  # send attempt fails (e.g. transient network blip)

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_queue_retry_pass())

    assert _row_status() == "queued_offline"
