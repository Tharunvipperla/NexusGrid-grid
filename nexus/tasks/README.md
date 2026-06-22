# tasks — task lifecycle, queue, lease, metadata

## What this owns

The *state* of a task from submission to completion. This is deliberately split
from `scheduler` (which *picks* a task for a worker) and `runtime` (which
*executes* a task). Keeping these three concerns separate means a developer
adding a new scheduler policy never reads execution code, and vice versa.

Responsibilities:

- **Lifecycle**: status transitions (respecting `ALLOWED_TRANSITIONS`), timeline
  events, retry state, preemption/disruption metadata.
- **Queue**: the in-memory `TASK_QUEUE` and its persisted view in `tasks` table.
- **Lease**: tracks which worker currently owns which task, with expiration.
- **Metadata**: `build_task_metadata()` builds the normalized task dict that is
  serialized to `/local/network` for the UI.

## Public surface

Exports from `nexus.tasks`:

- Lifecycle: `set_task_status(task, status, ...)`, `add_task_timeline_event`,
  `try_schedule_retry(task) -> bool`, `mark_task_interrupted`,
  `mark_task_preempted`, `is_task_interrupted`, `is_task_preempted`.
- Metadata: `build_task_metadata(task) -> dict`, `extract_task_metadata`,
  `parse_task_env`, `write_task_env`, `task_priority`, `task_created_at`,
  `task_retry_at`, `get_retry_policy` / `set_retry_policy`.
- Lease: `set_task_lease`, `refresh_task_lease`, `task_lease_expired`,
  `task_lease_owner`.
- Queue: `enqueue_task`, `dequeue_task`, `queue_depth`, `queue_empty`. The
  live queue itself lives in `nexus.core.STATE.task_queue` — access via these
  helpers rather than touching the list directly.
- Shadow: `upsert_remote_shadow_task(task_dict)` — mirror a peer's task into
  the local DB for UI visibility.

## Dependencies

- Imports from: `nexus.core`, `nexus.storage`, `nexus.utils`,
  `nexus.telemetry` (for audit + logs on transitions).
- Imported by: `scheduler`, `runtime`, `networking`, `api`.

Forbidden: `runtime`, `scheduler`, `networking`, `api`.

## Extending

- **New task status**: extend `core.constants.TASK_STATES` and the transition
  matrix. Add audit coverage in the transition helper.
- **New retry strategy**: add it to `metadata.py::RetryPolicy` and surface it
  through `get_retry_policy`/`set_retry_policy`.
- **New lease type**: compose inside `lease.py`; don't add ownership fields to
  `TaskRecord` without a `storage` migration.

## Key files

| File          | Purpose                                         |
|---------------|-------------------------------------------------|
| `lifecycle.py`| Status transitions, timeline, preemption        |
| `queue.py`    | `TASK_QUEUE` operations                         |
| `lease.py`    | Worker lease creation / expiration              |
| `metadata.py` | `build_task_metadata`, retry policy, task env   |
| `shadow.py`   | `upsert_remote_shadow_task` (mirror a peer task)|
| `step_targeting.py` | Per-step targeting/override resolution for DAG workflows |
| `workflow_resume.py`| One-click DAG-aware resume (re-queue failed steps)|
