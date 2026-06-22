"""P3 — ``active_workers`` metric must not count workers we know are offline."""

from __future__ import annotations

import time

import pytest

from nexus.core import STATE
from nexus.telemetry import presence
from nexus.telemetry.observability import _active_workers_metric_value


WORKER_LIVE = "10.0.0.1:9000"
WORKER_STALE = "10.0.0.2:9000"
WORKER_OFFLINE = "10.0.0.3:9000"


@pytest.fixture(autouse=True)
def _reset():
    STATE.active_workers.clear()
    STATE.peer_presence.clear()
    yield
    STATE.active_workers.clear()
    STATE.peer_presence.clear()


def test_counts_only_freshly_seen_workers():
    now = time.time()
    STATE.active_workers[WORKER_LIVE] = {"stats": {}, "last_seen": now}
    STATE.active_workers[WORKER_STALE] = {"stats": {}, "last_seen": now - 30}
    assert _active_workers_metric_value() == 1


def test_excludes_workers_marked_offline_by_presence():
    now = time.time()
    STATE.active_workers[WORKER_LIVE] = {"stats": {}, "last_seen": now}
    STATE.active_workers[WORKER_OFFLINE] = {"stats": {}, "last_seen": now}
    presence.mark_peer_offline(WORKER_OFFLINE, source="test")
    assert _active_workers_metric_value() == 1


def test_zero_when_all_are_stale_or_offline():
    now = time.time()
    STATE.active_workers[WORKER_STALE] = {"stats": {}, "last_seen": now - 60}
    STATE.active_workers[WORKER_OFFLINE] = {"stats": {}, "last_seen": now}
    presence.mark_peer_offline(WORKER_OFFLINE, source="test")
    assert _active_workers_metric_value() == 0


def test_missing_last_seen_is_treated_as_stale():
    # Defensive: a malformed entry without last_seen should not inflate the count.
    STATE.active_workers["odd"] = {"stats": {}}
    assert _active_workers_metric_value() == 0
