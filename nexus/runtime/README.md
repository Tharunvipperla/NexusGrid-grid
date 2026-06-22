# nexus/runtime

The **execution and feature layer** — everything the node *does* once the API and
scheduler have decided to do it: run tasks and services, host foreign storage,
operate relays, coordinate groups, and power node features (backup, updates,
secrets, webhooks…). It's the largest package in the node.

Routers in [`nexus/api`](../api/README.md) and loops in
[`nexus/scheduler`](../scheduler/README.md) call into here; this layer talks to
[`storage`](../storage/README.md), [`networking`](../networking/README.md), and
[`security`](../security/README.md). See the
[developer architecture guide](../../docs/dev/architecture.md) for the big picture.

> When you add a module here, add a one-line entry below so this stays a true map
> of the package.

---

## Task & service execution
| Module | Purpose |
|---|---|
| `executor.py` | Top-level execution dispatcher — runs a task bundle (docker / native / wasm) under a watchdog. |
| `replica_runner.py` | Replication auto-run; custom **build context** (Dockerfile FROM-allowlist + size cap + fingerprint cache) and pluggable runners. |
| `service_runner.py` | Long-running service-task runtime. |
| `service_replication.py` | Service replication primitives (snapshot capture/extract, standby promotion). |
| `service_kinds.py` | Connection-string templates for service tasks. |
| `service_tunnel.py` | Service data-plane tunnel (Phase B) + pump hook. |
| `service_grants.py` | Service-access grant lifecycle: request → approve/deny → revoke; DBaaS provisioning. |
| `native_sandbox.py` | Cross-platform sandbox primitives (bubblewrap etc.) for the native runtime. |
| `docker_client.py` | Lazy Docker SDK client + per-profile container security options. |
| `capacity.py` | Dispatch-capacity + **allowed-image gate** + required-capability helpers. |
| `worker_state.py` | Per-node local worker bookkeeping. |
| `workspace.py` | Per-task workspace dir + P2P cache resolution. |
| `child_job.py` | App-wide kill-on-close Job Object so no child outlives the node. |
| `process_tree.py` | Cross-platform recursive process kill (with a re-scan pass). |
| `idle_detect.py` | Cross-platform idle-input detection (for idle auto-accept). |

## DBaaS
| Module | Purpose |
|---|---|
| `db_engine.py` | One-click local database-engine bring-up (postgres/mysql/redis/mongo). |
| `db_provider.py` | Pluggable DB-provider adapters; per-consumer DB + login on an approved grant. |

## Foreign storage
| Module | Purpose |
|---|---|
| `foreign_storage_workflow.py` | The storage-frame handler (offer/accept/chunk/retrieve/delete/view-grant). |
| `foreign_storage_keys.py` | Depositor-side session-key cache. |
| `foreign_storage_quota.py` | Quota math. |
| `foreign_storage_cloud.py` | Depositor-side cloud-eviction core. |
| `foreign_storage_rclone.py` | rclone cloud-overflow for auto-rescue. |
| `foreign_storage_tripwire.py` | Detects out-of-workflow changes to a host's stored data. |
| `preview_pump.py` | Depositor-side per-chunk plaintext cache + in-flight fetches. |

## Relays
| Module | Purpose |
|---|---|
| `local_relay.py` | In-process local relay server + relay-code import/export. |
| `relay_state.py` | Relay binding state machine. |
| `relay_tunnel.py` | Auto-tunnel: make a local relay publicly reachable. |
| `relay_sandbox.py` | Sandboxed (out-of-process) execution of a foreign relay module. |
| `relay_selfheal.py` | Relay self-healing. |
| `relay_pause.py` | Pause/resume the local relay with a delayed-kill grace window. |
| `relay_latency.py` | Relay-latency cache + periodic probe loop. |
| `relay_codeprint.py` | Code fingerprint for the bundled relay implementation. |
| `relay_telemetry.py` / `relay_telemetry_rollup.py` | Relay frame counters + daily rollup. |

## Groups (compute & coordination)
| Module | Purpose |
|---|---|
| `group_compute.py` | Group-scoped compute: build the eligible-worker pool. |
| `group_compute_telemetry.py` / `group_compute_telemetry_rollup.py` | Time-bucketed pool-usage telemetry + retention sweep. |
| `group_inbox.py` | Replicated pending-join-request inbox. |
| `group_decisions.py` | Admin-side delivery of join decisions to joiners. |
| `group_heartbeat.py` | Admin-side grant heartbeat + TTL pruning. |
| `group_presence.py` | Member liveness presence beacons. |
| `pair_handshake.py` | Issuer-side pending pair-invite request tracking. |

## Messaging
| Module | Purpose |
|---|---|
| `chat_attachments.py` | Sender-hosted large (>5 MB) chat/DM attachments. |
| `dm_outbox.py` | Offline DM outbox (retry delivery). |

## Plugins
| Module | Purpose |
|---|---|
| `plugin_files.py` | In-app editor backend for the four drop-in plugin kinds (safe CRUD + syntax check, never executes). |
| `plugin_packages.py` | Share & install plugin packages (validate/install, never executes). |

## Node features & ops
| Module | Purpose |
|---|---|
| `backup.py` | Node backup export + staged restore (normal / full; version-guarded). |
| `updater.py` | Central, **signed** auto-update (verifies the chain + binary hash). |
| `secrets_vault.py` | Node-local secrets vault (`secret://NAME`, AES-256-GCM at rest). |
| `webhooks.py` | Outbound webhooks / event subscriptions (signed deliveries). |
| `result_browser.py` | Result/artifact browser (filesystem-backed, traversal-safe). |
| `storage_usage.py` | On-disk usage breakdown for Diagnostics. |
| `usage_receipts.py` | Counterparty-signed usage-receipt application/issuance/distribution. |
| `whats_new.py` | In-app "What's new" changelog source. |
| `cloud_connector.py` | Cloud connector: fetch task inputs / push results (SSRF-guarded). |
| `event_bus.py` | Tiny async fan-out feeding the SSE endpoint (UI live updates). |
