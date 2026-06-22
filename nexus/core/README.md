# core — foundation layer

## What this owns

Everything every other subpackage needs access to that is *not* purely stdlib:

- The node's **settings** (`LOCAL_SETTINGS` dict + schema).
- Hard-coded **constants** (`TASK_STATES`, `ALLOWED_TRANSITIONS`, defaults).
- The node's **identity** (UUID, display name, IP↔UUID resolution table).
- Path resolution that has to be PyInstaller-safe (`BASE_DIR`, `CACHE_DIR`).
- The **shared state registry** — the authoritative home for the long-lived
  in-memory structures that used to be top-level globals in
  `node_modified.py` (worker maps, presence tables, task queues).
- A lightweight in-process **event bus** so subpackages can emit and observe
  cross-cutting signals without importing each other directly.

added these settings keys (defaults in `config.py`):
`storage_bw_busy_mbps` (10), `storage_bw_idle_mbps` (100),
`storage_max_total_gb` (100), `storage_max_per_depositor_gb` (10),
`foreign_storage_host_terms` (operator override for the host T&C
copy). also added these `STATE` fields:
`service_rate_buckets`, `service_http_inspector`,
`service_replay_buffers`, `service_udp_listeners`,
`foreign_storage_pumps` + `foreign_storage_lock`. The throttle and
workflow-handler singletons attach to `STATE` at app start under the
attribute names `foreign_storage_throttle` and
`foreign_storage_workflow_handler`.

`core` does *not* own database access (see `storage`), network I/O (see
`networking`), or anything that runs subprocesses.

## Public surface

Exports from `nexus.core`:

- `get_settings() -> dict` — returns the live settings dict (mutable by
  authorized callers via `save_local_settings_to_db`)
- `LOCAL_SETTINGS` / `DEFAULT_LOCAL_SETTINGS` — the dict + its defaults
- `normalize_local_settings(raw)` / `normalize_bool(v)` / `normalize_list_field(v)`
- `BASE_DIR: pathlib.Path` — root for secret files, databases, caches
- `cache_dir(port) -> pathlib.Path` — per-port cache root (venv / pip / node)
- `get_resource_dir() -> pathlib.Path` — PyInstaller-safe resource root
- `secure_file_permissions(path)` — chmod 0o600 where supported
- `NODE_UUID: str` / `get_or_create_node_uuid()` / `get_node_identity()`
- `get_node_port()` / `set_node_port()`
- `register_peer_uuid(uuid, ip)` / `resolve_ip_to_uuid(ip)` / `resolve_uuid_to_ip(uuid)`
- `snapshot_mappings()` / `clear_mappings()` / `set_persist_hook(cb)`
- `fmt_peer(ip_or_uuid)` / `generate_random_display_name()`
- `STATE: SharedState` — the shared in-memory registry (class also re-exported)
- `events` — submodule exposing `subscribe`, `unsubscribe`, `publish`,
  `subscriber_count`, `clear_all`

Constants are re-exported from `nexus.core`: `TASK_STATES`, `TERMINAL_STATES`,
`ALLOWED_TRANSITIONS`, `DEFAULT_HTTP_PORT`, `DEFAULT_BIND_HOST`,
`DEFAULT_DISCOVERY_PORT`, `DEFAULT_GRID_KEY`, `MAX_LOG_LINES`,
`PEER_PRESENCE_TIMEOUT`.

## Dependencies

- Imports from: `nexus.utils`, stdlib.
- Imported by: every other nexus subpackage.

Forbidden: `storage`, `security`, and everything above them. If `core` needs the
database, wrap the access in an event so a higher layer can service it.

## Extending

- **Add a new setting**: put its default in `constants.py`, its validation in
  `config.py::normalize_local_settings`, and a one-line entry in this README.
- **Add a new shared state field**: extend `SharedState` in `state.py`. Always
  document the owner (which subpackage writes to it) inline.
- **Add an event**: declare it in `events.py` with a type-annotated payload
  shape. Subscribers should be decoupled — the event bus is *not* a replacement
  for direct function calls when the caller and callee are in the same layer.

## Key files

| File         | Purpose                                                        |
|--------------|----------------------------------------------------------------|
| `constants.py` | Task state machine, default port, default relay URL, etc.   |
| `config.py`  | `LOCAL_SETTINGS`, schema, normalize, load/save helpers         |
| `identity.py`| Node UUID lifecycle, IP↔UUID mapping                           |
| `paths.py`   | `BASE_DIR`, `CACHE_DIR`, PyInstaller-safe resource lookup      |
| `state.py`   | `SharedState` — in-memory registry of long-lived structures    |
| `events.py`  | In-process pub/sub bus                                         |
