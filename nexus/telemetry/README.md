# telemetry — logs, metrics, alerts, audit, presence, hardware

## What this owns

Everything a node knows about itself and its peers that is NOT task state:

- **logs.py** — `LogStream` (per-task rolling in-memory buffer), `task_log_append`
  / `task_log_tail`, IP-masked rendering for UI.
- **metrics.py** — the `METRICS` dict + the `observability_loop` that snapshots
  cpu/ram/queue depth / worker counts at a fixed cadence.
- **alerts.py** — threshold evaluation + the `ALERTS` ring buffer. Alerts are
  emitted over the event bus for UI broadcast.
- **audit.py** — tamper-evident audit log with structured event types (task
  dispatched, peer joined, settings changed, etc.).
- **presence.py** — peer heartbeat table + `zombie_sweeper` that evicts stale
  peers after `presence_timeout_s`.
- **hardware.py** — CPU/RAM/GPU sampling + network bandwidth estimation. Uses
  `psutil`; GPU detection probes `nvidia-smi` / `rocm-smi` once at startup.

## Public surface

Exports from `nexus.telemetry`:

- `LogStream`, `task_log_append(task_id, line)`, `task_log_tail(task_id, n)`,
  `clear_local_task_log(task_id)`.
- `incr_metric(name, delta=1)`, `get_metric(name)`, `snapshot_metrics()`,
  `KNOWN_METRICS`.
- `push_alert(level, code, detail)`, `snapshot_alerts()`.
- `record_audit_event(kind, payload)`, `write_audit_event(...)`.
- `presence` module: `mark_peer_online`, `mark_peer_offline`, `is_peer_offline`.
- `detect_gpu()`, `get_gpu_stats()`, `gpu_vendor()`, `sample_net_bandwidth()`.
- `compute_cluster_rollup()`, `analyze_cluster_health()`, `LONG_RUN_WARN_SEC`.
- Background loops (launched from `nexus.app.create_app`):
  `observability_loop`, `zombie_sweeper`.

## Dependencies

- Imports from: `nexus.core`, `nexus.storage`, `nexus.utils`.
- Imported by: `tasks`, `runtime`, `scheduler`, `networking`, `api`, `ui`.

Forbidden: `security`, `runtime`, `scheduler`, `networking`, `api`, `ui`. This
is a leaf-ish layer that anyone can call.

## Extending

- **New metric**: add a key in `metrics.py::METRICS`, update the observability
  loop snapshot, and document the expected range in this README.
- **New alert**: define the code + default threshold in `alerts.py`.
- **New audit event kind**: add a constant in `audit.py`, document the payload
  shape. Audit payloads are append-only; never mutate past events.

## Key files

| File                | Purpose                                                |
|---------------------|--------------------------------------------------------|
| `logs.py`           | Per-task rolling log buffers                           |
| `metrics.py`        | `METRICS` dict + snapshot helpers                      |
| `alerts.py`         | `ALERTS` ring buffer + thresholds                      |
| `audit.py`          | Audit event sink (append-only, DB-persisted)           |
| `presence.py`       | Peer presence table (online/offline)                   |
| `hardware.py`       | CPU/RAM/GPU + bandwidth sampling                       |
| `rollup.py`         | Cluster rollup + health analysis                       |
| `observability.py`  | Periodic queue / consent / retention loop              |
| `zombie_sweeper.py` | Dead-worker reap + lease-expired sweep                 |
| `audit_export.py`   | Audit-log export (CSV/JSON) for the Diagnostics feed   |
