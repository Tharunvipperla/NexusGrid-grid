"""Time-bucketed pool-usage telemetry for THIS node.

Mirrors :mod:`nexus.runtime.relay_telemetry`: hot paths bump an in-memory
counter via :func:`record`; a 60 s sampler drains it into the current hour
``GroupComputeBucket``. Every delta is recorded twice — once for the specific
group and once for the node-global ``group_id="*"`` rollup — so the Diagnostics
"all groups" view and the per-group history share one pipeline.

Older buckets are collapsed + pruned by
:mod:`nexus.runtime.group_compute_telemetry_rollup`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from nexus.storage import get_session
from nexus.storage.models import GroupComputeBucket

_log = logging.getLogger("nexus.runtime.group_compute_telemetry")

SAMPLER_INTERVAL_SEC = 60

BUCKET_HOUR = "hour"
BUCKET_DAY = "day"
BUCKET_WEEK = "week"

GLOBAL_GROUP = "*"

# The numeric columns we accumulate. Keep in sync with GroupComputeBucket.
_FIELDS = (
    "tasks_contributed",
    "tasks_consumed",
    "compute_secs_contributed",
    "compute_secs_consumed",
    "storage_bytes_hosted",
    "storage_bytes_used",
)

# (group_id, field) -> pending count
_counters: dict[tuple[str, str], int] = defaultdict(int)
_counters_lock = asyncio.Lock()


async def record(group_id: str, **deltas: int) -> None:
    """Accumulate pool-usage deltas for *group_id* (and the global rollup).

    Unknown field names are ignored. Zero/negative deltas are skipped.
    Safe to call from any hot path; the sampler persists it later.
    """
    if not group_id:
        return
    pending = {k: int(v) for k, v in deltas.items() if k in _FIELDS and int(v)}
    if not pending:
        return
    async with _counters_lock:
        for field, val in pending.items():
            _counters[(group_id, field)] += val
            _counters[(GLOBAL_GROUP, field)] += val


def _current_hour_bucket_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0).isoformat()


async def _drain_counters() -> dict[tuple[str, str], int]:
    async with _counters_lock:
        if not _counters:
            return {}
        snapshot = dict(_counters)
        _counters.clear()
        return snapshot


async def sample_once() -> int:
    """Drain counters into the current hour bucket. Returns total deltas written."""
    snapshot = await _drain_counters()
    if not snapshot:
        return 0
    from nexus.security.group_keys import get_local_group_pubkey

    me = get_local_group_pubkey()
    bucket_start = _current_hour_bucket_start()
    # Regroup by group_id so each row is touched once.
    by_group: dict[str, dict[str, int]] = defaultdict(dict)
    for (gid, field), val in snapshot.items():
        by_group[gid][field] = val
    async with get_session() as session:
        for gid, fields in by_group.items():
            row = await session.get(
                GroupComputeBucket, (gid, me, BUCKET_HOUR, bucket_start)
            )
            if row is None:
                row = GroupComputeBucket(
                    group_id=gid, member_pubkey=me,
                    bucket_kind=BUCKET_HOUR, bucket_start=bucket_start,
                )
                session.add(row)
            for field, val in fields.items():
                setattr(row, field, int(getattr(row, field, 0) or 0) + int(val))
        await session.commit()
    return sum(snapshot.values())


def bucket_dict(row) -> dict:
    """Serialize a GroupComputeBucket row for the API/export."""
    d = {
        "group_id": row.group_id,
        "bucket_kind": row.bucket_kind,
        "bucket_start": row.bucket_start,
    }
    for f in _FIELDS:
        d[f] = int(getattr(row, f, 0) or 0)
    return d


def range_to_since(range_str: str) -> str:
    """Map a UI range ('24h'|'7d'|'30d'|'all') to an ISO lower bound ('' = all)."""
    from datetime import timedelta
    deltas = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
              "30d": timedelta(days=30)}
    d = deltas.get(str(range_str or "").strip())
    if d is None:
        return ""
    return (datetime.now(timezone.utc) - d).isoformat()


async def fetch_buckets(group_id: str, since: str = "") -> list[dict]:
    """All buckets for *group_id* (use ``'*'`` for the node-global rollup),
    ordered by bucket_start, optionally filtered to ``bucket_start >= since``."""
    from sqlalchemy import select
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupComputeBucket)
                .where(GroupComputeBucket.group_id == group_id)
                .order_by(GroupComputeBucket.bucket_start)
            )
        ).scalars().all()
    return [bucket_dict(r) for r in rows if not since or r.bucket_start >= since]


async def purge_before(group_id: str, before: str) -> int:
    """Delete buckets for *group_id* whose ``bucket_start`` < *before*."""
    from sqlalchemy import and_, delete, select
    pruned = 0
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupComputeBucket).where(
                    GroupComputeBucket.group_id == group_id
                )
            )
        ).scalars().all()
        for r in rows:
            if r.bucket_start < before:
                await session.execute(
                    delete(GroupComputeBucket).where(and_(
                        GroupComputeBucket.group_id == r.group_id,
                        GroupComputeBucket.member_pubkey == r.member_pubkey,
                        GroupComputeBucket.bucket_kind == r.bucket_kind,
                        GroupComputeBucket.bucket_start == r.bucket_start,
                    ))
                )
                pruned += 1
        await session.commit()
    return pruned


def buckets_csv(rows: list[dict]) -> str:
    """Render bucket dicts as CSV (header + rows)."""
    header = "group_id,bucket_kind,bucket_start," + ",".join(_FIELDS) + "\n"
    lines = [header]
    for d in rows:
        cells = [d["group_id"], d["bucket_kind"], d["bucket_start"]]
        cells += [str(d[f]) for f in _FIELDS]
        lines.append(",".join(cells) + "\n")
    return "".join(lines)


async def sampler_loop() -> None:
    """Forever: drain pool-usage counters every ``SAMPLER_INTERVAL_SEC`` seconds."""
    while True:
        try:
            await sample_once()
        except Exception:
            _log.warning("pool telemetry sampler tick failed", exc_info=True)
        await asyncio.sleep(SAMPLER_INTERVAL_SEC)


__all__ = [
    "SAMPLER_INTERVAL_SEC",
    "BUCKET_HOUR",
    "BUCKET_DAY",
    "BUCKET_WEEK",
    "GLOBAL_GROUP",
    "record",
    "sample_once",
    "sampler_loop",
    "bucket_dict",
    "range_to_since",
    "fetch_buckets",
    "purge_before",
    "buckets_csv",
    "_FIELDS",
    "_current_hour_bucket_start",
]
