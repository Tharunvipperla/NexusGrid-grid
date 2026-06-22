"""Shadow task records for cross-peer coordination.

Extracted from Phase-1/node_modified.py (``upsert_remote_shadow_task`` at
lines 2487-2525).

When this node serves a task on behalf of a remote master we keep a local
``TaskRecord`` row with id ``remote__<master_ip>__<remote_task_id>``. The
row carries the log output and final status so the serving side has a
durable trail of every master it worked for. Shadow rows have an empty
payload and the coordination role ``"serving"``.

The function is split into its own module (rather than co-located with
:mod:`nexus.tasks.metadata`) to keep that module a pure serialization
surface. ``upsert_remote_shadow_task`` does IO.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from nexus.storage import TaskRecord, get_session
from nexus.tasks.metadata import build_task_metadata


async def upsert_remote_shadow_task(
    master_ip: str,
    remote_task_id: str,
    status: str,
    logs: str,
    worker_id: str | None = None,
) -> None:
    """Create or update the shadow row for a task served for *master_ip*."""
    shadow_id = f"remote__{master_ip.replace(':', '_')}__{remote_task_id}"
    shadow_env = build_task_metadata(
        {},
        coordination_role="serving",
        requested_by=master_ip,
        display_id=remote_task_id,
    )
    async with get_session() as db:
        task = (
            await db.execute(select(TaskRecord).filter(TaskRecord.id == shadow_id))
        ).scalar_one_or_none()
        if not task:
            db.add(
                TaskRecord(
                    id=shadow_id,
                    parent_id=master_ip,
                    status=status,
                    depends_on="",
                    env_vars=json.dumps(shadow_env),
                    worker=worker_id,
                    logs=logs,
                    payload=b"",
                )
            )
        else:
            task.status = status
            task.worker = worker_id
            task.logs = logs
            task.env_vars = json.dumps(shadow_env)
        await db.commit()


__all__ = ["upsert_remote_shadow_task"]
