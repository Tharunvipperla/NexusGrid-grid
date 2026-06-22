"""Wave 14 — host-side offer rejection on opt-out / insufficient space."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import foreign_storage_quota
from nexus.runtime.foreign_storage_workflow import _handle_offer
from nexus.security import tokens
from nexus.storage import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    LOCAL_SETTINGS.pop("foreign_storage_accept_offers", None)
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
    LOCAL_SETTINGS.pop("foreign_storage_accept_offers", None)
    tokens._reset_for_testing()


@pytest.fixture
def captured_frames(monkeypatch):
    out: list[tuple[str, dict]] = []

    async def _fake_send(peer_id, frame):
        out.append((peer_id, frame))
        return True

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", _fake_send)
    return out


def test_offer_rejected_when_opted_out(isolated_db, captured_frames, monkeypatch):
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = False
    asyncio.run(_handle_offer("peer-1", {
        "deposit_id": "dep-opt-out",
        "total_bytes": 100,
        "chunk_count": 1,
        "depositor_signature": "irrelevant",
    }))
    assert any(
        f.get("type") == "storage_offer_rejected"
        and f.get("deposit_id") == "dep-opt-out"
        and f.get("reason") == "opted_out"
        for _, f in captured_frames
    )


def test_offer_rejected_when_no_space(isolated_db, captured_frames, monkeypatch):
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    monkeypatch.setattr(foreign_storage_quota, "effective_free_gb", lambda: 0.0)
    five_gb = 5 * 1024 * 1024 * 1024
    asyncio.run(_handle_offer("peer-1", {
        "deposit_id": "dep-too-big",
        "total_bytes": five_gb,
        "chunk_count": 1,
        "depositor_signature": "irrelevant",
    }))
    assert any(
        f.get("type") == "storage_offer_rejected"
        and f.get("reason") == "insufficient_space"
        for _, f in captured_frames
    )


def test_offer_passes_capacity_check_when_room_available(
    isolated_db, captured_frames, monkeypatch
):
    """Sanity: with both gates open, we don't short-circuit reject — we fall
    through to the existing T&C signature check (which then fails because the
    test sig is bogus). We just verify no rejection frame is sent."""
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    monkeypatch.setattr(foreign_storage_quota, "effective_free_gb", lambda: 100.0)
    asyncio.run(_handle_offer("peer-1", {
        "deposit_id": "dep-ok",
        "total_bytes": 1024,
        "chunk_count": 1,
        "depositor_signature": "bogus",
    }))
    rejections = [
        f for _, f in captured_frames if f.get("type") == "storage_offer_rejected"
    ]
    assert rejections == []
