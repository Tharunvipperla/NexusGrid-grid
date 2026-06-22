"""Wave 48 — time-bucketed pool-usage telemetry (sampler + rollup + export)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nexus.runtime import group_compute_telemetry as gct
from nexus.runtime import group_compute_telemetry_rollup as roll
from nexus.security import group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import GroupComputeBucket


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    asyncio.run(database.init_db(0, url=f"sqlite+aiosqlite:///{(tmp_path/'g.db').as_posix()}"))
    # Start each test with empty in-memory counters.
    gct._counters.clear()
    yield

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    gct._counters.clear()
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def _all_rows():
    async def _go():
        from sqlalchemy import select
        async with get_session() as s:
            return (await s.execute(select(GroupComputeBucket))).scalars().all()
    return asyncio.run(_go())


def test_record_and_sample_writes_group_and_global(isolated_db):
    asyncio.run(gct.record("g1", tasks_contributed=2, compute_secs_contributed=30))
    total = asyncio.run(gct.sample_once())
    assert total == 64  # (2+30) recorded twice (group + global)
    rows = {(r.group_id, r.bucket_kind): r for r in _all_rows()}
    assert rows[("g1", "hour")].tasks_contributed == 2
    assert rows[("g1", "hour")].compute_secs_contributed == 30
    # Global rollup row mirrors it.
    assert rows[("*", "hour")].tasks_contributed == 2


def test_sample_accumulates_into_same_hour(isolated_db):
    asyncio.run(gct.record("g1", tasks_consumed=1))
    asyncio.run(gct.sample_once())
    asyncio.run(gct.record("g1", tasks_consumed=4))
    asyncio.run(gct.sample_once())
    rows = {(r.group_id, r.bucket_kind): r for r in _all_rows()}
    assert rows[("g1", "hour")].tasks_consumed == 5


def test_rollup_collapses_hour_to_day_then_week_and_prunes(isolated_db):
    # Seed an old hour bucket directly (25h ago) for both group + global.
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).replace(
        minute=0, second=0, microsecond=0).isoformat()

    async def _seed():
        async with get_session() as s:
            for gid in ("g1", "*"):
                s.add(GroupComputeBucket(
                    group_id=gid, member_pubkey="me", bucket_kind="hour",
                    bucket_start=old, tasks_contributed=3, compute_secs_contributed=10,
                ))
            await s.commit()
    asyncio.run(_seed())

    summary = asyncio.run(roll.run_rollup())
    assert summary["hour_to_day"] == 2  # both rows collapsed
    rows = {(r.group_id, r.bucket_kind): r for r in _all_rows()}
    assert ("g1", "hour") not in rows  # source gone
    assert rows[("g1", "day")].tasks_contributed == 3

    # Retention=1 day prunes the just-made day bucket (dated 25h ago).
    from nexus.core import LOCAL_SETTINGS
    LOCAL_SETTINGS["pool_telemetry_retention_days"] = 1
    summary2 = asyncio.run(roll.run_rollup())
    assert summary2["pruned"] >= 1
    LOCAL_SETTINGS.pop("pool_telemetry_retention_days", None)


def test_fetch_and_csv_export(isolated_db):
    asyncio.run(gct.record("g1", storage_bytes_used=4096))
    asyncio.run(gct.sample_once())
    rows = asyncio.run(gct.fetch_buckets("g1"))
    assert rows and rows[0]["storage_bytes_used"] == 4096
    csv = gct.buckets_csv(rows)
    assert "group_id,bucket_kind,bucket_start" in csv
    assert "4096" in csv


def test_purge_before_removes_old(isolated_db):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()

    async def _seed():
        async with get_session() as s:
            s.add(GroupComputeBucket(
                group_id="g1", member_pubkey="me", bucket_kind="day",
                bucket_start=old, tasks_contributed=1,
            ))
            await s.commit()
    asyncio.run(_seed())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pruned = asyncio.run(gct.purge_before("g1", cutoff))
    assert pruned == 1
    assert not _all_rows()
