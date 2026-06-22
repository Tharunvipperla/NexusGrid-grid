"""Histories endpoint: terminal-state deposits surfaced separately.

The Foreign Storage UI now has a dedicated Histories panel so terminal
records (auto-purged, withdrawn, failed_in_transit, declined, rejected)
don't clutter the active My Deposits / Hosting tables. The endpoint
backs both depositor and host roles in one shot.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert

from nexus.api.local import router as local_router
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database
from nexus.storage.models import ForeignStorageDeposit


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


async def _seed(deposit_id, role, status, *, depositor_uuid="dep-uuid",
                host_uuid="host-uuid", total_bytes=10, created_at="2026-05-01T00:00:00+00:00"):
    async with database.get_session() as db:
        await db.execute(
            insert(ForeignStorageDeposit).values(
                deposit_id=deposit_id,
                role=role,
                depositor_uuid=depositor_uuid,
                host_uuid=host_uuid,
                status=status,
                total_bytes=total_bytes,
                chunk_count=1,
                transport="stream",
                created_at=created_at,
            )
        )
        await db.commit()


def test_histories_returns_only_terminal_states(client):
    asyncio.run(_seed("dep-active", "depositor", "stored"))
    asyncio.run(_seed("dep-purged", "depositor", "purged"))
    asyncio.run(_seed("dep-withdrawn", "depositor", "withdrawn"))
    asyncio.run(_seed("dep-failed", "depositor", "failed_in_transit"))
    asyncio.run(_seed("dep-declined", "depositor", "declined"))
    asyncio.run(_seed("dep-transferring", "depositor", "transferring"))

    res = client.get("/local/foreign_storage/histories")
    assert res.status_code == 200
    ids = {h["deposit_id"] for h in res.json()["histories"]}
    assert ids == {"dep-purged", "dep-withdrawn", "dep-failed", "dep-declined"}


def test_histories_covers_both_roles(client):
    asyncio.run(_seed("dep-side", "depositor", "purged"))
    asyncio.run(_seed("host-side", "host", "purged"))

    res = client.get("/local/foreign_storage/histories")
    histories = res.json()["histories"]
    roles = {h["role"] for h in histories}
    assert roles == {"depositor", "host"}


def test_histories_exposes_counterparty_uuid_by_role(client):
    """Host rows surface the depositor as the counterparty;
    depositor rows surface the host. The UI uses ``counterparty_uuid``
    + ``counterparty_display_name`` uniformly across roles."""
    asyncio.run(_seed("h-row", "host", "purged", depositor_uuid="depA", host_uuid="self"))
    asyncio.run(_seed("d-row", "depositor", "withdrawn", depositor_uuid="self", host_uuid="hostB"))

    by_id = {h["deposit_id"]: h for h in client.get("/local/foreign_storage/histories").json()["histories"]}
    assert by_id["h-row"]["counterparty_uuid"] == "depA"
    assert by_id["d-row"]["counterparty_uuid"] == "hostB"


def test_histories_orders_newest_first(client):
    asyncio.run(_seed("old-row", "depositor", "purged", created_at="2026-01-01T00:00:00+00:00"))
    asyncio.run(_seed("new-row", "depositor", "purged", created_at="2026-05-01T00:00:00+00:00"))

    histories = client.get("/local/foreign_storage/histories").json()["histories"]
    assert histories[0]["deposit_id"] == "new-row"
    assert histories[1]["deposit_id"] == "old-row"


def test_histories_empty_when_no_terminal_rows(client):
    asyncio.run(_seed("only-active", "depositor", "stored"))
    res = client.get("/local/foreign_storage/histories")
    assert res.status_code == 200
    assert res.json() == {"histories": []}
