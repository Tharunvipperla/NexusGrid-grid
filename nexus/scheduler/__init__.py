"""Worker fitness, task selection, retry policy, DAG resolution.

See ``README.md`` for the contract. Public surface re-exported below.

``local_capabilities`` / ``task_required_caps`` / ``image_allowed`` live in
:mod:`nexus.runtime.capacity` but are re-exported here because the
scheduler README documents them as part of the scheduling surface.
"""

from nexus.runtime.capacity import (
    image_allowed,
    local_capabilities,
    task_required_caps,
)
from nexus.runtime.dm_outbox import dm_outbox_loop
from nexus.runtime.group_decisions import group_decision_delivery_loop
from nexus.runtime.group_heartbeat import group_heartbeat_loop
from nexus.runtime.group_compute_telemetry import sampler_loop as pool_sampler_loop
from nexus.runtime.group_compute_telemetry_rollup import (
    rollup_loop as pool_rollup_loop,
)
from nexus.runtime.group_presence import presence_beacon_loop
from nexus.scheduler.dag import dag_scheduler_loop
from nexus.scheduler.fitness import worker_fit_score, worker_supports_task
from nexus.scheduler.manifest import clear_manifest_cache, read_task_manifest
from nexus.scheduler.retry import retry_scheduler_loop
from nexus.scheduler.selection import select_task_for_worker, select_top_n_workers


async def start_scheduler_loops() -> list:
    """Start DAG + retry + group-heartbeat + decision-delivery + presence loops."""
    import asyncio

    return [
        asyncio.create_task(dag_scheduler_loop(), name="nexus.scheduler.dag"),
        asyncio.create_task(retry_scheduler_loop(), name="nexus.scheduler.retry"),
        asyncio.create_task(
            group_heartbeat_loop(), name="nexus.runtime.group_heartbeat"
        ),
        asyncio.create_task(
            group_decision_delivery_loop(),
            name="nexus.runtime.group_decisions",
        ),
        asyncio.create_task(
            presence_beacon_loop(), name="nexus.runtime.group_presence"
        ),
        asyncio.create_task(
            dm_outbox_loop(), name="nexus.runtime.dm_outbox"
        ),
        asyncio.create_task(
            pool_sampler_loop(), name="nexus.runtime.pool_telemetry_sampler"
        ),
        asyncio.create_task(
            pool_rollup_loop(), name="nexus.runtime.pool_telemetry_rollup"
        ),
    ]


__all__ = [
    # fitness
    "worker_fit_score",
    "worker_supports_task",
    # selection
    "select_task_for_worker",
    "select_top_n_workers",
    # manifest
    "read_task_manifest",
    "clear_manifest_cache",
    # loops
    "dag_scheduler_loop",
    "retry_scheduler_loop",
    "start_scheduler_loops",
    # re-exports from runtime.capacity (documented as scheduler API)
    "local_capabilities",
    "task_required_caps",
    "image_allowed",
]
