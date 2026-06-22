# storage — SQLite persistence

## What this owns

The SQLite schema, async SQLAlchemy engine, session factory, and repository
helpers that wrap common queries. The database file lives at
`{BASE_DIR}/nexus_mod_{port}.db` (path owned by `core.paths`).

Tables:

- `tasks` — every dispatched task with timeline, status, retry state.
- `peers` — trusted and pending peers with per-peer auth + signing keys.
- `local_config` — persisted key/value settings backing `LOCAL_SETTINGS`.
- `audit_events` — tamper-evident audit trail.
- `presence_events` — peer heartbeat history for the zombie sweeper.
- `foreign_storage_deposits` — per-deposit metadata; a row
  exists on both the host (`role='host'`) and depositor
  (`role='depositor'`) sides. The host row references on-disk
  ciphertext under `cache_dir/foreign_storage/<depositor>/<deposit>/`;
  the depositor row only carries metadata.
- `foreign_storage_db_grace` — encrypted blob held during
  the 2-day DB grace window after eviction.
- `cloud_credentials` — depositor-side encrypted cloud
  provider credentials (gdrive / s3 / r2 / b2). Encrypted with
  `nexus.security.cred_crypto.wrap_credential_blob` (AES-256-GCM
  keyed off `.nexus_secret`). The host never persists these.

Current `SCHEMA_VERSION` is **5** (bumped from 4 for the new
`cloud_credentials` table + four `cloud_*` columns on
`foreign_storage_deposits`).

The `nexus.storage.cloud` subpackage holds the `CloudProvider` ABC
(see `cloud/base.py`) plus per-provider drivers (`gdrive.py` real,
`s3.py` / `r2.py` / `b2.py` stubs). Drivers register themselves via a
`PROVIDERS` registry at import time. The host-side cloud upload
pipeline lives in `nexus/runtime/foreign_storage_workflow.py`.

## Public surface

Exports from `nexus.storage`:

- `init_db()` — create tables, run migrations
- `dispose()` — tear down the engine (used on shutdown + tests)
- `get_engine()` — access the SQLAlchemy async engine
- `get_session()` — async context manager yielding `AsyncSession`
- ORM models: `TaskRecord`, `Peer`, `LocalConfigRecord`, `AuditEvent`,
  `PresenceEvent`, `ForeignStorageDeposit`, `ForeignStorageDBGrace`;
  plus `Base` (declarative base) and `SCHEMA_VERSION`
- Repository helpers: `get_peer_by_ip`, `list_peers`,
  `load_local_settings_from_db`, `save_local_settings_to_db`,
  `persist_resolved_ip`, `seed_identity_mappings`

Audit + presence *events* are persisted by `nexus.telemetry.audit` and
`nexus.telemetry.presence`; this layer only owns the table definitions.

## Dependencies

- Imports from: `nexus.core`, `nexus.utils`.
- Imported by: `security`, `tasks`, `caches`, `telemetry`, `scheduler`,
  `networking`, `api`, `ui`.

Forbidden: `runtime`, `scheduler`, `networking`, `api`, `ui` (would create
cycles). If a higher layer needs custom queries, add a repository function here
instead.

## Extending

- **New table**: add the ORM model in `models.py`, add a `_migrate_vN` step in
  `database.py`, bump the migration version constant. Never modify an existing
  model definition without a migration.
- **New query helper**: put it in `repositories.py`, keep it async, and expose
  it via `__init__.py` so callers don't touch `models.py` directly.
- **Schema drift protection**: every ALTER TABLE must be reversible or the
  migration must be idempotent. The node ships on user machines — migrations
  run automatically on startup and must tolerate partial prior runs.

## Key files

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `models.py`       | SQLAlchemy ORM classes                               |
| `database.py`     | Engine, session factory, `init_db`, migrations       |
| `repositories.py` | Query helpers used from outside the package         |
| `cloud/` | `CloudProvider` ABC + per-provider drivers |
