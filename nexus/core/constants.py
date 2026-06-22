"""Constants that are never user-configurable.

Extracted from node_modified.py (lines 167-199).

A constant belongs here if and only if:

* it is compiled into the binary (no setting ever overrides it), AND
* it is referenced from more than one module.

Transport defaults (ports, timeouts) that a user might want to change at
runtime live in :mod:`nexus.core.config`, not here.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------

TASK_STATES: frozenset[str] = frozenset(
    {
        "waiting",
        "awaiting_approval",
        "queued",
        "processing",
        "serving",
        "preempted",
        "disrupted",
        "cancelled",
        "retrying",
        "completed",
        "failed",
        "lease_expired",
    }
)
"""Every legal value of ``TaskRecord.status``.

``serving`` marks a long-running service task — the
container is up and exposing TCP ports. It is *active but non-terminal*;
the scheduler ignores it just like ``processing``.
"""

TERMINAL_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "disrupted"}
)
"""States from which a task never advances on its own.

A user can still re-queue a terminal task explicitly (see the
``ALLOWED_TRANSITIONS`` out-edges), but the scheduler will not touch one.
"""

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "waiting": frozenset({"queued", "awaiting_approval", "cancelled"}),
    "awaiting_approval": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"processing", "cancelled", "retrying"}),
    "processing": frozenset(
        {
            "completed",
            "failed",
            "preempted",
            "disrupted",
            "lease_expired",
            "cancelled",
            "retrying",
            "serving",
        }
    ),
    "serving": frozenset(
        {
            "completed",
            "failed",
            "preempted",
            "disrupted",
            "cancelled",
        }
    ),
    "preempted": frozenset({"retrying", "queued", "failed"}),
    "lease_expired": frozenset({"retrying", "queued", "failed"}),
    "retrying": frozenset({"queued", "cancelled", "failed"}),
    "failed": frozenset({"retrying", "queued"}),
    "completed": frozenset({"queued"}),
    "cancelled": frozenset(),
    "disrupted": frozenset({"retrying", "queued", "failed"}),
}
"""Directed graph of legal ``status`` transitions.

``set_task_status`` (in :mod:`nexus.tasks.lifecycle`) refuses any edge not
listed here. To add a new transition, edit this dict AND the corresponding
test in ``tests/test_task_lifecycle.py``.
"""


# ---------------------------------------------------------------------------
# Transport defaults
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 8000
DEFAULT_DISCOVERY_PORT = 34567
DEFAULT_GRID_KEY = "nexus-beta-key"

# Listen address used when binding the HTTP + WebSocket server.
DEFAULT_BIND_HOST = "0.0.0.0"

# Max age (seconds) before a peer with no heartbeat is considered offline.
PEER_PRESENCE_TIMEOUT = 30

# Cap on in-memory log buffer per task (number of lines).
MAX_LOG_LINES = 4000
