"""Wave 42 — operator-adjustable relay metadata (label/region/priority)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import group_inbox
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
def client(isolated_db, monkeypatch):
    # Don't actually fan out the relay.update frame in these unit tests.
    async def _noop_publish(*a, **k):
        return {"via": "test-stub"}

    monkeypatch.setattr(
        "nexus.runtime.group_inbox.publish_relay_update", _noop_publish
    )
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _create_group(client) -> str:
    return client.post("/local/groups", json={"name": "g"}).json()["id"]


def _bind(client, gid: str, url: str) -> None:
    res = client.post(f"/local/groups/{gid}/relays", json={"relay_url": url})
    assert res.status_code == 200, res.text


def _detail_relay(client, gid: str, url: str) -> dict:
    detail = client.get(f"/local/groups/{gid}").json()
    return next(r for r in detail["relays"] if r["relay_url"] == url)


def test_config_sets_label_region_priority(client):
    gid = _create_group(client)
    url = "wss://relay.example"
    _bind(client, gid, url)

    res = client.post(
        f"/local/groups/{gid}/relays/config",
        json={"relay_url": url, "label": "Home", "region": "us-east", "priority": 5},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["label"] == "Home"
    assert body["region"] == "us-east"
    assert body["priority"] == 5

    row = _detail_relay(client, gid, url)
    assert row["label"] == "Home"
    assert row["region"] == "us-east"
    assert row["priority"] == 5


def test_config_partial_update_leaves_others(client):
    gid = _create_group(client)
    url = "wss://relay.example"
    _bind(client, gid, url)
    client.post(
        f"/local/groups/{gid}/relays/config",
        json={"relay_url": url, "label": "Home", "region": "us-east", "priority": 5},
    )
    # Only change the label; region + priority must persist.
    res = client.post(
        f"/local/groups/{gid}/relays/config",
        json={"relay_url": url, "label": "Garage"},
    )
    assert res.status_code == 200, res.text
    row = _detail_relay(client, gid, url)
    assert row["label"] == "Garage"
    assert row["region"] == "us-east"
    assert row["priority"] == 5


def test_founder_bind_auto_freezes_fingerprint(client):
    from nexus.runtime.relay_codeprint import CURRENT_FINGERPRINT

    gid = _create_group(client)
    # Founder binds a relay -> group fingerprint auto-freezes to this
    # node's relay code.
    _bind(client, gid, "wss://relay.example")
    detail = client.get(f"/local/groups/{gid}").json()
    assert detail["relay_code_fingerprint"] == CURRENT_FINGERPRINT
    assert CURRENT_FINGERPRINT  # sanity: the relay module hashed to something


def test_config_unknown_binding_404(client):
    gid = _create_group(client)
    res = client.post(
        f"/local/groups/{gid}/relays/config",
        json={"relay_url": "wss://nope", "label": "x"},
    )
    assert res.status_code == 404


def test_config_priority_out_of_range_rejected(client):
    gid = _create_group(client)
    url = "wss://relay.example"
    _bind(client, gid, url)
    res = client.post(
        f"/local/groups/{gid}/relays/config",
        json={"relay_url": url, "priority": 9999},
    )
    assert res.status_code == 422


# ---- frame replication (apply side) ------------------------------------


def _seed_binding(gid: str, url: str) -> None:
    async def _go():
        async with get_session() as s:
            s.add(GroupRelayBinding(
                group_id=gid, relay_url=url, operator_pubkey="op",
                registered_at=iso_now(), status="active",
            ))
            await s.commit()
    asyncio.run(_go())


def _get_binding(gid: str, url: str) -> GroupRelayBinding:
    async def _go():
        async with get_session() as s:
            return await s.get(GroupRelayBinding, (gid, url))
    return asyncio.run(_go())


def test_apply_config_frame_updates_metadata(isolated_db):
    gid, url = "g1", "wss://relay.example"
    _seed_binding(gid, url)
    payload = {
        "group_id": gid,
        "action": "config",
        "relay_url": url,
        "operator_pubkey": "op",
        "label": "Remote",
        "region": "eu-west",
        "priority": 9,
    }
    opened = SimpleNamespace(
        channel=gid,
        sender_pubkey="op",
        payload=json.dumps(payload).encode("utf-8"),
    )
    ok = asyncio.run(group_inbox.apply_relay_update(opened))
    assert ok is True
    row = _get_binding(gid, url)
    assert row.label == "Remote"
    assert row.region == "eu-west"
    assert row.priority == 9


def test_apply_config_frame_unknown_binding_is_noop(isolated_db):
    gid, url = "g1", "wss://missing.example"
    payload = {
        "group_id": gid, "action": "config", "relay_url": url, "label": "x",
    }
    opened = SimpleNamespace(
        channel=gid, sender_pubkey="op",
        payload=json.dumps(payload).encode("utf-8"),
    )
    ok = asyncio.run(group_inbox.apply_relay_update(opened))
    assert ok is False


# ---- priority drives send ordering -------------------------------------


def test_group_relay_priorities_map(isolated_db):
    from nexus.networking import relay_client

    gid = "g1"
    _seed_binding(gid, "wss://low")
    _seed_binding(gid, "wss://high")

    async def _set():
        async with get_session() as s:
            high = await s.get(GroupRelayBinding, (gid, "wss://high"))
            high.priority = 10
            await s.commit()
    asyncio.run(_set())

    prios = asyncio.run(relay_client._group_relay_priorities(gid))
    assert prios["wss://high"] == 10
    assert prios["wss://low"] == 0
