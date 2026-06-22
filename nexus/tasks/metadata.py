"""Task env-var serialization + metadata builders.

Extracted from node_modified.py:

* ``parse_task_env`` / ``write_task_env`` — lines 1668-1676
* ``build_task_metadata`` — lines 2370-2442
* ``extract_task_metadata`` — lines 2445-2484
* ``task_priority`` / ``task_created_at`` / ``task_retry_at`` — lines 1800-1811
* retry policy IO (``get_retry_policy`` / ``set_retry_policy``) — lines 1723-1760

Rationale
---------
All task metadata is stored as JSON inside ``TaskRecord.env_vars`` under
``NEXUS_META_*`` keys. This module owns serialization / deserialization so no
other package has to know the key names. Lifecycle, scheduler, and API
layers go through the helpers here.
"""

from __future__ import annotations

import json
from typing import Any

from nexus.core import LOCAL_SETTINGS, normalize_list_field
from nexus.storage import TaskRecord
from nexus.utils import now_epoch


# ---------------------------------------------------------------------------
# Env-var IO
# ---------------------------------------------------------------------------

def parse_task_env(task: TaskRecord) -> dict:
    """Return ``task.env_vars`` as a dict (empty dict on parse failure)."""
    try:
        return json.loads(task.env_vars or "{}")
    except Exception:
        return {}


def write_task_env(task: TaskRecord, env: dict) -> None:
    """Serialize *env* back into ``task.env_vars``."""
    task.env_vars = json.dumps(env)


# ---------------------------------------------------------------------------
# Scalar accessors (hot path — avoid reparsing the full env twice)
# ---------------------------------------------------------------------------

def task_priority(task: TaskRecord) -> int:
    """Return task priority clamped to ``[0, 100]`` (default 50)."""
    return max(
        0, min(100, int(parse_task_env(task).get("NEXUS_META_PRIORITY", 50) or 50))
    )


def task_created_at(task: TaskRecord) -> float:
    """Epoch seconds when the task record was first built."""
    return float(parse_task_env(task).get("NEXUS_META_CREATED_AT", 0) or 0)


def task_retry_at(task: TaskRecord) -> float:
    """Earliest epoch the task may re-enter the queue (0 if not retry-gated)."""
    return float(parse_task_env(task).get("NEXUS_META_NEXT_RETRY_AT", 0) or 0)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def get_retry_policy(task: TaskRecord) -> tuple[int, int, int]:
    """Return ``(retry_max, retry_count, backoff_base_seconds)``."""
    env = parse_task_env(task)
    return (
        max(0, int(env.get("NEXUS_META_RETRY_MAX", 2) or 2)),
        max(0, int(env.get("NEXUS_META_RETRY_COUNT", 0) or 0)),
        max(
            1,
            int(
                env.get(
                    "NEXUS_META_RETRY_BACKOFF_BASE",
                    LOCAL_SETTINGS["retry_backoff_base_sec"],
                )
                or LOCAL_SETTINGS["retry_backoff_base_sec"]
            ),
        ),
    )


def set_retry_policy(
    task: TaskRecord,
    retry_max: int,
    retry_count: int,
    next_retry_at: float | None,
    backoff_base: int,
) -> None:
    """Persist a full retry policy snapshot back into the task env."""
    env = parse_task_env(task)
    (
        env["NEXUS_META_RETRY_MAX"],
        env["NEXUS_META_RETRY_COUNT"],
        env["NEXUS_META_RETRY_BACKOFF_BASE"],
        env["NEXUS_META_NEXT_RETRY_AT"],
    ) = (
        max(0, int(retry_max)),
        max(0, int(retry_count)),
        max(1, int(backoff_base)),
        float(next_retry_at or 0),
    )
    write_task_env(task, env)


# ---------------------------------------------------------------------------
# Metadata builders
# ---------------------------------------------------------------------------

