# Changelog

User-facing release notes. The in-app **What's new** panel (notification bell)
reads this file via `/local/whats_new`. Newest version first; one `## [version] - date`
header per release, then `-` bullets for the highlights people actually notice.

## [1.0.0] - 2026-06-21
First public release. NexusGrid is a peer-to-peer compute and storage grid — pool
compute and storage across machines you and people you trust own, with no central
server and no account.

- **Run tasks & workflows** — dispatch single tasks or multi-step DAG workflows to
  your own node, a trusted peer, or a group; sandboxed runtimes (Docker / Podman /
  WASM, plus an opt-in native runtime), an image allowlist, custom build contexts,
  and a visual DAG editor with per-step targeting and "verify each step" gating.
- **Host services & databases** — run long-running services on the grid with
  access grants (free / permission), and one-click managed databases
  (Postgres, MySQL, Redis, MongoDB) provisioned per consumer.
- **Foreign storage** — store data on peers' disks, always encrypted at rest with a
  key only you hold; consent-gated hosting, view-grant sharing, and automatic
  encrypted recovery with optional cloud overflow.
- **Connect privately** — pair 1:1 or form groups; direct messages and group chat
  are end-to-end encrypted and sender-authenticated; relays bridge peers across
  NAT without ever seeing your plaintext.
- **Extend & integrate** — drop-in plugins (relays, service pumps, sandbox runners,
  DB providers) editable in-app and shareable as packages; a full local REST API
  with OpenAPI, an SDK/CLI, and outbound webhooks.
- **Operate with confidence** — live topology and telemetry, a result/artifact
  browser, an audit log, storage-usage breakdown, node backup/restore, and signed
  auto-updates.
