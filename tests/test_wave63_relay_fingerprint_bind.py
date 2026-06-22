"""Wave 63 — bind-time relay code-fingerprint validation."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.runtime import local_relay
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database


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
    async def _noop_publish(*a, **k):
        return {"via": "test-stub"}
    monkeypatch.setattr("nexus.runtime.group_inbox.publish_relay_update", _noop_publish)
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _gid(client) -> str:
    return client.post("/local/groups", json={"name": "g"}).json()["id"]


# --- fingerprint_for_url (local resolution) ---------------------------------


def test_fingerprint_for_url_matches_running_relays(monkeypatch):
    monkeypatch.setattr(local_relay, "_port", 9000)
    monkeypatch.setattr(local_relay, "_module", "default")
    monkeypatch.setattr(local_relay, "is_running", lambda: True)
    monkeypatch.setattr(local_relay, "_active_fingerprint", lambda: "PRIMARYFP")

    class _Alive:
        def is_alive(self): return True
    monkeypatch.setitem(local_relay._instances, 9100,
                        {"server": None, "thread": _Alive(), "module": "echo"})
    monkeypatch.setattr(local_relay, "_fingerprint_for_module",
                        lambda n: "ECHOFP" if n == "echo" else "")

    assert local_relay.fingerprint_for_url("ws://1.2.3.4:9000") == "PRIMARYFP"
    assert local_relay.fingerprint_for_url("ws://1.2.3.4:9100") == "ECHOFP"
    assert local_relay.fingerprint_for_url("ws://1.2.3.4:9999") == ""   # no match
    assert local_relay.fingerprint_for_url("wss://x.trycloudflare.com") == ""  # no port
    local_relay._instances.pop(9100, None)


# --- bind-time validation ----------------------------------------------------


def test_founder_first_bind_freezes_to_bound_relay_fp(client, monkeypatch):
    # The founder is running a CUSTOM relay; first bind should freeze the group
    # to that relay's fingerprint (not the bundled one).
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "CUSTOMFP")
    gid = _gid(client)
    res = client.post(f"/local/groups/{gid}/relays", json={"relay_url": "ws://h:9100"})
    assert res.status_code == 200, res.text
    detail = client.get(f"/local/groups/{gid}").json()
    assert detail["relay_code_fingerprint"] == "CUSTOMFP"


def test_bind_rejected_on_fingerprint_mismatch(client, monkeypatch):
    # Freeze the group to one fingerprint via the founder's first bind...
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "GROUPFP")
    gid = _gid(client)
    assert client.post(f"/local/groups/{gid}/relays",
                       json={"relay_url": "ws://h:9000"}).status_code == 200

    # ...now a relay running DIFFERENT code is refused.
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "OTHERFP")
    res = client.post(f"/local/groups/{gid}/relays", json={"relay_url": "ws://h:9100"})
    assert res.status_code == 409
    assert "fingerprint mismatch" in res.json()["detail"]

    # ...a relay matching the frozen code binds fine.
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "GROUPFP")
    res = client.post(f"/local/groups/{gid}/relays", json={"relay_url": "ws://h:9200"})
    assert res.status_code == 200, res.text


def test_unresolvable_relay_skips_validation(client, monkeypatch):
    # A remote/tunnel URL we can't attest locally ("") must NOT be rejected.
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "GROUPFP")
    gid = _gid(client)
    assert client.post(f"/local/groups/{gid}/relays",
                       json={"relay_url": "ws://h:9000"}).status_code == 200
    monkeypatch.setattr("nexus.runtime.local_relay.fingerprint_for_url",
                        lambda url: "")  # can't resolve
    res = client.post(f"/local/groups/{gid}/relays",
                      json={"relay_url": "wss://x.trycloudflare.com"})
    assert res.status_code == 200, res.text
