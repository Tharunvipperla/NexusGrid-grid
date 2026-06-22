# Troubleshooting

Common issues and how to resolve them.

---

## The control panel won't open / "Invalid local API token"
- Open the **UI URL** the node printed on start (it injects the token for you).
  Don't hand-type a URL without the token.
- If you're trying to reach the panel from **another machine**, that's blocked by
  default — management is restricted to local/private-network clients. Use the
  machine the node runs on, or set `NEXUS_ALLOW_REMOTE_UI=1` (understand the risk
  first).
- The token lives in `.nexus_local_token` next to the node.

## A peer can't reach me / I can't reach a peer
- **Same LAN:** check both nodes are running and not firewalled on their port.
- **Across the internet / behind NAT:** you need a **relay**. Configure a relay URL
  and a shared **grid key** on both sides ([Local Config → Internet relay](screens/local-config.md#internet-relay)).
- Make sure you've actually **paired** (Network Web) or **joined a group** —
  discovery showing a node isn't the same as trusting it.

## My task won't run on a peer
- The peer must have **Accept network work** on and must **consent** the first time.
- The container **image must be on the peer's allowlist**
  ([Capability & security](screens/local-config.md#capability--security)).
- Check **Why queued** in the [task detail modal](screens/telemetry.md#the-task-detail-modal)
  — it explains exactly what the scheduler is waiting for.

## A container task fails immediately with "runtime unavailable"
- Docker/Podman isn't running on the target node. Start it, or pick a different
  **runtime** (`native` must be enabled on the target; `wasm` needs `wasmtime`).

## "Native runtime is disabled"
- The `native` (unsandboxed host) runtime is **off by default**. Enable it only on
  nodes where you trust the work ([Security Center](screens/security-center.md) /
  Local Config → Capability & security).

## I can't decrypt my foreign-storage deposit
- You need the **encryption password** you set when depositing — it never leaves
  your machine and isn't recoverable if lost. Check your **password hint**.

## Restore was refused ("newer version")
- The backup was made on a **newer** node version than this one. Update this node
  first, then restore. (Old → new always works; new → old does not.) See
  [Backup & restore](backup-and-restore.md).

## A webhook isn't firing
- Check **Recent deliveries** on [API & docs → Webhooks](screens/api-and-docs.md#webhooks)
  for the HTTP status. Confirm the event you expect is **ticked**, the URL is
  reachable from the node, and (if signed) your receiver verifies the
  `X-NexusGrid-Signature` correctly.

## Disk is filling up
- Open [Diagnostics → Storage usage](screens/diagnostics.md#storage-usage) to see
  the breakdown and delete artifacts, caches, or backup leftovers. Use **Clear
  database** in [Task Telemetry](screens/telemetry.md#clear-database) to drop task
  history.

## Where are the logs / state on disk?
Next to the node you'll find: `.nexus_*` (identity/keys/token — **keep these**),
`nexus_mod_<port>.db` (the database), `nexus_cache_<port>/` (caches + hosted
bytes), the plugin folders (`nexus_relays/`, `nexus_runners/`, `nexus_pumps/`,
`nexus_dbproviders/`), `nexus_packages/` (saved plugin packages), and
`completed_tasks/` (artifacts).

---

If something looks like a security issue, check the
[Diagnostics audit feed](screens/diagnostics.md#audit-feed) — it records consent
decisions, tripwires, and blocked requests.
