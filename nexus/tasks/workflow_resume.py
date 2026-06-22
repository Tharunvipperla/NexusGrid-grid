"""C2 — one-click DAG-aware resume.

After a workflow stalls (a step exhausted retries → ``failed``, leaving
downstream steps stuck ``waiting``), resuming it by hand means hunting each
failed step and re-queuing it. :func:`resume_workflow` does it in one call for
all tasks sharing a ``parent_id``:

* a failed/disrupted/cancelled step whose deps are all ``completed`` (or has
  none) is **re-queued**;
* one whose deps aren't satisfied yet is **re-armed** to ``waiting`` so the DAG
  scheduler releases it once its upstreams finish;
* ``completed`` / in-flight steps are left alone (idempotent).

The decision is a pure function (:func:`plan_workflow_resume`) for easy testing;
:func:`resume_workflow` applies it against the DB.
"""

from __future__ import annotations

_FAILED = {"failed", "disrupted", "cancelled"}


def plan_workflow_resume(tasks: list[dict]) -> dict:
    """Given ``[{id, status, depends_on:[ids]}]`` return ``{requeue, rearm}``
    lists of task ids. Pure — no DB."""
    status = {t["id"]: (t.get("status") or "").lower() for t in tasks}
    requeue: list[str] = []
    rearm: list[str] = []
    for t in tasks:
        if (t.get("status") or "").lower() not in _FAILED:
            continue
        deps = t.get("depends_on") or []
        if all(status.get(d) == "completed" for d in deps):
            requeue.append(t["id"])
        else:
            rearm.append(t["id"])
    return {"requeue": requeue, "rearm": rearm}


async def resume_workflow(workflow_id: str) -> dict:
    """Resume all stalled steps of *workflow_id*. Returns a summary dict."""
    from sqlalchemy import select

    from nexus.storage import TaskRecord, get_session
    from nexus.tasks.lifecycle import set_task_status
    from nexus.tasks.queue import enqueue_task

    requeued: list[str] = []
    rearmed: list[str] = []
    async with get_session() as db:
        rows = (
            await db.execute(
                select(TaskRecord).filter(TaskRecord.parent_id == workflow_id)
            )
        ).scalars().all()
        if not rows:
            return {"found": 0, "requeued": [], "rearmed": []}

        tasks = [
            {
                "id": r.id,
                "status": (r.status or "").lower(),
                "depends_on": [d.strip() for d in (r.depends_on or "").split(",") if d.strip()],
            }
            for r in rows
        ]
        plan = plan_workflow_resume(tasks)
        by_id = {r.id: r for r in rows}
        for tid in plan["requeue"]:
            r = by_id[tid]
            if set_task_status(r, "queued", "Workflow resume.", force=True):
                r.worker = None
                requeued.append(tid)
        for tid in plan["rearm"]:
            r = by_id[tid]
            if set_task_status(r, "waiting", "Workflow resume — re-armed.", force=True):
                rearmed.append(tid)
        await db.commit()

    for tid in requeued:
        await enqueue_task(tid)
    return {"found": len(rows), "requeued": requeued, "rearmed": rearmed}


__all__ = ["plan_workflow_resume", "resume_workflow"]
