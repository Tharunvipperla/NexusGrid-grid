"""Task lifecycle, queue, lease, metadata.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.tasks.lease import (
    refresh_task_lease,
    set_task_lease,
    task_lease_expired,
    task_lease_owner,
)
from nexus.tasks.lifecycle import (
    add_task_timeline_event,
    is_task_interrupted,
    is_task_preempted,
    mark_task_interrupted,
    mark_task_preempted,
    set_task_status,
    try_schedule_retry,
)
from nexus.tasks.metadata import (
    build_task_metadata,
    extract_task_metadata,
    get_retry_policy,
    parse_task_env,
    set_retry_policy,
    task_created_at,
    task_priority,
    task_retry_at,
    write_task_env,
)
from nexus.tasks.queue import (
    dequeue_task,
    enqueue_task,
    queue_depth,
    queue_empty,
)
from nexus.tasks.shadow import upsert_remote_shadow_task

__all__ = [
    # metadata
    "parse_task_env",
    "write_task_env",
    "task_priority",
    "task_created_at",
    "task_retry_at",
    "get_retry_policy",
    "set_retry_policy",
    "build_task_metadata",
    "extract_task_metadata",
    # lifecycle
    "set_task_status",
    "add_task_timeline_event",
    "try_schedule_retry",
    "mark_task_interrupted",
    "mark_task_preempted",
    "is_task_interrupted",
    "is_task_preempted",
    # lease
    "set_task_lease",
    "refresh_task_lease",
    "task_lease_expired",
    "task_lease_owner",
    # queue
    "enqueue_task",
    "dequeue_task",
    "queue_depth",
    "queue_empty",
    # shadow
    "upsert_remote_shadow_task",
]
