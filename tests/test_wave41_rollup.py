"""Wave 41 — daily rollup sweeper for the telemetry archive."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import relay_telemetry as rt
from nexus.runtime import relay_telemetry_rollup as rr
from nexus.security import tokens
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


def _seed(url: str, kind: str, start: datetime, frame_count: int = 1) -> None:
    async def _go():
        async with get_session() as s:
            s.add(RelayTelemetryBucket(
                relay_url=url, bucket_kind=kind,
                bucket_start=start.isoformat(),
                frame_count=frame_count,
            ))
            await s.commit()
    asyncio.run(_go())


def _count(url: str, kind: str) -> int:
    async def _go():
        from sqlalchemy import select
        async with get_session() as s:
            rows = (
                await s.execute(
                    select(RelayTelemetryBucket).where(
                        RelayTelemetryBucket.relay_url == url,
                        RelayTelemetryBucket.bucket_kind == kind,
                    )
                )
            ).scalars().all()
            return len(rows), sum(int(r.frame_count or 0) for r in rows)
    return asyncio.run(_go())


def test_hour_rolls_into_day_after_24h(isolated_db):
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    # Two hour buckets from yesterday morning + one from now.
    yesterday_8am = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    yesterday_9am = datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc)
    recent_11am = datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc)
    _seed("wss://r", rt.BUCKET_HOUR, yesterday_8am, frame_count=5)
    _seed("wss://r", rt.BUCKET_HOUR, yesterday_9am, frame_count=7)
    _seed("wss://r", rt.BUCKET_HOUR, recent_11am, frame_count=3)

    asyncio.run(rr.run_rollup(now=now))

    h_count, h_sum = _count("wss://r", rt.BUCKET_HOUR)
    d_count, d_sum = _count("wss://r", rt.BUCKET_DAY)
    # Recent hour bucket (within 24h) survives untouched.
    assert h_count == 1
    assert h_sum == 3
    # Both old hour buckets collapsed into one day bucket (2026-05-29).
    assert d_count == 1
    assert d_sum == 12


def test_day_rolls_into_week_after_7d(isolated_db):
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    eight_days_ago = datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc)
    nine_days_ago = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc)
    _seed("wss://r", rt.BUCKET_DAY, eight_days_ago, frame_count=4)
    _seed("wss://r", rt.BUCKET_DAY, nine_days_ago, frame_count=6)
    _seed("wss://r", rt.BUCKET_DAY, yesterday, frame_count=10)

    asyncio.run(rr.run_rollup(now=now))

    d_count, d_sum = _count("wss://r", rt.BUCKET_DAY)
    w_count, w_sum = _count("wss://r", rt.BUCKET_WEEK)
    # Yesterday's bucket (within 7d) untouched.
    assert d_count == 1
    assert d_sum == 10
    # Both old day buckets collapsed into the same Monday-anchored week.
    assert w_count == 1
    assert w_sum == 10


def test_retention_prunes_old_week_buckets(isolated_db):
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    LOCAL_SETTINGS["relay_telemetry_retention_days"] = 14
    ancient = now - timedelta(days=30)
    recent = now - timedelta(days=10)
    _seed("wss://r", rt.BUCKET_WEEK, ancient, frame_count=99)
    _seed("wss://r", rt.BUCKET_WEEK, recent, frame_count=7)

    summary = asyncio.run(rr.run_rollup(now=now))

    w_count, w_sum = _count("wss://r", rt.BUCKET_WEEK)
    assert summary["pruned"] >= 1
    assert w_count == 1
    assert w_sum == 7


def test_retention_zero_keeps_everything(isolated_db):
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    LOCAL_SETTINGS["relay_telemetry_retention_days"] = 0
    ancient = now - timedelta(days=400)
    _seed("wss://r", rt.BUCKET_WEEK, ancient, frame_count=1)

    summary = asyncio.run(rr.run_rollup(now=now))

    w_count, _ = _count("wss://r", rt.BUCKET_WEEK)
    assert w_count == 1
    assert summary["pruned"] == 0


def test_rollup_is_idempotent(isolated_db):
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    yesterday = datetime(2026, 5, 29, 8, 0, tzinfo=timezone.utc)
    _seed("wss://r", rt.BUCKET_HOUR, yesterday, frame_count=4)

    asyncio.run(rr.run_rollup(now=now))
    after_first = _count("wss://r", rt.BUCKET_DAY)

    asyncio.run(rr.run_rollup(now=now))
    after_second = _count("wss://r", rt.BUCKET_DAY)

    assert after_first == after_second


def test_run_rollup_summary_shape(isolated_db):
    summary = asyncio.run(rr.run_rollup())
    assert set(summary) == {
        "hour_to_day", "day_to_week", "pruned", "retention_days",
    }
