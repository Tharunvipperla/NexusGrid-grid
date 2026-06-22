"""Numeric counters and the periodic observability loop.

Extracted from Phase-1/node_modified.py (line 494, 1558-1560, 5800-…).

Metrics live in :data:`nexus.core.STATE.metrics`, a ``defaultdict(int)``
shared with the UI. Callers ``incr_metric("tasks_dispatched")`` on hot
paths; the ``observability_loop`` samples them + hardware state + queue
depth at a regular cadence and emits a UI broadcast.

The loop itself is started from :mod:`nexus.app` lifespan once all
subpackages are in place. The metric vocabulary (list of known keys) is
kept in :data:`KNOWN_METRICS` so the UI can render zero-baselines for
metrics that haven't fired yet.
"""

from __future__ import annotations

from nexus.core.state import STATE


KNOWN_METRICS: tuple[str, ...] = (
    "tasks_dispatched",
    "tasks_completed",
    "tasks_failed",
    "tasks_preempted",
    "tasks_disrupted",
    "tasks_cancelled",
    "task_retries",
    "tasks_requeued",
    "peer_joins",
    "peer_revokes",
    "relay_reconnects",
    "threat_findings",
)


def incr_metric(name: str, value: int = 1) -> None:
    """Increment metric *name* by *value* (default 1)."""
    STATE.metrics[name] += value


def get_metric(name: str) -> int:
    """Return metric *name* (0 if never set)."""
    return int(STATE.metrics.get(name, 0))


def snapshot_metrics() -> dict[str, int]:
    """Return a plain-dict copy of every metric, including zero-baselines."""
    snap = {k: 0 for k in KNOWN_METRICS}
    snap.update({k: int(v) for k, v in STATE.metrics.items()})
    return snap
