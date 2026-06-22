"""Relay frame-counter + telemetry archive.

Two pieces:

1. An in-memory ``dict[url, int]`` that ``relay_send`` /
   ``relay_send_to_peer`` increment on every successful send. Atomic-int
   semantics under an asyncio.Lock — cheap, lockless on hot paths in
   practice because the contention is microseconds.

2. A 60-second sampler that drains the counters into the
   ``RelayTelemetryBucket`` table's current ``hour`` bucket. Older
   buckets get collapsed by :mod:`nexus.runtime.relay_telemetry_rollup`
   (step 6a) and pruned per
   :data:`LOCAL_SETTINGS["relay_telemetry_retention_days"]`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from nexus.storage import get_session
from nexus.storage.models import RelayTelemetryBucket

_log = logging.getLogger("nexus.runtime.relay_telemetry")

#: Sampler tick — how often the in-memory counter is drained into the
#: current hour bucket. Trade-off: shorter = finer-grained on-disk
#: history, longer = fewer DB writes. 60 s gives ~24 writes/h/relay.
SAMPLER_INTERVAL_SEC = 60

BUCKET_HOUR = "hour"
BUCKET_DAY = "day"
BUCKET_WEEK = "week"


_counters: dict[str, int] = defaultdict(int)
_counters_lock = asyncio.Lock()


async def increment(relay_url: str, by: int = 1) -> None:
    """Bump the in-memory frame counter for *relay_url* by *by* (default 1).

    Called from the relay send paths after a successful WS ``ws.send``.
    Empty URLs are silently ignored — happens during early bring-up
    before the legacy primary is configured.
    """
    if not relay_url:
        return
    async with _counters_lock:
        _counters[relay_url] += by


def _current_hour_bucket_start() -> str:
    """ISO8601 timestamp truncated to the current hour boundary (UTC)."""
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0).isoformat()


async def _drain_counters() -> dict[str, int]:
    """Atomic snapshot-and-zero of the in-memory counters."""
    async with _counters_lock:
        if not _counters:
            return {}
        snapshot = dict(_counters)
        _counters.clear()
        return snapshot


async def sample_once() -> int:
    """Drain counters + write into the current hour bucket.

    Returns the total number of frames recorded by this tick (sum across
    all URLs). Used by the verification test and by the integration
    sampler. Idempotent on the bucket — repeated calls within the same
    hour accumulate into the same row.
    """
    snapshot = await _drain_counters()
    if not snapshot:
        return 0
    bucket_start = _current_hour_bucket_start()
    async with get_session() as session:
        for url, count in snapshot.items():
            row = await session.get(
                RelayTelemetryBucket, (url, BUCKET_HOUR, bucket_start)
            )
            if row is None:
                session.add(RelayTelemetryBucket(
                    relay_url=url,
                    bucket_kind=BUCKET_HOUR,
                    bucket_start=bucket_start,
                    frame_count=int(count),
                ))
            else:
                row.frame_count = int(row.frame_count or 0) + int(count)
        await session.commit()
    return sum(snapshot.values())


async def sampler_loop() -> None:
    """Forever: tick every :data:`SAMPLER_INTERVAL_SEC` seconds."""
    while True:
        try:
            await sample_once()
        except Exception:
            _log.warning("relay telemetry sampler tick failed", exc_info=True)
        await asyncio.sleep(SAMPLER_INTERVAL_SEC)


async def bucket_total(
    relay_url: str, bucket_kind: str, bucket_start: str,
) -> int:
    """Test helper — current persisted frame_count for one bucket cell."""
    async with get_session() as session:
        row = await session.get(
            RelayTelemetryBucket, (relay_url, bucket_kind, bucket_start)
        )
    return int(row.frame_count or 0) if row else 0


__all__ = [
    "SAMPLER_INTERVAL_SEC",
    "BUCKET_HOUR",
    "BUCKET_DAY",
    "BUCKET_WEEK",
    "increment",
    "sample_once",
    "sampler_loop",
    "bucket_total",
    "_current_hour_bucket_start",
]
