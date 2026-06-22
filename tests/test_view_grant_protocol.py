"""Wave 10.7: per-deposit host-view grant protocol tests."""

from __future__ import annotations

import asyncio
import base64
import secrets

import pytest
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import foreign_storage_keys
from nexus.runtime.foreign_storage_workflow import (
    _handle_view_grant,
    _handle_view_grant_accepted,
    _handle_view_grant_rejected,
    _handle_view_revoke,
)
from nexus.security import tokens
from nexus.security.cred_crypto import (
    EVICTION_NONCE_BYTES,
    wrap_view_grant_for_transit,
)
from nexus.storage import database
from nexus.storage.models import AuditEvent


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    LOCAL_SETTINGS.pop("allow_view_grants", None)
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
    LOCAL_SETTINGS.pop("allow_view_grants", None)
    tokens._reset_for_testing()


@pytest.fixture
def captured_frames(monkeypatch):
    out: list[tuple[str, dict]] = []

    async def _fake_send(peer_id, frame):
        out.append((peer_id, frame))
        return True

    monkeypatch.setattr(
        "nexus.networking.tunnel._send_to_peer", _fake_send
    )
    return out


def _seed_host_row(deposit_id: str, depositor_uuid: str, signing_key: str):
    from nexus.storage import ForeignStorageDeposit, Peer, get_session

    async def _go():
        async with get_session() as db:
            db.add(Peer(ip=depositor_uuid, status="trusted", signing_key=signing_key))
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="host",
                    depositor_uuid=depositor_uuid,
                    host_uuid="self",
                    status="stored",
                    total_bytes=10,
                    chunk_count=1,
                    transport="stream",
                )
            )
            await db.commit()

    asyncio.run(_go())


def _build_grant_frame(deposit_id: str, signing_key: str, deposit_key: bytes,
                       sealed_manifest: bytes = b"") -> tuple[dict, bytes]:
    nonce = secrets.token_bytes(EVICTION_NONCE_BYTES)
    wrapped = wrap_view_grant_for_transit(signing_key, nonce, deposit_key)
    frame = {
        "type": "storage_view_grant",
        "deposit_id": deposit_id,
        "grant_nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "deposit_key_blob_b64": base64.b64encode(wrapped).decode("ascii"),
        "sealed_manifest_b64": (
            base64.b64encode(sealed_manifest).decode("ascii")
            if sealed_manifest else ""
        ),
    }
    return frame, nonce


# ---------------------------------------------------------------------------
# _handle_view_grant
# ---------------------------------------------------------------------------

def test_grant_accepted_regardless_of_setting(isolated_db, captured_frames):
    """The host always honors view grants now — there is no opt-in
    toggle. Pre-existing ``allow_view_grants = False`` setting must
    NOT cause a rejection.
    """
    DEP = "192.168.1.5:9000"
    DEPOSIT = "dep-grant-1"
    SK = "peer-key-1"
    deposit_key = secrets.token_bytes(32)
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = False  # legacy / stale value

    frame, _ = _build_grant_frame(DEPOSIT, SK, deposit_key)
    try:
        asyncio.run(_handle_view_grant(DEP, frame))
        assert all(
            f.get("type") != "storage_view_grant_rejected"
            for _, f in captured_frames
        )
        assert foreign_storage_keys.get(DEPOSIT) == deposit_key
    finally:
        foreign_storage_keys.drop(DEPOSIT)


def test_grant_accepted_when_setting_enabled(isolated_db, captured_frames):
    DEP = "192.168.1.6:9000"
    DEPOSIT = "dep-grant-2"
    SK = "peer-key-2"
    deposit_key = secrets.token_bytes(32)
    sealed = b"\x00\x01sealed-manifest-blob\x00"
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = True

    frame, _ = _build_grant_frame(DEPOSIT, SK, deposit_key, sealed)
    try:
        asyncio.run(_handle_view_grant(DEP, frame))

        # No rejection sent.
        assert all(
            f.get("type") != "storage_view_grant_rejected"
            for _, f in captured_frames
        )
        # Key cached.
        cached = foreign_storage_keys.get(DEPOSIT)
        assert cached == deposit_key
        # Sealed manifest persisted on the host row.
        from nexus.storage import ForeignStorageDeposit, get_session
        from sqlalchemy.orm import undefer

        async def _read_manifest():
            async with get_session() as db:
                row = (
                    await db.execute(
                        select(ForeignStorageDeposit)
                        .options(undefer(ForeignStorageDeposit.encrypted_manifest))
                        .filter(
                            ForeignStorageDeposit.deposit_id == DEPOSIT,
                            ForeignStorageDeposit.role == "host",
                        )
                    )
                ).scalar_one()
                return bytes(row.encrypted_manifest or b""), int(row.host_view_granted_at or 0)

        m, ts = asyncio.run(_read_manifest())
        assert m == sealed
        assert ts > 0
        asyncio.run(_assert_audit("storage.view_grant_accepted", DEPOSIT))
    finally:
        foreign_storage_keys.drop(DEPOSIT)


