# scheduler — worker selection, retry, DAG resolution

## What this owns

The decision logic that answers: given a queue of tasks and a set of workers,
which task goes to which worker, when do we retry, and in what order do we
resolve dependencies?

- **fitness.py** — `worker_fit_score(task, worker)` returns a numeric score
  taking CPU/RAM headroom, GPU availability, prior success rate, and latency
  class (LAN vs relay) into account. Also owns
  `worker_supports_task(worker, task)`.
- **selection.py** — the actual dequeue loop: finds the best (task, worker)
  pair from the current queue (`select_task_for_worker`).
- **manifest.py** — on-disk task manifest cache (`read_task_manifest`,
  `clear_manifest_cache`) so the DAG + selection loops don't re-parse on
  every pass.
- **retry.py** — retry policy evaluation + the background
  `retry_scheduler_loop` that re-enqueues tasks after backoff.
- **dag.py** — resolves workflow task graphs into execution order, runs
  `dag_scheduler_loop`. The 2-second tick also runs four follow-up
  passes: `service_health_pass` (failover + traffic
  switch), `service_image_refresh_pass` (push
  `service_image_refresh` to standbys every 60 s),
  `foreign_storage_lifecycle_pass` (eviction → DB grace
  → purge state machine for foreign-storage deposits), and
  `foreign_storage_key_gc_pass` (drop session keys idle
  past the 30-min TTL, zeroizing the underlying bytearray and
  wiping any cached preview plaintext for that deposit).
  `foreign_storage_lifecycle_pass` additionally runs
  `_foreign_storage_auto_rescue_pass` — depositor-side salvage of our
  own at-risk deposits (host evicting, or TTL within N days when the
  trigger is `days`). Each at-risk row is acted on once (tracked in
  `STATE.foreign_storage_auto_rescue_seen`): if `fs_auto_rescue_cloud_cred`
  is set it asks the host to stream the ciphertext to our bucket
  (no password needed); otherwise it pulls the bytes to
  `fs_auto_rescue_dir` — decrypting straight to the file if the deposit
  is already unlocked, else saving the *ciphertext* (`storage_pump`'s
  `rescued_deposit_dir`) and flipping the row to `rescued_encrypted` so
  the user can decrypt later via `POST /foreign_storage/decrypt_rescued/{id}`
  with the password (verified by unsealing the manifest). If local disk is
  full and `fs_auto_rescue_rclone_targets` are configured, the ciphertext is
  streamed straight into `rclone rcat` (never staged locally — see
  `nexus.runtime.foreign_storage_rclone`), trying each target in order; the
  workflow handler feeds chunks to a per-deposit queue on
  `STATE.foreign_storage_stream_queues`. The host being offline (retry next
  tick), or disk full with no rclone, audits `storage.auto_rescue_failed`
  so the bell can warn that files may be lost. Gated by the `fs_auto_rescue`
  setting (default on), with per-deposit overrides
  (`fs_auto_rescue_overrides[deposit_id]` → enable/disable + dir + rclone
  targets) resolved through `config.effective_auto_rescue`; edited from the
  Auto-rescue button on each "My deposits" row
  (`POST /foreign_storage/auto_rescue_config/{id}`).

Capability descriptors (`local_capabilities`, `task_required_caps`,
`image_allowed`) live in `nexus.runtime.capacity` because they interrogate
real runtime state (Docker availability, GPU presence). The scheduler just
consumes those results.

Scheduling is *pure* decision logic. It does not execute tasks (that is
`runtime`) or touch the network (that is `networking`).

## Public surface

Exports from `nexus.scheduler`:

- `select_task_for_worker(worker_ip, worker_caps)` — returns the best
  queued task for that worker.
- `worker_fit_score(task, worker)` / `worker_supports_task(worker, task)`.
- `read_task_manifest(task)` / `clear_manifest_cache()`.
- `dag_scheduler_loop()` — background DAG resolver.
- `retry_scheduler_loop()` — background retry dispatcher.
- `start_scheduler_loops()` — launches DAG + retry loops (called from
  `nexus.app.create_app`).

## Dependencies

- Imports from: `nexus.core`, `nexus.storage`, `nexus.tasks`, `nexus.telemetry`,
  `nexus.runtime` (capacity queries only — no direct execution).
- Imported by: `networking.worker_client`, `api`.

Forbidden: `networking`, `api`, `ui` (reverse deps would form a cycle).

## Extending

- **New scheduling policy**: add a strategy class in `selection.py` and a
  setting (`scheduler_strategy`) in `core.config` to pick it. Existing code
  continues to work as the default.
- **New retry strategy**: add it in `retry.py`, expose via `tasks.RetryPolicy`.
- **New fitness dimension**: extend `local_capabilities` /
  `task_required_caps` in `nexus.runtime.capacity`, then update
  `worker_fit_score` here. Keep the function pure and deterministic.

## Key files

| File           | Purpose                                                |
|----------------|--------------------------------------------------------|
| `fitness.py`   | `worker_fit_score` + `worker_supports_task`            |
| `selection.py` | Dequeue / match loop (`select_task_for_worker`)        |
| `manifest.py`  | On-disk manifest cache                                 |
| `retry.py`     | Retry policy + background retry loop                   |
| `dag.py`       | DAG resolver + DAG scheduler loop                      |
| `reliability.py`| Per-worker finished/fail tally → reliability-aware ranking |
| `benchmark.py` | Self-benchmark (CPU/IO) feeding the fitness score      |
