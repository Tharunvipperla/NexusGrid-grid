"""Rollup + retention sweeper for GroupComputeBucket.

Mirrors :mod:`nexus.runtime.relay_telemetry_rollup`:

1. Collapse ``hour`` buckets older than 24h into their ``day`` bucket.
2. Collapse ``day`` buckets older than 7d into their ``week`` bucket.
3. Prune anything older than
   ``LOCAL_SETTINGS["pool_telemetry_retention_days"]`` (default 14, 0 = unlimited).

Idempotent so a test can inject ``now`` and re-run without surprises.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, delete, select

from nexus.core import LOCAL_SETTINGS
from nexus.runtime.group_compute_telemetry import (
    BUCKET_DAY,
    BUCKET_HOUR,
    BUCKET_WEEK,
    _FIELDS,
)
from nexus.storage import get_session
from nexus.storage.models import GroupComputeBucket

_log = logging.getLogger("nexus.runtime.group_compute_telemetry_rollup")

DEFAULT_RETENTION_DAYS = 14
ROLLUP_INTERVAL_SEC = 3600  # sweep hourly; collapses/prunes are time-gated


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _day_floor(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_floor(ts: datetime) -> datetime:
    floor = _day_floor(ts)
    return floor - timedelta(days=floor.weekday())


def _retention_days() -> int:
    raw = LOCAL_SETTINGS.get("pool_telemetry_retention_days", DEFAULT_RETENTION_DAYS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _roll_kind_to(
    *, src_kind: str, dst_kind: str, cutoff: datetime,
    bucket_floor: Callable[[datetime], datetime],
) -> int:
    """Sum every ``src_kind`` row older than *cutoff* into its enclosing
    ``dst_kind`` bucket, then delete the source rows. Returns rows collapsed."""
    collapsed = 0
    async with get_session() as session:
        src_rows = (
            await session.execute(
                select(GroupComputeBucket).where(
                    GroupComputeBucket.bucket_kind == src_kind
                )
            )
        ).scalars().all()
        merges: dict[tuple[str, str, str], dict[str, int]] = {}
        to_delete: list[tuple[str, str, str, str]] = []
        for row in src_rows:
            ts = _parse_iso(row.bucket_start)
            if ts is None or ts >= cutoff:
                continue
            dst_start = bucket_floor(ts).isoformat()
            key = (row.group_id, row.member_pubkey, dst_start)
            acc = merges.setdefault(key, {f: 0 for f in _FIELDS})
            for f in _FIELDS:
                acc[f] += int(getattr(row, f, 0) or 0)
            to_delete.append(
                (row.group_id, row.member_pubkey, src_kind, row.bucket_start)
            )
            collapsed += 1
        for (gid, member, dst_start), acc in merges.items():
            dst = await session.get(
                GroupComputeBucket, (gid, member, dst_kind, dst_start)
            )
            if dst is None:
                dst = GroupComputeBucket(
                    group_id=gid, member_pubkey=member,
                    bucket_kind=dst_kind, bucket_start=dst_start,
                )
                session.add(dst)
            for f in _FIELDS:
                setattr(dst, f, int(getattr(dst, f, 0) or 0) + acc[f])
        for gid, member, kind, start in to_delete:
            await session.execute(
                delete(GroupComputeBucket).where(and_(
                    GroupComputeBucket.group_id == gid,
                    GroupComputeBucket.member_pubkey == member,
                    GroupComputeBucket.bucket_kind == kind,
                    GroupComputeBucket.bucket_start == start,
                ))
            )
        await session.commit()
    return collapsed


async def _prune_older_than(cutoff: datetime) -> int:
    pruned = 0
    async with get_session() as session:
        rows = (
            await session.execute(select(GroupComputeBucket))
        ).scalars().all()
        for row in rows:
            ts = _parse_iso(row.bucket_start)
            if ts is None or ts >= cutoff:
                continue
            await session.execute(
                delete(GroupComputeBucket).where(and_(
                    GroupComputeBucket.group_id == row.group_id,
                    GroupComputeBucket.member_pubkey == row.member_pubkey,
                    GroupComputeBucket.bucket_kind == row.bucket_kind,
                    GroupComputeBucket.bucket_start == row.bucket_start,
                ))
            )
            pruned += 1
        await session.commit()
    return pruned


async def run_rollup(now: datetime | None = None) -> dict:
    """One full rollup pass. Returns a summary dict for logs + tests."""
    when = now or _now_utc()
    retention = _retention_days()
    collapsed_h2d = await _roll_kind_to(
        src_kind=BUCKET_HOUR, dst_kind=BUCKET_DAY,
        cutoff=when - timedelta(hours=24), bucket_floor=_day_floor,
    )
    collapsed_d2w = await _roll_kind_to(
        src_kind=BUCKET_DAY, dst_kind=BUCKET_WEEK,
        cutoff=when - timedelta(days=7), bucket_floor=_week_floor,
    )
    pruned = 0
    if retention > 0:
        pruned = await _prune_older_than(when - timedelta(days=retention))
    return {
        "hour_to_day": collapsed_h2d,
        "day_to_week": collapsed_d2w,
        "pruned": pruned,
        "retention_days": retention,
    }


async def rollup_loop() -> None:
    """Forever: run a rollup pass every ``ROLLUP_INTERVAL_SEC`` seconds."""
    while True:
        try:
            await run_rollup()
        except Exception:
            _log.warning("pool telemetry rollup tick failed", exc_info=True)
        await asyncio.sleep(ROLLUP_INTERVAL_SEC)


__all__ = ["DEFAULT_RETENTION_DAYS", "run_rollup", "rollup_loop"]
