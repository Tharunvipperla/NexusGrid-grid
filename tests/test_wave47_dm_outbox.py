"""Wave 47 — offline DM outbox: undelivered DMs retry until they land."""

from __future__ import annotations

import asyncio

import pytest

from nexus.api import local as local_api
from nexus.runtime import dm_outbox
from nexus.security import group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import DirectMessage
from nexus.utils.time import iso_now


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "groups.db"
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
    group_keys._reset_for_testing()


def _add_dm(msg_id, peer="peer-1", direction="out", delivered=0, deleted=0):
    async def _go():
        async with get_session() as s:
            s.add(DirectMessage(
                msg_id=msg_id, peer_uuid=peer, direction=direction,
                body="hi", sent_at=iso_now(), received_at=iso_now(),
                delivered=delivered, deleted=deleted,
            ))
            await s.commit()
    asyncio.run(_go())


def _delivered(msg_id):
    async def _go():
        async with get_session() as s:
            r = await s.get(DirectMessage, msg_id)
            return int(r.delivered)
    return asyncio.run(_go())


def test_flush_delivers_pending_and_marks(isolated_db, monkeypatch):
    _add_dm("m1")

    async def _ok(target, msg_id, *a, **k):
        async with get_session() as s:
            r = await s.get(DirectMessage, msg_id)
            r.delivered = 1
            await s.commit()
        return True

    monkeypatch.setattr(local_api, "_deliver_dm", _ok)
    n = asyncio.run(dm_outbox.flush_outbox())
    assert n == 1
    assert _delivered("m1") == 1


def test_flush_skips_inbound_and_already_delivered(isolated_db, monkeypatch):
    _add_dm("out-pending", direction="out", delivered=0)
    _add_dm("out-done", direction="out", delivered=1)
    _add_dm("in-msg", direction="in", delivered=0)

    attempted = []

    async def _record(target, msg_id, *a, **k):
        attempted.append(msg_id)
        return True

    monkeypatch.setattr(local_api, "_deliver_dm", _record)
    asyncio.run(dm_outbox.flush_outbox())
    assert attempted == ["out-pending"]


def test_flush_leaves_undelivered_when_peer_offline(isolated_db, monkeypatch):
    _add_dm("m1")

    async def _fail(target, msg_id, *a, **k):
        return False  # peer still offline

    monkeypatch.setattr(local_api, "_deliver_dm", _fail)
    n = asyncio.run(dm_outbox.flush_outbox())
    assert n == 0
    assert _delivered("m1") == 0  # stays queued for the next pass


def test_flush_skips_deleted(isolated_db, monkeypatch):
    _add_dm("gone", deleted=1)
    attempted = []

    async def _record(target, msg_id, *a, **k):
        attempted.append(msg_id)
        return True

    monkeypatch.setattr(local_api, "_deliver_dm", _record)
    asyncio.run(dm_outbox.flush_outbox())
    assert attempted == []
