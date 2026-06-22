"""Wave 41 — state-change timeline endpoint."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import relay_state
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import GroupRelayBinding
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


def _create_group(client) -> str:
    return client.post("/local/groups", json={"name": "g"}).json()["id"]


def _drive(group_id: str, relay_url: str, states: list[str]) -> None:
    """Walk a binding through the given states + commit each transition.

    Awaits the deferred audit-write tasks before returning so the
    timeline endpoint can read what we wrote.
    """
    async def _go():
        async with get_session() as s:
            row = await s.get(GroupRelayBinding, (group_id, relay_url))
            if row is None:
                row = GroupRelayBinding(
                    group_id=group_id,
                    relay_url=relay_url,
                    operator_pubkey="op",
                    registered_at=iso_now(),
                    last_seen_at="",
                    status="active",
                    state=relay_state.STATE_ONLINE,
                )
                s.add(row)
                await s.flush()
            for new in states:
                await relay_state.transition(row, new, reason=f"test {new}")
            await s.commit()
        # Drain the deferred audit-write tasks spawned by transition().
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(_go())


def test_timeline_returns_recent_transitions(client):
    gid = _create_group(client)
    url = "wss://relay.example"
    _drive(gid, url, [
        relay_state.STATE_OFFLINE,
        relay_state.STATE_RECONNECTING,
        relay_state.STATE_SYNCING,
        relay_state.STATE_ONLINE,
    ])
    res = client.get(
        f"/local/groups/{gid}/relays/timeline",
        params={"relay_url": url},
    )
    assert res.status_code == 200, res.text
    events = res.json()["events"]
    # 4 transitions, newest-first.
    assert len(events) == 4
    transitions = [e["transition"] for e in events]
    assert transitions[0] == "syncing->online"
    assert transitions[-1] == "online->offline"
    assert all(e["reason"].startswith("test ") for e in events)


def test_timeline_filters_by_relay(client):
    gid = _create_group(client)
    url_a = "wss://a.example"
    url_b = "wss://b.example"
    _drive(gid, url_a, [relay_state.STATE_OFFLINE])
    _drive(gid, url_b, [relay_state.STATE_OFFLINE])

    res = client.get(
        f"/local/groups/{gid}/relays/timeline",
        params={"relay_url": url_a},
    )
    events = res.json()["events"]
    assert len(events) == 1


def test_timeline_limit_caps_events(client):
    gid = _create_group(client)
    url = "wss://relay.example"
    chain = [
        relay_state.STATE_OFFLINE,
        relay_state.STATE_RECONNECTING,
        relay_state.STATE_SYNCING,
        relay_state.STATE_ONLINE,
        relay_state.STATE_OFFLINE,
        relay_state.STATE_RECONNECTING,
    ]
    _drive(gid, url, chain)

    res = client.get(
        f"/local/groups/{gid}/relays/timeline",
        params={"relay_url": url, "limit": 2},
    )
    events = res.json()["events"]
    assert len(events) == 2


def test_timeline_empty_for_unknown_binding(client):
    gid = _create_group(client)
    res = client.get(
        f"/local/groups/{gid}/relays/timeline",
        params={"relay_url": "wss://nope"},
    )
    assert res.status_code == 200
    assert res.json()["events"] == []
