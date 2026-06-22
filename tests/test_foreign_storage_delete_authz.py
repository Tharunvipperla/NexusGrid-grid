"""Authorization test for `storage_delete_now`.

Wave 5b shipped the delete-from-host flow but the host-side handler did
not check that the trusted peer sending the frame actually owns the
deposit. This test pins the fix: a non-owner's delete frame is dropped
+ audited, the deposit row stays untouched, and the on-disk ciphertext
survives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from nexus.core import STATE
from nexus.runtime.foreign_storage_workflow import _handle_delete_now
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, get_session
from nexus.storage import database


OWNER = "10.0.0.1:9000"
ATTACKER = "10.0.0.99:9000"
DEPOSIT = "dep-authz-1"


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


@pytest.fixture
def state_reset():
    prior = dict(STATE.foreign_storage_pumps)
    STATE.foreign_storage_pumps.clear()
    yield
    STATE.foreign_storage_pumps.clear()
    STATE.foreign_storage_pumps.update(prior)


def _seed_deposit_with_chunks(pump_dir: Path) -> None:
    pump_dir.mkdir(parents=True, exist_ok=True)
    (pump_dir / "chunk_00000000.enc").write_bytes(b"ciphertext-payload")

    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEPOSIT,
                    role="host",
                    depositor_uuid=OWNER,
                    host_uuid="self",
                    status="stored",
                    total_bytes=18,
                    chunk_count=1,
                    transport="stream",
                )
            )
            await db.commit()

    asyncio.run(_go())
    STATE.foreign_storage_pumps[DEPOSIT] = {"dir": str(pump_dir)}


def _row_status() -> str:
    async def _go():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEPOSIT
                    )
                )
            ).scalar_one()
            return row.status

    return asyncio.run(_go())


def test_owner_can_delete_their_own_deposit(isolated_db, state_reset, tmp_path):
    pump_dir = tmp_path / "pump"
    _seed_deposit_with_chunks(pump_dir)

    asyncio.run(_handle_delete_now(OWNER, {
        "type": "storage_delete_now", "deposit_id": DEPOSIT, "signature": "",
    }))

    assert _row_status() == "purged"
    assert not list(pump_dir.glob("chunk_*.enc"))


def test_non_owner_cannot_delete_someone_elses_deposit(
    isolated_db, state_reset, tmp_path
):
    """Trusted peer ≠ owner ⇒ host MUST refuse and leave the deposit alone."""
    pump_dir = tmp_path / "pump"
    _seed_deposit_with_chunks(pump_dir)

    asyncio.run(_handle_delete_now(ATTACKER, {
        "type": "storage_delete_now", "deposit_id": DEPOSIT, "signature": "",
    }))

    # Status unchanged.
    assert _row_status() == "stored"
    # Ciphertext still on disk.
    assert (pump_dir / "chunk_00000000.enc").read_bytes() == b"ciphertext-payload"
    # Pump still registered.
    assert DEPOSIT in STATE.foreign_storage_pumps


def test_non_owner_attempt_is_audited(isolated_db, state_reset, tmp_path):
    from nexus.storage.models import AuditEvent

    pump_dir = tmp_path / "pump"
    _seed_deposit_with_chunks(pump_dir)

    asyncio.run(_handle_delete_now(ATTACKER, {
        "type": "storage_delete_now", "deposit_id": DEPOSIT, "signature": "",
    }))

    async def _fetch():
        async with get_session() as db:
            return (
                (
                    await db.execute(
                        select(AuditEvent).filter(
                            AuditEvent.task_id == DEPOSIT
                        )
                    )
                )
                .scalars()
                .all()
            )

    events = asyncio.run(_fetch())
    actions = {e.action for e in events}
    assert "storage.deposit_delete_unauthorized" in actions
    # The legitimate purge audit must NOT have been written.
    assert "storage.deposit_purged" not in actions