def test_grant_decrypt_failure_emits_rejection(isolated_db, captured_frames):
    DEP = "192.168.1.7:9000"
    DEPOSIT = "dep-grant-3"
    SK = "peer-key-3"
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = True

    # Wrap with the WRONG signing key — the host will fail to decrypt.
    deposit_key = secrets.token_bytes(32)
    frame, _ = _build_grant_frame(DEPOSIT, "different-key", deposit_key)
    asyncio.run(_handle_view_grant(DEP, frame))

    assert any(
        f.get("type") == "storage_view_grant_rejected"
        and f.get("reason") == "decrypt_failed"
        for _, f in captured_frames
    )
    assert foreign_storage_keys.get(DEPOSIT) is None


# ---------------------------------------------------------------------------
# _handle_view_revoke
# ---------------------------------------------------------------------------

def test_revoke_drops_cached_key_and_clears_timestamp(isolated_db, captured_frames):
    DEP = "192.168.1.8:9000"
    DEPOSIT = "dep-revoke-1"
    SK = "peer-key-4"
    deposit_key = secrets.token_bytes(32)
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = True

    grant_frame, _ = _build_grant_frame(DEPOSIT, SK, deposit_key)
    asyncio.run(_handle_view_grant(DEP, grant_frame))
    assert foreign_storage_keys.get(DEPOSIT) is not None

    asyncio.run(_handle_view_revoke(DEP, {
        "type": "storage_view_revoke",
        "deposit_id": DEPOSIT,
    }))
    assert foreign_storage_keys.get(DEPOSIT) is None

    from nexus.storage import ForeignStorageDeposit, get_session

    async def _ts():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEPOSIT,
                        ForeignStorageDeposit.role == "host",
                    )
                )
            ).scalar_one()
            return int(row.host_view_granted_at or 0)

    assert asyncio.run(_ts()) == 0
    asyncio.run(_assert_audit("storage.view_grant_revoked", DEPOSIT))


def test_revoke_from_non_owner_is_rejected(isolated_db, captured_frames):
    DEP = "192.168.1.9:9000"
    OTHER = "192.168.1.99:9000"
    DEPOSIT = "dep-revoke-2"
    SK = "peer-key-5"
    deposit_key = secrets.token_bytes(32)
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = True

    grant_frame, _ = _build_grant_frame(DEPOSIT, SK, deposit_key)
    asyncio.run(_handle_view_grant(DEP, grant_frame))
    assert foreign_storage_keys.get(DEPOSIT) is not None

    # OTHER (not the depositor) tries to revoke — should be ignored.
    asyncio.run(_handle_view_revoke(OTHER, {
        "type": "storage_view_revoke",
        "deposit_id": DEPOSIT,
    }))
    # Key still cached, timestamp still set.
    assert foreign_storage_keys.get(DEPOSIT) is not None
    asyncio.run(_assert_audit(
        "storage.deposit_view_revoke_unauthorized", DEPOSIT
    ))
    foreign_storage_keys.drop(DEPOSIT)


def test_grant_with_unknown_deposit_is_rejected(isolated_db, captured_frames):
    DEP = "192.168.1.10:9000"
    SK = "peer-key-6"
    LOCAL_SETTINGS["allow_view_grants"] = True

    # Seed only the peer, no deposit row.
    from nexus.storage import Peer, get_session

    async def _seed():
        async with get_session() as db:
            db.add(Peer(ip=DEP, status="trusted", signing_key=SK))
            await db.commit()

    asyncio.run(_seed())

    deposit_key = secrets.token_bytes(32)
    frame, _ = _build_grant_frame("unknown-dep", SK, deposit_key)
    asyncio.run(_handle_view_grant(DEP, frame))

    assert any(
        f.get("type") == "storage_view_grant_rejected"
        and f.get("reason") == "unknown_deposit"
        for _, f in captured_frames
    )


