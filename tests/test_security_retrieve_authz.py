"""Security F-014 — foreign-storage retrieve must be depositor-only.

The host stores encrypted chunks; only the deposit's own depositor may pull them
back. Without the check, any authenticated peer that learns a deposit_id could
force the host to stream the ciphertext (resource abuse) and confirm the
deposit's size — so the host must drop a non-owner's retrieve frame.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.core import STATE
from nexus.runtime.foreign_storage_workflow import _handle_retrieve_open
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session

OWNER = "10.0.0.1:9000"
ATTACKER = "10.0.0.99:9000"
DEPOSIT = "dep-retr-1"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _td():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""
    asyncio.run(_td())
    tokens._reset_for_testing()


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def _fake(peer_uuid, frame):
        out.append((peer_uuid, frame))

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", _fake)
    return out


def _seed(pump_dir: Path):
    pump_dir.mkdir(parents=True, exist_ok=True)
    (pump_dir / "chunk_00000000.enc").write_bytes(b"ciphertext")

    async def _go():
        async with get_session() as db:
            db.add(ForeignStorageDeposit(
                deposit_id=DEPOSIT, role="host", depositor_uuid=OWNER,
                host_uuid="self", status="stored", total_bytes=10,
                chunk_count=1, transport="stream"))
            await db.commit()
    asyncio.run(_go())
    STATE.foreign_storage_pumps[DEPOSIT] = {"dir": str(pump_dir)}


def _frame():
    return {"type": "storage_retrieve_open", "deposit_id": DEPOSIT,
            "first_chunk_idx": 0, "last_chunk_idx": 0}


def test_owner_can_retrieve_own_chunks(isolated_db, sent, tmp_path):
    _seed(tmp_path / "pump")
    try:
        asyncio.run(_handle_retrieve_open(OWNER, _frame()))
        # The owner gets at least one retrieve_chunk frame back.
        assert any(s[0] == OWNER for s in sent)
    finally:
        STATE.foreign_storage_pumps.pop(DEPOSIT, None)


def test_non_owner_retrieve_is_dropped(isolated_db, sent, tmp_path):
    _seed(tmp_path / "pump")
    try:
        asyncio.run(_handle_retrieve_open(ATTACKER, _frame()))
        # No chunks streamed to the attacker.
        assert sent == []
    finally:
        STATE.foreign_storage_pumps.pop(DEPOSIT, None)
