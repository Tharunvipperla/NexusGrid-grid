"""Wave 41 — telemetry export + manual purge endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.relay_admin import router as relay_admin_router
from nexus.runtime import relay_telemetry as rt
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database, get_session
from nexus.storage.models import RelayTelemetryBucket


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(relay_admin_router)
    app.dependency_overrides[verify_local_auth] = lambda: True
    return TestClient(app)


def _seed_bucket(url: str, kind: str, start: datetime, frame_count: int) -> None:
    async def _go():
        async with get_session() as s:
            s.add(RelayTelemetryBucket(
                relay_url=url, bucket_kind=kind,
                bucket_start=start.isoformat(),
                frame_count=frame_count,
            ))
            await s.commit()
    asyncio.run(_go())


def test_export_json_returns_seeded_buckets(client):
    t0 = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc)
    _seed_bucket("wss://r", rt.BUCKET_HOUR, t0, 5)
    _seed_bucket("wss://r", rt.BUCKET_HOUR, t1, 11)

    res = client.get("/local/relay/telemetry/export?format=json")
    assert res.status_code == 200
    body = json.loads(res.text)
    assert len(body["buckets"]) == 2
    total = sum(b["frame_count"] for b in body["buckets"])
    assert total == 16


def test_export_csv_streams_rows(client):
    t = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    _seed_bucket("wss://x", rt.BUCKET_HOUR, t, 4)
    res = client.get("/local/relay/telemetry/export?format=csv")
    assert res.status_code == 200
    lines = res.text.strip().splitlines()
    assert lines[0].startswith("relay_url,bucket_kind,bucket_start")
    assert "wss://x,hour" in lines[1]
    assert ",4," in lines[1]


def test_export_filters_by_since_until(client):
    base = datetime(2026, 5, 30, 0, 0, tzinfo=timezone.utc)
    for h in range(0, 6):
        _seed_bucket("wss://r", rt.BUCKET_HOUR, base + timedelta(hours=h), h + 1)
    since = (base + timedelta(hours=2)).isoformat()
    until = (base + timedelta(hours=5)).isoformat()
    res = client.get(
        f"/local/relay/telemetry/export?since={since}&until={until}&format=json"
    )
    body = json.loads(res.text)
    counts = sorted(b["frame_count"] for b in body["buckets"])
    # Hours 2, 3, 4 fall in [2,5) → frame_counts 3, 4, 5
    assert counts == [3, 4, 5]


def test_purge_requires_before(client):
    res = client.request("DELETE", "/local/relay/telemetry", json={"before": ""})
    assert res.status_code == 400


def test_purge_deletes_old_buckets_only(client):
    old = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    keep = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
    _seed_bucket("wss://r", rt.BUCKET_HOUR, old, 100)
    _seed_bucket("wss://r", rt.BUCKET_HOUR, keep, 50)

    cutoff = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc).isoformat()
    res = client.request("DELETE", "/local/relay/telemetry", json={"before": cutoff})
    assert res.status_code == 200
    assert res.json()["pruned"] == 1

    # Surviving bucket still readable via export.
    res = client.get("/local/relay/telemetry/export?format=json")
    body = json.loads(res.text)
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["frame_count"] == 50


def test_retention_round_trip(client):
    res = client.get("/local/relay/telemetry/retention")
    assert res.status_code == 200
    default_days = res.json()["days"]
    assert default_days >= 0

    res = client.post("/local/relay/telemetry/retention", json={"days": 30})
    assert res.status_code == 200
    assert res.json()["days"] == 30

    res = client.get("/local/relay/telemetry/retention")
    assert res.json()["days"] == 30


def test_retention_rejects_negative(client):
    res = client.post("/local/relay/telemetry/retention", json={"days": -1})
    assert res.status_code == 422


def test_retention_zero_means_unlimited(client):
    res = client.post("/local/relay/telemetry/retention", json={"days": 0})
    assert res.status_code == 200
    assert res.json()["days"] == 0