def test_idle_ttl_evicts_host_side_grant(isolated_db, captured_frames):
    """Host's cached key is subject to the Wave 7 idle-TTL GC pass."""
    DEP = "192.168.1.11:9000"
    DEPOSIT = "dep-ttl-1"
    SK = "peer-key-7"
    deposit_key = secrets.token_bytes(32)
    _seed_host_row(DEPOSIT, DEP, SK)
    LOCAL_SETTINGS["allow_view_grants"] = True

    grant_frame, _ = _build_grant_frame(DEPOSIT, SK, deposit_key)
    asyncio.run(_handle_view_grant(DEP, grant_frame))

    # Force the entry's last_used_at far in the past.
    bucket = foreign_storage_keys._bucket()
    entry = bucket[DEPOSIT]
    entry["last_used_at"] = entry["unlocked_at"] - 99999
    entry["unlocked_at"] = entry["last_used_at"]

    import time as _time
    evicted = foreign_storage_keys.gc(_time.monotonic(), idle_ttl_s=1)
    assert DEPOSIT in evicted
    assert foreign_storage_keys.get(DEPOSIT) is None


# ---------------------------------------------------------------------------
# _handle_view_grant_rejected (depositor side)
# ---------------------------------------------------------------------------

def _seed_depositor_row(deposit_id: str, host_uuid: str, *, granted_at: int = 0):
    from nexus.storage import ForeignStorageDeposit, get_session

    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="depositor",
                    depositor_uuid="self",
                    host_uuid=host_uuid,
                    status="stored",
                    total_bytes=10,
                    chunk_count=1,
                    transport="stream",
                    host_view_granted_at=granted_at,
                )
            )
            await db.commit()

    asyncio.run(_go())


def test_grant_rejected_rolls_back_depositor_row(isolated_db, captured_frames):
    """Host bounced our grant frame — depositor's optimistic ``host_view_granted_at``
    must be cleared so the UI stops showing 'Shared' for a deposit the host
    will not honor.
    """
    HOST = "192.168.2.5:9000"
    DEPOSIT = "dep-rejected-1"
    _seed_depositor_row(DEPOSIT, HOST, granted_at=1234567)

    asyncio.run(_handle_view_grant_rejected(HOST, {
        "type": "storage_view_grant_rejected",
        "deposit_id": DEPOSIT,
        "reason": "disabled",
    }))

    from nexus.storage import ForeignStorageDeposit, get_session

    async def _ts():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEPOSIT,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one()
            return int(row.host_view_granted_at or 0)

    assert asyncio.run(_ts()) == 0
    asyncio.run(_assert_audit("storage.view_grant_rejected_by_host", DEPOSIT))


def test_grant_accepted_stamps_real_timestamp_from_sentinel(isolated_db, captured_frames):
    """After host echoes the ack frame, depositor's row flips from the
    ``-1`` "Share pending" sentinel to a real timestamp. The UI uses
    this stamp to render the "Shared" badge.
    """
    HOST = "192.168.3.5:9000"
    DEPOSIT = "dep-accepted-1"
    _seed_depositor_row(DEPOSIT, HOST, granted_at=-1)

    asyncio.run(_handle_view_grant_accepted(HOST, {
        "type": "storage_view_grant_accepted",
        "deposit_id": DEPOSIT,
    }))

    from nexus.storage import ForeignStorageDeposit, get_session

    async def _ts():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEPOSIT,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one()
            return int(row.host_view_granted_at or 0)

    assert asyncio.run(_ts()) > 0
    asyncio.run(_assert_audit("storage.view_grant_acked_by_host", DEPOSIT))


def test_grant_accepted_is_noop_when_no_depositor_row(isolated_db, captured_frames):
    """Defensive: ack for an unknown deposit shouldn't raise."""
    asyncio.run(_handle_view_grant_accepted("192.168.3.6:9000", {
        "type": "storage_view_grant_accepted",
        "deposit_id": "ghost-deposit",
    }))


def test_grant_rejected_is_noop_when_no_depositor_row(isolated_db, captured_frames):
    """Defensive: rejection for an unknown deposit shouldn't raise."""
    asyncio.run(_handle_view_grant_rejected("192.168.2.6:9000", {
        "type": "storage_view_grant_rejected",
        "deposit_id": "ghost-deposit",
        "reason": "disabled",
    }))
    # Still emits an audit row keyed off the deposit id we received.
    asyncio.run(_assert_audit("storage.view_grant_rejected_by_host", "ghost-deposit"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _assert_audit(action: str, deposit_id: str) -> None:
    from nexus.storage import get_session

    async with get_session() as db:
        rows = (
            await db.execute(
                select(AuditEvent).filter(
                    AuditEvent.action == action,
                    AuditEvent.task_id == deposit_id,
                )
            )
        ).scalars().all()
    assert rows, f"expected at least one {action!r} audit row for {deposit_id}"
