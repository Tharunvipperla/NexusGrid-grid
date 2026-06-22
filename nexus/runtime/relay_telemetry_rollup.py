"""Daily rollup sweeper for RelayTelemetryBucket.

Job:

1. Collapse every ``hour`` bucket older than 24 hours into the
   corresponding ``day`` bucket (sum ``frame_count`` + ``state_changes``).
2. Collapse every ``day`` bucket older than 7 days into the corresponding
   ``week`` bucket.
3. Prune any bucket older than
   :data:`LOCAL_SETTINGS["relay_telemetry_retention_days"]` (default 14,
   ``0`` = unlimited).

Designed to be idempotent so the test fixture can fast-forward the clock
by injecting a ``now`` and re-running without surprises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, delete, select

from nexus.core import LOCAL_SETTINGS
from nexus.runtime.relay_telemetry import (
    BUCKET_DAY,
    BUCKET_HOUR,
    BUCKET_WEEK,
)
from nexus.storage import get_session
from nexus.storage.models import RelayTelemetryBucket

_log = logging.getLogger("nexus.runtime.relay_telemetry_rollup")


DEFAULT_RETENTION_DAYS = 14


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _day_floor(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_floor(ts: datetime) -> datetime:
    # Anchor weeks to Monday 00:00 UTC.
    floor = _day_floor(ts)
    return floor - timedelta(days=floor.weekday())


def _retention_days() -> int:
    raw = LOCAL_SETTINGS.get("relay_telemetry_retention_days", DEFAULT_RETENTION_DAYS)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return max(0, n)


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _roll_kind_to(
    *,
    src_kind: str,
    dst_kind: str,
    cutoff: datetime,
    bucket_floor: Callable[[datetime], datetime],
) -> int:
    """Sum every ``src_kind`` row whose ``bucket_start < cutoff`` into the
    enclosing ``dst_kind`` bucket, then delete the source rows.

    Returns the number of source rows collapsed.
    """
    collapsed = 0
    async with get_session() as session:
        src_rows = (
            await session.execute(
                select(RelayTelemetryBucket).where(
                    RelayTelemetryBucket.bucket_kind == src_kind
                )
            )
        ).scalars().all()
        merges: dict[tuple[str, str], tuple[int, int]] = {}
        to_delete: list[tuple[str, str, str]] = []
        for row in src_rows:
            ts = _parse_iso(row.bucket_start)
            if ts is None or ts >= cutoff:
                continue
            dst_start = bucket_floor(ts).isoformat()
            key = (row.relay_url, dst_start)
            cnt, sc = merges.get(key, (0, 0))
            merges[key] = (
                cnt + int(row.frame_count or 0),
                sc + int(row.state_changes or 0),
            )
            to_delete.append((row.relay_url, src_kind, row.bucket_start))
            collapsed += 1
        for (url, dst_start), (cnt, sc) in merges.items():
            dst = await session.get(
                RelayTelemetryBucket, (url, dst_kind, dst_start)
            )
            if dst is None:
                session.add(RelayTelemetryBucket(
                    relay_url=url,
                    bucket_kind=dst_kind,
                    bucket_start=dst_start,
                    frame_count=cnt,
                    state_changes=sc,
                ))
            else:
                dst.frame_count = int(dst.frame_count or 0) + cnt
                dst.state_changes = int(dst.state_changes or 0) + sc
        for url, kind, start in to_delete:
            await session.execute(
                delete(RelayTelemetryBucket).where(and_(
                    RelayTelemetryBucket.relay_url == url,
                    RelayTelemetryBucket.bucket_kind == kind,
                    RelayTelemetryBucket.bucket_start == start,
                ))
            )
        await session.commit()
    return collapsed


async def _prune_older_than(cutoff: datetime) -> int:
    """Drop any bucket whose ``bucket_start < cutoff``. Returns count."""
    pruned = 0
    async with get_session() as session:
        rows = (
            await session.execute(select(RelayTelemetryBucket))
        ).scalars().all()
        for row in rows:
            ts = _parse_iso(row.bucket_start)
            if ts is None or ts >= cutoff:
                continue
            await session.execute(
                delete(RelayTelemetryBucket).where(and_(
                    RelayTelemetryBucket.relay_url == row.relay_url,
                    RelayTelemetryBucket.bucket_kind == row.bucket_kind,
                    RelayTelemetryBucket.bucket_start == row.bucket_start,
                ))
            )
            pruned += 1
        await session.commit()
    return pruned


async def run_rollup(now: datetime | None = None) -> dict:
    """Execute one full rollup pass.

    Returns a small dict summarizing what changed — useful for the
    background loop's structured logs + for tests.
    """
    when = now or _now_utc()
    hour_cutoff = when - timedelta(hours=24)
    day_cutoff = when - timedelta(days=7)
    retention = _retention_days()

    collapsed_h2d = await _roll_kind_to(
        src_kind=BUCKET_HOUR, dst_kind=BUCKET_DAY,
        cutoff=hour_cutoff, bucket_floor=_day_floor,
    )
    collapsed_d2w = await _roll_kind_to(
        src_kind=BUCKET_DAY, dst_kind=BUCKET_WEEK,
        cutoff=day_cutoff, bucket_floor=_week_floor,
    )
    pruned = 0
    if retention > 0:
        prune_cutoff = when - timedelta(days=retention)
        pruned = await _prune_older_than(prune_cutoff)
    return {
        "hour_to_day": collapsed_h2d,
        "day_to_week": collapsed_d2w,
        "pruned": pruned,
        "retention_days": retention,
    }


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "run_rollup",
]
