"""Wave 47 — member presence beacons + last-seen tracking."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import group_inbox, group_presence
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import GroupMember
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


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _opened(channel: str, sender: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        channel=channel, sender_pubkey=sender,
        payload=json.dumps(payload).encode("utf-8"),
    )


def _seed_member(gid: str, pubkey: str, last_seen: str = ""):
    async def _go():
        async with get_session() as s:
            s.add(GroupMember(group_id=gid, pubkey=pubkey, last_seen_at=last_seen))
            await s.commit()
    asyncio.run(_go())


def _last_seen(gid: str, pubkey: str) -> str:
    async def _go():
        async with get_session() as s:
            row = await s.get(GroupMember, (gid, pubkey))
            return row.last_seen_at if row else None
    return asyncio.run(_go())


def test_beacon_sets_last_seen(isolated_db):
    gid, sender = "g1", "peer-1"
    _seed_member(gid, sender)
    ts = iso_now()
    op = _opened(gid, sender, {"group_id": gid, "ts": ts})
    assert asyncio.run(group_inbox.apply_presence_beacon(op)) is True
    assert _last_seen(gid, sender) == ts


def test_beacon_unknown_member_is_noop(isolated_db):
    op = _opened("g1", "ghost", {"group_id": "g1", "ts": iso_now()})
    assert asyncio.run(group_inbox.apply_presence_beacon(op)) is False


def test_beacon_only_moves_forward(isolated_db):
    gid, sender = "g1", "peer-1"
    newer = "2026-05-31T12:00:00+00:00"
    older = "2026-05-31T11:00:00+00:00"
    _seed_member(gid, sender, last_seen=newer)
    op = _opened(gid, sender, {"group_id": gid, "ts": older})
    assert asyncio.run(group_inbox.apply_presence_beacon(op)) is True
    # Stale beacon must not roll last_seen backwards.
    assert _last_seen(gid, sender) == newer


def test_beacon_future_ts_clamped(isolated_db):
    gid, sender = "g1", "peer-1"
    _seed_member(gid, sender)
    op = _opened(gid, sender, {"group_id": gid, "ts": "2999-01-01T00:00:00+00:00"})
    assert asyncio.run(group_inbox.apply_presence_beacon(op)) is True
    assert _last_seen(gid, sender) < "2999-01-01T00:00:00+00:00"


def test_presence_not_archived_in_frame_log(isolated_db):
    # A presence beacon must never be written to the catch-up frame log.
    asyncio.run(group_inbox.capture_frame_to_log(
        group_id="g1", frame_id="f1", envelope={"x": 1},
        frame_type=group_inbox.FRAME_PRESENCE_BEACON,
    ))
    rows = asyncio.run(group_inbox.fetch_log_since("g1", ""))
    assert rows == []


def test_presence_endpoint_lists_members(client):
    gid = client.post("/local/groups", json={"name": "g"}).json()["id"]
    res = client.get(f"/local/groups/{gid}/presence")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["online_window_s"] == group_presence.PRESENCE_ONLINE_WINDOW_S
    assert len(body["members"]) == 1  # founder is the sole member


def test_beacon_my_groups_publishes(isolated_db, monkeypatch):
    from nexus.security.group_keys import get_local_group_pubkey
    me = get_local_group_pubkey()
    _seed_member("ga", me)
    _seed_member("gb", me)
    _seed_member("gc", "someone-else")  # not me — skipped

    sent = []

    async def _stub(session, gid, **k):
        sent.append(gid)
        return {"published": 1}

    monkeypatch.setattr(group_inbox, "publish_presence_beacon", _stub)
    n = asyncio.run(group_presence.beacon_my_groups())
    assert n == 2
    assert set(sent) == {"ga", "gb"}