def build_task_metadata(
    base_env: dict | None = None,
    *,
    coordination_role: str = "requested",
    requested_by: str | None = None,
    display_id: str | None = None,
    preferred_worker: str | None = None,
    preferred_workers: list[str] | None = None,
    target_groups: list[str] | None = None,
    blocked_members: list[str] | None = None,
    priority: int = 50,
    retry_max: int = 2,
    retry_backoff_base: int | None = None,
    lease_seconds: int | None = None,
    required_tags: list[str] | None = None,
    require_gpu: bool = False,
    preferred_region: str = "",
    orphan_policy: str = "retry",
    queue_timeout_sec: int = 0,
    one_step_per_node: bool = False,
    prefer_reliable_workers: bool | None = None,
    step_gate: bool | None = None,
) -> dict:
    """Build the ``NEXUS_META_*`` env block for a newly-submitted task.

    Returns a plain dict suitable for ``write_task_env`` or embedding in a
    ``TaskRecord``. See ``tasks/README.md`` for the key vocabulary.
    """
    env = dict(base_env or {})
    env["NEXUS_META_COORDINATION"] = coordination_role
    if requested_by:
        env["NEXUS_META_REQUESTED_BY"] = requested_by
    if display_id:
        env["NEXUS_META_DISPLAY_ID"] = display_id
    env["NEXUS_META_CREATED_AT"] = float(env.get("NEXUS_META_CREATED_AT", now_epoch()))
    env["NEXUS_META_PRIORITY"] = max(0, min(100, int(priority)))
    env["NEXUS_META_RETRY_MAX"] = max(0, int(retry_max))
    env["NEXUS_META_RETRY_COUNT"] = max(
        0, int(env.get("NEXUS_META_RETRY_COUNT", 0) or 0)
    )
    env["NEXUS_META_RETRY_BACKOFF_BASE"] = max(
        1, int(retry_backoff_base or LOCAL_SETTINGS.get("retry_backoff_base_sec", 5))
    )
    # Per-task lease override: when present, set_task_lease and
    # refresh_task_lease use this TTL instead of the node setting.
    if lease_seconds and int(lease_seconds) > 0:
        env["NEXUS_META_LEASE_SECONDS"] = max(5, int(lease_seconds))
    env["NEXUS_META_NEXT_RETRY_AT"] = float(env.get("NEXUS_META_NEXT_RETRY_AT", 0) or 0)
    env["NEXUS_META_REQUIRED_TAGS"] = normalize_list_field(
        required_tags or env.get("NEXUS_META_REQUIRED_TAGS", [])
    )
    env["NEXUS_META_REQUIRE_GPU"] = bool(
        require_gpu or env.get("NEXUS_META_REQUIRE_GPU", False)
    )
    env["NEXUS_META_PREFERRED_REGION"] = str(
        preferred_region or env.get("NEXUS_META_PREFERRED_REGION", "")
    ).strip()
    env["NEXUS_ORPHAN_POLICY"] = str(orphan_policy or "retry")
    effective_timeout = (
        int(queue_timeout_sec)
        if int(queue_timeout_sec) > 0
        else int(LOCAL_SETTINGS.get("queue_timeout_sec", 0) or 0)
    )
    env["NEXUS_META_QUEUE_TIMEOUT"] = max(0, effective_timeout)
    env["NEXUS_META_QUEUED_AT"] = now_epoch()
    timeline = env.get("NEXUS_META_TIMELINE", [])
    if not isinstance(timeline, list):
        timeline = []
    if not timeline:
        timeline.append(
            {
                "ts": now_epoch(),
                "event": "created",
                "details": "Task metadata initialized",
            }
        )
    env["NEXUS_META_TIMELINE"] = timeline[-250:]
    merged_targets: list[str] = []
    if isinstance(preferred_workers, list):
        for worker_id in preferred_workers:
            if str(worker_id).strip() and str(worker_id).strip() not in merged_targets:
                merged_targets.append(str(worker_id).strip())
    if preferred_worker and str(preferred_worker).strip() not in merged_targets:
        merged_targets.append(str(preferred_worker).strip())
    if merged_targets:
        env["NEXUS_META_PREFERRED_WORKERS"] = merged_targets
        env["NEXUS_META_PREFERRED_WORKER"] = merged_targets[0]
    # Group-scoped compute. Empty list = grid-wide (default).
    group_targets = normalize_list_field(
        target_groups if target_groups is not None
        else env.get("NEXUS_META_TARGET_GROUPS", [])
    )
    if group_targets:
        env["NEXUS_META_TARGET_GROUPS"] = group_targets
    # Per-dispatch member exclusion (node UUIDs of blocked members).
    blocked = normalize_list_field(
        blocked_members if blocked_members is not None
        else env.get("NEXUS_META_BLOCKED_MEMBERS", [])
    )
    if blocked:
        env["NEXUS_META_BLOCKED_MEMBERS"] = blocked
    # DAG anti-affinity: when set, a node already running a sibling step of this
    # workflow is skipped for further steps (enforced in select_task_for_worker).
    if one_step_per_node:
        env["NEXUS_META_ONE_STEP_PER_NODE"] = True
    # Reliability-aware scheduling override. Only stored when explicitly set
    # (True/False) so the selector can tell "inherit the node default" (absent)
    # from an explicit per-task on/off choice.
    if prefer_reliable_workers is not None:
        env["NEXUS_META_PREFER_RELIABLE"] = bool(prefer_reliable_workers)
    # Step-gate: when on, the DAG scheduler holds a step at "awaiting_approval"
    # once its deps complete (instead of queuing it) until the user approves the
    # level. Only stored when explicitly set so "inherit the node default" is
    # distinguishable from an explicit per-dispatch choice.
    if step_gate is not None:
        env["NEXUS_META_STEP_GATE"] = bool(step_gate)
    return env


