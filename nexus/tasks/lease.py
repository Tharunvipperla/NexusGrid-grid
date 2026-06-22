"""Worker lease creation / refresh / expiration.

Extracted from Phase-1/node_modified.py (lines 1814-1834).

A lease is metadata stored in the task env_vars:

* ``NEXUS_META_LEASE_ID`` — unique identifier per lease attempt
* ``NEXUS_META_LEASE_OWNER`` — worker UUID that claimed the task
* ``NEXUS_META_LEASE_EXPIRES_AT`` — epoch seconds after which the lease is
  considered dead and the task may be reclaimed

The ``lease_seconds`` setting controls the TTL. Master nodes expire leases
via :mod:`nexus.scheduler` (retry path) and workers keep theirs fresh via
:func:`refresh_task_lease` inside :mod:`nexus.runtime`.
"""

from __future__ import annotations

import uuid

from nexus.core import LOCAL_SETTINGS
from nexus.storage import TaskRecord
from nexus.tasks.metadata import parse_task_env, write_task_env
from nexus.utils import now_epoch


def _lease_ttl(env: dict) -> float:
    """Per-task NEXUS_META_LEASE_SECONDS override, else the node setting."""
    try:
        override = float(env.get("NEXUS_META_LEASE_SECONDS", 0) or 0)
    except (TypeError, ValueError):
        override = 0.0
    return override if override > 0 else float(LOCAL_SETTINGS["lease_seconds"])


def set_task_lease(task: TaskRecord, worker_id: str) -> None:
    """Mint a fresh lease for *worker_id* with TTL = ``lease_seconds``."""
    env = parse_task_env(task)
    env["NEXUS_META_LEASE_ID"] = str(uuid.uuid4())
    env["NEXUS_META_LEASE_OWNER"] = worker_id
    env["NEXUS_META_LEASE_EXPIRES_AT"] = now_epoch() + _lease_ttl(env)
    write_task_env(task, env)


def refresh_task_lease(task: TaskRecord) -> None:
    """Extend the expiry on the current lease without changing the owner."""
    env = parse_task_env(task)
    env["NEXUS_META_LEASE_EXPIRES_AT"] = now_epoch() + _lease_ttl(env)
    write_task_env(task, env)


def task_lease_expired(task: TaskRecord) -> bool:
    """Return ``True`` when the task has a lease whose deadline has passed."""
    lease_exp = float(
        parse_task_env(task).get("NEXUS_META_LEASE_EXPIRES_AT", 0) or 0
    )
    return bool(lease_exp and now_epoch() > lease_exp)


def task_lease_owner(task: TaskRecord) -> str:
    """Worker UUID currently holding the lease (``""`` if none)."""
    return str(parse_task_env(task).get("NEXUS_META_LEASE_OWNER", "") or "")


__all__ = [
    "set_task_lease",
    "refresh_task_lease",
    "task_lease_expired",
    "task_lease_owner",
]
