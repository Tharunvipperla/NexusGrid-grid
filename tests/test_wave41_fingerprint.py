"""Wave 41 — relay code fingerprint validation."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core import STATE
from nexus.networking.relay_client import relay_fingerprint_ok_for_group
from nexus.security import tokens
from nexus.storage import database, get_session
from nexus.storage.models import Group
from nexus.utils.time import iso_now


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    STATE.relay_code_fingerprints.clear()
    yield url
    STATE.relay_code_fingerprints.clear()


def _seed_group(group_id: str, fingerprint: str) -> None:
    async def _go():
        async with get_session() as s:
            s.add(Group(
                id=group_id, name="x", founder_pubkey="founder",
                created_at=iso_now(), relay_code_fingerprint=fingerprint,
            ))
            await s.commit()
    asyncio.run(_go())


def test_unfrozen_group_accepts_any_fingerprint(isolated_db):
    _seed_group("g-no-freeze", "")
    STATE.relay_code_fingerprints["wss://anyrelay.example"] = (
        "abcdef0123456789abcdef0123456789"
    )
    ok, reason = asyncio.run(
        relay_fingerprint_ok_for_group("wss://anyrelay.example", "g-no-freeze")
    )
    assert ok, reason
    assert reason == ""


def test_frozen_group_accepts_matching_fingerprint(isolated_db):
    fp = "abcdef0123456789abcdef0123456789"
    _seed_group("g-match", fp)
    STATE.relay_code_fingerprints["wss://goodrelay.example"] = fp
    ok, _ = asyncio.run(
        relay_fingerprint_ok_for_group("wss://goodrelay.example", "g-match")
    )
    assert ok


def test_frozen_group_rejects_mismatched_fingerprint(isolated_db):
    _seed_group("g-mismatch", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    STATE.relay_code_fingerprints["wss://badrelay.example"] = (
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    ok, reason = asyncio.run(
        relay_fingerprint_ok_for_group("wss://badrelay.example", "g-mismatch")
    )
    assert not ok
    assert "mismatch" in reason


def test_frozen_group_rejects_silent_relay(isolated_db):
    _seed_group("g-silent", "cccccccccccccccccccccccccccccccc")
    STATE.relay_code_fingerprints.pop("wss://silentrelay.example", None)
    ok, reason = asyncio.run(
        relay_fingerprint_ok_for_group("wss://silentrelay.example", "g-silent")
    )
    assert not ok
    assert "advertise" in reason


def test_missing_group_passes(isolated_db):
    # Group row doesn't exist locally: don't block — binding path will
    # fail downstream for other reasons.
    ok, _ = asyncio.run(
        relay_fingerprint_ok_for_group("wss://anything", "nonexistent-group-id")
    )
    assert ok


def test_empty_args_pass():
    ok, _ = asyncio.run(relay_fingerprint_ok_for_group("", "g1"))
    assert ok
    ok, _ = asyncio.run(relay_fingerprint_ok_for_group("wss://x", ""))
    assert ok
