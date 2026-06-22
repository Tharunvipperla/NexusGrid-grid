"""Auto-rescue lifecycle pass — depositor-side salvage of at-risk deposits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime import foreign_storage_keys
from nexus.scheduler.dag import _foreign_storage_auto_rescue_pass
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session
from nexus.telemetry import presence


DEP_ID = "dep-rescue-1"
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
def _reset(tmp_path, monkeypatch):
    foreign_storage_keys.reset_for_testing()
    STATE.peer_presence.clear()
    STATE.foreign_storage_auto_rescue_seen.clear()
    # Sensible defaults for every test; individual tests override.
    LOCAL_SETTINGS["fs_auto_rescue"] = True
    LOCAL_SETTINGS["fs_auto_rescue_mode"] = "folder_then_cloud"
    LOCAL_SETTINGS["fs_auto_rescue_trigger"] = "eviction"
    LOCAL_SETTINGS["fs_auto_rescue_days"] = 2
    LOCAL_SETTINGS["fs_auto_rescue_cloud_cred"] = ""
    LOCAL_SETTINGS["fs_auto_rescue_dir"] = str(tmp_path / "rescued")
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = {}
    yield
    foreign_storage_keys.reset_for_testing()
    STATE.peer_presence.clear()
    STATE.foreign_storage_auto_rescue_seen.clear()


def _seed(status: str = "eviction_requested", ttl_at: str | None = None) -> None:
    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=HOST,
                    status=status,
                    total_bytes=1024,
                    chunk_count=3,
                    transport="stream",
                    salt=b"\x00" * 16,
                    password_hint="",
                    ttl_days=30,
                    ttl_at=ttl_at or "",
                    created_at=datetime.now(timezone.utc).isoformat(),
                    depositor_signature="sig-x",
                    filename="report.pdf",
                )
            )
            await db.commit()

    asyncio.run(_go())


def test_local_download_started_when_unlocked(isolated_db, monkeypatch):
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame)
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1
    assert sent[0]["type"] == "storage_retrieve_open"
    assert sent[0]["deposit_id"] == DEP_ID
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"
    entry = foreign_storage_keys.get_entry(DEP_ID)
    assert entry["save_to"].endswith("report.pdf")


def test_locked_deposit_downloads_encrypted(isolated_db, monkeypatch):
    # Locked deposit (no cached key) → pull the ciphertext to disk for
    # decrypt-later, instead of giving up.
    _seed("eviction_requested")

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame)
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1
    assert sent[0]["type"] == "storage_retrieve_open"
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"
    # A raw (keyless) entry carrying the ciphertext landing dir was staged.
    entry = foreign_storage_keys.get_entry(DEP_ID)
    assert entry and entry.get("raw_dir")
    assert foreign_storage_keys.get(DEP_ID) is None  # no key — it's locked


def test_offline_host_leaves_at_risk(isolated_db, monkeypatch):
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)
    presence.mark_offline(HOST, "test")

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("must not act while host offline")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert DEP_ID not in STATE.foreign_storage_auto_rescue_seen


def test_cloud_path_calls_evict_to_cloud(isolated_db, monkeypatch):
    # cloud_only mode + a cloud credential → host-side cloud eviction.
    _seed("eviction_requested")
    LOCAL_SETTINGS["fs_auto_rescue_mode"] = "cloud_only"
    LOCAL_SETTINGS["fs_auto_rescue_cloud_cred"] = "cred-123"

    calls: list[tuple] = []

    async def fake_cloud(deposit_id, cred_id, cloud_dest=""):
        calls.append((deposit_id, cred_id))

    monkeypatch.setattr(
        "nexus.runtime.foreign_storage_cloud.request_cloud_eviction", fake_cloud
    )

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert calls == [(DEP_ID, "cred-123")]
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"


def test_cloud_only_without_cloud_warns(isolated_db, monkeypatch):
    # cloud_only but nothing configured → no folder download; warn instead.
    _seed("eviction_requested")
    LOCAL_SETTINGS["fs_auto_rescue_mode"] = "cloud_only"
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("cloud_only must not fall back to a folder download")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "no_cloud"


def test_cloud_then_folder_falls_back_when_no_cloud(isolated_db, monkeypatch):
    # cloud_then_folder with no cloud configured → straight to folder download.
    _seed("eviction_requested")
    LOCAL_SETTINGS["fs_auto_rescue_mode"] = "cloud_then_folder"
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame); return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1 and sent[0]["type"] == "storage_retrieve_open"
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"


def test_disabled_is_noop(isolated_db, monkeypatch):
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)
    LOCAL_SETTINGS["fs_auto_rescue"] = False

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("auto-rescue is off")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert DEP_ID not in STATE.foreign_storage_auto_rescue_seen


def test_stored_skipped_under_eviction_trigger(isolated_db, monkeypatch):
    # trigger=eviction (default): a healthy stored deposit near TTL is NOT rescued.
    near = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _seed("stored", ttl_at=near)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("stored deposit must not be rescued under eviction trigger")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert DEP_ID not in STATE.foreign_storage_auto_rescue_seen


def test_encrypted_rescue_then_decrypt_roundtrip(isolated_db, tmp_path):
    # Full path: receive ciphertext chunks (locked) -> rescued_encrypted ->
    # decrypt with the password -> original bytes back.
    from fastapi import HTTPException

    from nexus.api.local import foreign_storage_decrypt_rescued
    from nexus.networking.storage_pump import rescued_deposit_dir
    from nexus.runtime.foreign_storage_workflow import _save_raw_chunk
    from nexus.security.deposit_crypto import (
        derive_key,
        encrypt_chunk,
        seal_manifest,
    )

    password = "RescueMe123!"
    salt = b"\x11" * 16
    key = derive_key(password, salt)
    plaintext_chunks = [b"alpha-block-0", b"beta-block-1", b"gamma-2!"]
    plaintext = b"".join(plaintext_chunks)
    sealed = seal_manifest(key, {"filename": "secret.txt", "size": len(plaintext)})

    async def _seed_row():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID,
                    role="depositor",
                    depositor_uuid=DEPOSITOR,
                    host_uuid=HOST,
                    status="eviction_requested",
                    total_bytes=len(plaintext),
                    chunk_count=len(plaintext_chunks),
                    transport="stream",
                    salt=salt,
                    encrypted_manifest=sealed,
                    ttl_days=30,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    depositor_signature="sig-x",
                    filename="secret.txt",
                )
            )
            await db.commit()

    asyncio.run(_seed_row())

    raw_dir = str(rescued_deposit_dir(DEP_ID))
    entry: dict = {}

    async def _receive():
        for idx, pt in enumerate(plaintext_chunks):
            blob = encrypt_chunk(key, pt, idx)
            await _save_raw_chunk(DEP_ID, HOST, idx, blob, raw_dir, entry)

    asyncio.run(_receive())

    # Final chunk flips the row to rescued_encrypted.
    async def _status():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == DEP_ID
                    )
                )
            ).scalar_one()
            return row.status

    assert asyncio.run(_status()) == "rescued_encrypted"

    # Wrong password is rejected.
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            foreign_storage_decrypt_rescued(DEP_ID, {"password": "WrongPass1!"})
        )
    assert ei.value.status_code == 401

    # Correct password decrypts to the original bytes.
    out = tmp_path / "out.txt"
    res = asyncio.run(
        foreign_storage_decrypt_rescued(
            DEP_ID, {"password": password, "save_to_path": str(out)}
        )
    )
    assert res["status"] == "ok"
    assert out.read_bytes() == plaintext
    assert asyncio.run(_status()) == "completed"


def test_per_deposit_override_disables_rescue(isolated_db, monkeypatch):
    # Global on, but this deposit is overridden off → no action.
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = {DEP_ID: {"enabled": False}}

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("overridden-off deposit must not be rescued")

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert DEP_ID not in STATE.foreign_storage_auto_rescue_seen


def test_per_deposit_override_enables_when_global_off(isolated_db, monkeypatch):
    # Global off, but this deposit is overridden on → it IS rescued.
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)
    LOCAL_SETTINGS["fs_auto_rescue"] = False
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = {DEP_ID: {"enabled": True}}

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame)
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"


def test_per_deposit_mode_override_uses_cloud(isolated_db, monkeypatch):
    # Global mode is folder_then_cloud, but this deposit overrides to cloud_only
    # with a credential → cloud eviction, no folder download.
    _seed("eviction_requested")
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)  # unlocked, but mode says cloud
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = {
        DEP_ID: {"mode": "cloud_only", "cloud_cred": "cred-77"}
    }

    calls: list[tuple] = []

    async def fake_cloud(deposit_id, cred_id, cloud_dest=""):
        calls.append((deposit_id, cred_id))

    async def fake_send(target, frame):  # pragma: no cover
        raise AssertionError("cloud_only override must not download to a folder")

    monkeypatch.setattr(
        "nexus.runtime.foreign_storage_cloud.request_cloud_eviction", fake_cloud
    )
    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert calls == [(DEP_ID, "cred-77")]


def test_per_deposit_trigger_override_days(isolated_db, monkeypatch):
    # Global trigger is "eviction", but this stored deposit overrides to "days"
    # with a 5-day window and a TTL 1 day out → it IS rescued pre-emptively.
    near = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _seed("stored", ttl_at=near)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)
    LOCAL_SETTINGS["fs_auto_rescue_trigger"] = "eviction"   # global default
    LOCAL_SETTINGS["fs_auto_rescue_overrides"] = {
        DEP_ID: {"trigger": "days", "days": 5}
    }

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame); return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1 and sent[0]["type"] == "storage_retrieve_open"
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"


def test_stored_rescued_under_days_trigger(isolated_db, monkeypatch):
    LOCAL_SETTINGS["fs_auto_rescue_trigger"] = "days"
    LOCAL_SETTINGS["fs_auto_rescue_days"] = 2
    near = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _seed("stored", ttl_at=near)
    foreign_storage_keys.store(DEP_ID, b"\x01" * 32)

    sent: list[dict] = []

    async def fake_send(target, frame):
        sent.append(frame)
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    asyncio.run(_foreign_storage_auto_rescue_pass())

    assert len(sent) == 1
    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "started"
