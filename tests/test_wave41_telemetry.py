"""Wave 41 — frame-count instrumentation + sampler."""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import relay_telemetry as rt
from nexus.security import tokens
from nexus.storage import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    # Reset in-memory counters between tests so prior runs don't bleed.
    asyncio.run(rt._drain_counters())
    yield url
    asyncio.run(rt._drain_counters())


def test_increment_then_sample_writes_bucket(isolated_db):
    async def _go():
        for _ in range(7):
            await rt.increment("wss://r.example")
        total = await rt.sample_once()
        bucket = rt._current_hour_bucket_start()
        persisted = await rt.bucket_total(
            "wss://r.example", rt.BUCKET_HOUR, bucket,
        )
        return total, persisted
    total, persisted = asyncio.run(_go())
    assert total == 7
    assert persisted == 7


def test_sample_with_no_traffic_returns_zero(isolated_db):
    total = asyncio.run(rt.sample_once())
    assert total == 0


def test_repeated_sampling_in_same_hour_accumulates(isolated_db):
    async def _go():
        await rt.increment("wss://r.example", by=3)
        await rt.sample_once()
        await rt.increment("wss://r.example", by=2)
        await rt.sample_once()
        bucket = rt._current_hour_bucket_start()
        return await rt.bucket_total(
            "wss://r.example", rt.BUCKET_HOUR, bucket,
        )
    assert asyncio.run(_go()) == 5


def test_independent_urls_get_independent_buckets(isolated_db):
    async def _go():
        await rt.increment("wss://a", by=4)
        await rt.increment("wss://b", by=11)
        await rt.sample_once()
        bucket = rt._current_hour_bucket_start()
        return (
            await rt.bucket_total("wss://a", rt.BUCKET_HOUR, bucket),
            await rt.bucket_total("wss://b", rt.BUCKET_HOUR, bucket),
        )
    a, b = asyncio.run(_go())
    assert (a, b) == (4, 11)


def test_drain_zeros_in_memory_state(isolated_db):
    async def _go():
        await rt.increment("wss://r.example", by=10)
        first = await rt.sample_once()
        # No new increments → second drain should report nothing
        second = await rt.sample_once()
        return first, second
    first, second = asyncio.run(_go())
    assert first == 10
    assert second == 0


def test_empty_url_ignored(isolated_db):
    async def _go():
        await rt.increment("")
        await rt.increment("", by=99)
        return await rt.sample_once()
    assert asyncio.run(_go()) == 0
