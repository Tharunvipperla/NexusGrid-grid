"""P2 — Auto-mode FS deposit fan-out: top-3 selection, first-accept-wins,
loser-cancel broadcast, timeout fallback (no auto-retry)."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime import foreign_storage_keys
from nexus.runtime.foreign_storage_workflow import (
    _handle_offer_cancelled,
    _handle_offer_response,
)
from nexus.scheduler.dag import _foreign_storage_auto_offer_timeout_pass
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session


DEP_ID = "dep-auto-1"
DEPOSITOR = "10.0.0.1:9000"
CAND_A = "10.0.0.10:9000"  # winner candidate
CAND_B = "10.0.0.11:9000"  # loser candidate 1
CAND_C = "10.0.0.12:9000"  # loser candidate 2


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
    STATE.foreign_storage_auto_candidates.clear()
    STATE.foreign_storage_auto_started_at.clear()
    yield
    foreign_storage_keys.reset_for_testing()
    STATE.foreign_storage_auto_candidates.clear()
    STATE.foreign_storage_auto_started_at.clear()


def _seed_offering_multi(candidates: list[str]) -> None:
    """Insert a depositor row in offering_multi state with the given pool."""
    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid="",
                    status="offering_multi",
                    total_bytes=1024,
                    chunk_count=1,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at="",
                    depositor_signature="sig-x",
                )
            )
            await db.commit()

    asyncio.run(_go())
    STATE.foreign_storage_auto_candidates[DEP_ID] = list(candidates)
    STATE.foreign_storage_auto_started_at[DEP_ID] = time.time()
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32, file_path="/tmp/x")


def _row():
    async def _go():
        async with get_session() as db:
            return (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()

    return asyncio.run(_go())


def test_first_accept_wins_others_get_cancel(isolated_db, monkeypatch):
    _seed_offering_multi([CAND_A, CAND_B, CAND_C])
    cancels: list[tuple[str, dict]] = []

    async def fake_send(target, frame):
        cancels.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_handle_offer_response(
        CAND_A,
        {
            "type": "storage_offer_response",
            "deposit_id": DEP_ID,
            "accepted": True,
            "host_signature": "sig-host",
        },
    ))

    row = _row()
    assert row.status == "transferring"
    assert row.host_uuid == CAND_A
    assert row.host_signature == "sig-host"
    # Both losers got a cancel; the winner did not.
    cancelled_to = {t for t, _ in cancels if _.get("type") == "storage_offer_cancelled"}
    assert cancelled_to == {CAND_B, CAND_C}
    # STATE bookkeeping is dropped on the win.
    assert DEP_ID not in STATE.foreign_storage_auto_candidates
    assert DEP_ID not in STATE.foreign_storage_auto_started_at


def test_decline_shrinks_pool_but_does_not_withdraw(isolated_db, monkeypatch):
    _seed_offering_multi([CAND_A, CAND_B, CAND_C])

    async def fake_send(target, frame):  # pragma: no cover - shouldn't be called
        raise AssertionError("decline must not send anything")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_handle_offer_response(
        CAND_B,
        {"type": "storage_offer_response", "deposit_id": DEP_ID, "accepted": False},
    ))

    assert _row().status == "offering_multi"
    assert STATE.foreign_storage_auto_candidates[DEP_ID] == [CAND_A, CAND_C]


def test_all_decline_flips_to_withdrawn(isolated_db, monkeypatch):
    _seed_offering_multi([CAND_A])

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("decline path must not send")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_handle_offer_response(
        CAND_A,
        {"type": "storage_offer_response", "deposit_id": DEP_ID, "accepted": False},
    ))

    assert _row().status == "withdrawn"
    assert DEP_ID not in STATE.foreign_storage_auto_candidates
    assert DEP_ID not in STATE.foreign_storage_auto_started_at


def test_late_accept_after_win_gets_cancel(isolated_db, monkeypatch):
    # Row is already transferring; a late accept from a loser should
    # receive its own cancel so it can drop the offer.
    async def _seed():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=CAND_A,
                    status="transferring",
                    total_bytes=1024,
                    chunk_count=1,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at="",
                    depositor_signature="sig-x",
                )
            )
            await db.commit()
    asyncio.run(_seed())

    cancels: list[tuple[str, dict]] = []

    async def fake_send(target, frame):
        cancels.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_handle_offer_response(
        CAND_B,
        {"type": "storage_offer_response", "deposit_id": DEP_ID, "accepted": True},
    ))

    assert len(cancels) == 1
    assert cancels[0][0] == CAND_B
    assert cancels[0][1]["type"] == "storage_offer_cancelled"
    # Row not regressed.
    assert _row().status == "transferring"
    assert _row().host_uuid == CAND_A


def test_timeout_pass_withdraws_and_cancels(isolated_db, monkeypatch):
    _seed_offering_multi([CAND_A, CAND_B, CAND_C])
    # Pretend the offer was sent long enough ago to time out.
    STATE.foreign_storage_auto_started_at[DEP_ID] = time.time() - 9999
    LOCAL_SETTINGS["fs_auto_offer_timeout_sec"] = 60

    cancels: list[tuple[str, dict]] = []

    async def fake_send(target, frame):
        cancels.append((target, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    try:
        asyncio.run(_foreign_storage_auto_offer_timeout_pass())
    finally:
        LOCAL_SETTINGS["fs_auto_offer_timeout_sec"] = 300

    assert _row().status == "withdrawn"
    cancelled_to = {t for t, f in cancels if f.get("type") == "storage_offer_cancelled"}
    assert cancelled_to == {CAND_A, CAND_B, CAND_C}
    # All cancel reasons should be "timeout".
    for _, frame in cancels:
        assert frame.get("reason") == "timeout"
    # STATE + cached key dropped — caller must redo.
    assert DEP_ID not in STATE.foreign_storage_auto_candidates
    assert DEP_ID not in STATE.foreign_storage_auto_started_at
    assert foreign_storage_keys.get(DEP_ID) is None


def test_timeout_pass_skips_offers_still_within_window(isolated_db, monkeypatch):
    _seed_offering_multi([CAND_A])
    # Started "now" — well within the default 300s timeout.
    LOCAL_SETTINGS["fs_auto_offer_timeout_sec"] = 300

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("must not cancel a fresh offer")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_offer_timeout_pass())

    assert _row().status == "offering_multi"
    assert STATE.foreign_storage_auto_candidates.get(DEP_ID) == [CAND_A]


def test_offer_cancelled_handler_drops_offered_host_row(isolated_db):
    """Candidate-side: a depositor-authored cancel withdraws the host row."""
    async def _seed():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="host",
                    depositor_uuid=DEPOSITOR,
                    host_uuid="10.0.0.2:9000",
                    status="offered",
                    total_bytes=1024,
                    chunk_count=1,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at="",
                    depositor_signature="sig-x",
                )
            )
            await db.commit()
    asyncio.run(_seed())

    asyncio.run(_handle_offer_cancelled(
        DEPOSITOR,
        {"type": "storage_offer_cancelled", "deposit_id": DEP_ID, "reason": "timeout"},
    ))

    async def _check():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()
            return row.status

    assert asyncio.run(_check()) == "withdrawn"


def test_offer_cancelled_handler_rejects_unauthorized_sender(isolated_db):
    """Candidate-side: only the deposit's depositor may cancel."""
    async def _seed():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="host",
                    depositor_uuid=DEPOSITOR,
                    host_uuid="10.0.0.2:9000",
                    status="offered",
                    total_bytes=1024,
                    chunk_count=1,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    created_at="",
                    depositor_signature="sig-x",
                )
            )
            await db.commit()
    asyncio.run(_seed())

    asyncio.run(_handle_offer_cancelled(
        "some-other-peer:9000",
        {"type": "storage_offer_cancelled", "deposit_id": DEP_ID, "reason": "x"},
    ))

    async def _check():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()
            return row.status

    assert asyncio.run(_check()) == "offered"
