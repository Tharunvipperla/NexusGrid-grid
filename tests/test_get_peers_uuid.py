"""Auto-discovery UI flap fix: /local/peers must expose peer_uuid on both
``discovered_lan`` and ``peers`` so the UI can match by identity instead
of by IP. Without this, the discovery row's Connect/Connected button
flaps when the beacon source rotates between LAN (IP form) and relay
(UUID form).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.core import identity
from nexus.core.state import STATE
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database
from nexus.storage.models import Peer
from sqlalchemy import insert


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


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _seed_discovered_peer(uuid: str, real_ip: str = "") -> None:
    STATE.discovered_peers[uuid] = (
        time.time(),  # ts
        "Friendly Name",  # display_name
        "lan",  # source
        {"cpu_cores": 4, "ram_free_mb": 8000, "ram_total_mb": 16000, "cpu_pct": 10},
        False,  # hide
        real_ip,
    )


async def _seed_peer_row(ip: str, status: str = "trusted") -> None:
    async with database.get_session() as db:
        await db.execute(
            insert(Peer).values(
                ip=ip,
                status=status,
                role="single",
                display_name="Friendly Name",
            )
        )
        await db.commit()


def test_discovered_lan_row_exposes_peer_uuid(client):
    STATE.discovered_peers.clear()
    _seed_discovered_peer("uuid-aaa", real_ip="10.0.0.5:8000")
    try:
        res = client.get("/local/peers")
        assert res.status_code == 200
        body = res.json()
        assert body["discovered_lan"], "expected one discovered entry"
        row = body["discovered_lan"][0]
        assert row["peer_uuid"] == "uuid-aaa"
    finally:
        STATE.discovered_peers.clear()


def test_peers_row_exposes_peer_uuid_via_resolver(client):
    """``peers[*].peer_uuid`` should resolve to the UUID even when
    Peer.ip is stored as a LAN IP, because the identity resolver maps
    IPs↔UUIDs once a peer has been seen."""
    identity._IP_TO_UUID["10.0.0.5:8000"] = "uuid-aaa"
    identity._UUID_TO_IP["uuid-aaa"] = "10.0.0.5:8000"
    try:
        asyncio.run(_seed_peer_row("10.0.0.5:8000"))
        res = client.get("/local/peers")
        assert res.status_code == 200
        body = res.json()
        assert body["peers"], "expected one peer row"
        assert body["peers"][0]["peer_uuid"] == "uuid-aaa"
    finally:
        identity.clear_mappings()


def test_peers_row_peer_uuid_falls_back_to_ip_when_unresolved(client):
    """When the resolver has no mapping, ``peer_uuid`` falls back to
    ``p.ip`` (the resolver returns the input unchanged). The UI's IP
    fallback path still works in that case."""
    identity.clear_mappings()
    asyncio.run(_seed_peer_row("10.0.0.99:8000"))
    res = client.get("/local/peers")
    assert res.status_code == 200
    body = res.json()
    assert body["peers"], "expected one peer row"
    # No mapping → resolver echoes input.
    assert body["peers"][0]["peer_uuid"] == "10.0.0.99:8000"
