# Architecture

NexusGrid is one Python package, `nexus`, organized into layers that avoid
importing "upward." A FastAPI app ties them together; a React UI sits on top of
the local API.

---

## Boot flow

```
python -m nexus
  └─ nexus.__main__:main()         parse CLI (nexus/cli.py)
       └─ nexus.app:create_app()   build the FastAPI app + lifespan
            ├─ lifespan startup:   apply staged restore → init_db → migrations →
            │                      load settings → identity → background loops
            │                      (scheduler, gossip, zombie sweeper, observability…)
            ├─ middleware:         CORS + BodySizeLimitMiddleware
            ├─ register_routers(): mount /local/*, /peer/*, etc.
            └─ mount_ui():         serve /app + the event bridge
       └─ uvicorn.run(app, host, port)
```

The app lifespan (`nexus/app.py`) is the single place background work and hooks
are wired. A staged backup restore is applied **before** `init_db` so a running
node is never overwritten in place.

---

## Module map

| Package | Responsibility |
|---|---|
| `nexus/api/` | HTTP routers. `local.py` (management API), `peer.py` (peer protocol), `groups.py`/`group_peer.py`, `events.py` (SSE), `websocket.py`, `relay_admin.py`, `diagnostics.py`, `pair_invites.py`, `network_cache.py`. |
| `nexus/core/` | Cross-cutting primitives: `config.py` (settings + normalization), `state.py` (`STATE` singleton), `identity.py` (node UUID, UUID↔IP map), `events.py` (in-process domain bus), `constants.py`, `paths.py`. |
| `nexus/security/` | Auth + crypto: `auth.py` (`verify_local_auth`, `verify_trusted_peer`), `group_grant.py` (Ed25519), `group_frame.py` (channel AEAD), `group_ecies.py` (X25519+ChaCha20Poly1305), `usage_receipt.py` (signed statements/receipts), `cred_crypto.py` (at-rest + transit wrap), `app_update.py` (signed update chain), `tokens.py`, `tls.py`, `limits.py`, `body_limit.py`, invite modules. |
| `nexus/networking/` | Transport: `peer_http.py` (cert-pinned peer HTTP), `relay_client.py`, `worker_client.py`, `tunnel.py`, `gossip.py` (beacon), `discovery.py` (UDP listener), `connection_manager.py`, `peer_protocol.py` (join/callback HMAC). |
| `nexus/runtime/` | Execution & features: `executor.py` (run a task), `replica_runner.py` (services/runners + build context), `service_*` (service host/tunnel/replication/grants), `db_engine.py`/`db_provider.py` (DBaaS), `foreign_storage_*` (deposits/recovery), `secrets_vault.py`, `backup.py`, `updater.py`, `webhooks.py`, `result_browser.py`, `storage_usage.py`, `plugin_files.py`/`plugin_packages.py`, `whats_new.py`, `native_sandbox.py`. |
| `nexus/scheduler/` | Task selection: `fitness.py`/`selection.py` (worker scoring), `dag.py` (DAG release/gate), `retry.py`, `reliability.py`, `benchmark.py`. |
| `nexus/tasks/` | Task lifecycle: `lifecycle.py` (`set_task_status` — publishes `task.status_changed`), `metadata.py`, `lease.py`, `step_targeting.py`, `workflow_resume.py`. |
| `nexus/storage/` | Persistence: SQLAlchemy `models.py` (+ `SCHEMA_VERSION`), `database.py` (`init_db` + additive `_migrate_schema`), `repositories.py`, `cloud/` (S3/GDrive/R2/B2 adapters). |
| `nexus/telemetry/` | Observability: `audit.py`/`audit_export.py`, `metrics.py`, `observability.py`, `alerts.py`, `presence.py`, `hardware.py`, `logs.py`. |
| `nexus/ui/` | Serve the SPA (`serve.py`), avatar, and the WebSocket/event broadcaster (`broadcaster.py`). |
| `nexus/caches/` | Worker venv/wheel/dependency caches + prewarm. |
| `nexus/sdk/` | The thin Python client + OpenAPI-driven CLI (`python -m nexus.sdk`). |
| `nexus/utils/` | Helpers: `net.py` (`client_host`, `is_private_or_loopback_host`), `text.py` (`safe_extractall`), `time.py`, `hashing.py`, `fs.py`. |

`webui/` is the React front end (esbuild). `tests/` holds the pytest suite;
`webui/test/` holds the JS tests (`node --test`).

---

## Two event buses (don't confuse them)

1. **`nexus.core.events`** — the **in-process domain bus**. Code publishes dotted
   events (`task.status_changed`, `scheduler.dag_released`, `storage.*`). Subscribers:
   the UI broadcaster, and the **webhook dispatcher** (`runtime/webhooks.py`).
2. **`nexus.runtime.event_bus`** — a tiny async fan-out feeding the **SSE endpoint**
   (`/local/events/stream`) so the browser re-fetches affected views live.

To make a new event reach the UI/webhooks, publish it on `nexus.core.events` and
subscribe where appropriate.

---

## Settings model

`LOCAL_SETTINGS` (a dict) is the node's config. It's persisted to the DB and loaded
through `normalize_local_settings` = `DEFAULT_LOCAL_SETTINGS` + saved overrides
(unknown keys dropped, out-of-range values clamped). Add a setting by:
1. adding a default to `DEFAULT_LOCAL_SETTINGS` in `core/config.py`,
2. (if structured) adding a `_normalize_*` function and calling it in `normalize_local_settings`,
3. surfacing it in the UI (`webui/src/screens/config.jsx`) and/or an endpoint.

---

## Data on disk (next to the node)
`.nexus_*` (identity/keys/token/secret), `nexus_mod_<port>.db` (SQLite),
`nexus_cache_<port>/` (caches + hosted deposit bytes), the four plugin dirs,
`nexus_packages/`, and `completed_tasks/` (artifacts). The `.gitignore` excludes
all runtime state.