def extract_task_metadata(task: TaskRecord) -> dict:
    """Project a ``TaskRecord`` into the compact dict the UI expects."""
    from nexus.core.identity import NODE_UUID  # local import to avoid cycles

    env = parse_task_env(task)
    coordination = env.get("NEXUS_META_COORDINATION", "requested")
    requested_by = env.get("NEXUS_META_REQUESTED_BY")
    preferred_workers = normalize_list_field(
        env.get("NEXUS_META_PREFERRED_WORKERS", [])
    )
    if (
        env.get("NEXUS_META_PREFERRED_WORKER")
        and env.get("NEXUS_META_PREFERRED_WORKER") not in preferred_workers
    ):
        preferred_workers.insert(0, env.get("NEXUS_META_PREFERRED_WORKER"))
    group_targets = normalize_list_field(
        env.get("NEXUS_META_TARGET_GROUPS", [])
    )
    coordination_text = (
        f"Serving {requested_by or 'Unknown'}"
        if coordination == "serving"
        else (
            f"Sent to {task.worker}"
            if task.worker
            else (
                f"Pinned to {', '.join(preferred_workers)}"
                if preferred_workers
                else f"Requested by {requested_by or NODE_UUID}"
            )
        )
    )
    # Surface the originating group(s) on the worker's view too.
    if group_targets:
        coordination_text += f" · via group {', '.join(group_targets)}"
    return {
        "display_id": env.get("NEXUS_META_DISPLAY_ID", task.id),
        "coordination": coordination,
        "requested_by": requested_by,
        "preferred_worker": preferred_workers[0] if preferred_workers else None,
        "preferred_workers": preferred_workers,
        "target_groups": group_targets,
        "blocked_members": normalize_list_field(
            env.get("NEXUS_META_BLOCKED_MEMBERS", [])
        ),
        "one_step_per_node": bool(env.get("NEXUS_META_ONE_STEP_PER_NODE", False)),
        # None when unset (inherit node default); True/False when overridden.
        "prefer_reliable_workers": env.get("NEXUS_META_PREFER_RELIABLE"),
        "step_gate": env.get("NEXUS_META_STEP_GATE"),
        "priority": int(env.get("NEXUS_META_PRIORITY", 50) or 50),
        "retry_max": int(env.get("NEXUS_META_RETRY_MAX", 2) or 2),
        "retry_count": int(env.get("NEXUS_META_RETRY_COUNT", 0) or 0),
        "timeline": env.get("NEXUS_META_TIMELINE", [])[-50:],
        "queue_timeout": int(env.get("NEXUS_META_QUEUE_TIMEOUT", 0) or 0),
        "queued_at": float(env.get("NEXUS_META_QUEUED_AT", 0) or 0),
        "started_at": float(env.get("NEXUS_META_STARTED_AT", 0) or 0),
        "completed_at": float(env.get("NEXUS_META_COMPLETED_AT", 0) or 0),
        "elapsed_secs": _task_elapsed_secs(env),
        "coordination_text": coordination_text,
        "has_download": coordination != "serving" and task.status == "completed",
    }


def _task_elapsed_secs(env: dict) -> float | None:
    """Run time: completed-started when finished, else started-now while live."""
    started = float(env.get("NEXUS_META_STARTED_AT", 0) or 0)
    if not started:
        return None
    completed = float(env.get("NEXUS_META_COMPLETED_AT", 0) or 0)
    end = completed if completed else now_epoch()
    return round(max(0.0, end - started), 1)


__all__ = [
    "parse_task_env",
    "write_task_env",
    "task_priority",
    "task_created_at",
    "task_retry_at",
    "get_retry_policy",
    "set_retry_policy",
    "build_task_metadata",
    "extract_task_metadata",
]
