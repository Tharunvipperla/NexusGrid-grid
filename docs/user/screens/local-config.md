# Local Config

**Sidebar → My node → Local Config.** Every node setting lives here. **Changes save
automatically.** These are your node's *defaults* — individual dispatches and
deposits can override the scheduler/transfer values per job.

The screen is organized into cards:

---

## Node identity
- **Display name** — how peers see you.
- **About me** — a short blurb on your node profile.

## Node status — Accept network work
The master switch for whether your node accepts work from the grid. Turn it off to
go "compute-private" (you can still dispatch, store, and chat).

## User control / Master control
- **User control** — a watchdog mode where *you keep priority* on your own machine
  (your interactive use isn't crowded out by grid work).
- **Master control** — coordinator behavior:
  - **Isolate to one** — serve a single coordinator at a time, or
  - serve multiple coordinators while resources allow.

## Resources & sharing
How much of your machine the grid may use — CPU/RAM caps, GPU sharing, etc. —
plus **Share by capacity** (offer resources proportional to what's free).

## Capability & security
- **Allowed images** — the container-image allowlist. Tasks/services may only run
  images on this list. Enter one per line (a bare repo like `python` allows any
  tag; `python:3.11-slim` allows exactly that). **This is a key safety control.**
- Sandbox **security profile**, **native runtime** opt-in, **worker consent**,
  **task network access**, **task code scanning**, **idle auto-accept**, **IP
  privacy** — the controls summarized on the [Security Center](security-center.md).

## Scheduler & safety
Node-wide scheduling defaults — **prefer reliable workers**, retry/lease/backoff
defaults, step-gate default, queue timeouts — each overridable per dispatch in the
[Dispatcher](dispatcher.md).

## Auto-clean / Keep cached
- **Auto-clean** — delete datasets after processing.
- **Keep cached** — retain files on local disk for reuse.

## Internet relay
Reach peers outside your LAN via a relay:
- **Relay URL** to connect through.
- **Grid key** — the shared secret that gates which nodes share a relay (optional;
  blank = open join). See [relays](../concepts.md#relays).

## Cloud integration
Configure the **cloud connector** data plane (e.g. Drive-backed workspaces, rclone
remotes) used by task inputs and foreign-storage overflow.

## Foreign storage pledge / Share by capacity
How much disk you **pledge** to host for peers, and whether to offer it by free
capacity.

## Auto-recovery
Node-wide defaults for automatically salvaging data you've stored on peers before
a host drops it (rescue folder, cloud overflow, when to act). Each deposit can
override these on the [Foreign Storage](foreign-storage.md) screen.

## Secrets
A small **encrypted vault** for task/service env secrets. Add a secret
(`NAME` in `UPPER_SNAKE_CASE`, a write-only **value**, optional description) and
reference it as `secret://NAME` in a task or service's env — the value is injected
at run time and never stored in the spec. Values are **encrypted at rest** and
never shown again after saving.

## Backup & restore
Export your node, or restore one by uploading it. There are **two kinds**:
- **Backup** — your identity + database (settings, templates, secrets vault,
  deposit records, DBaaS defs). Everything DB/settings-backed.
- **Full backup** — the above **plus** on-disk data the DB only references: plugin
  folders, completed-task artifacts, and hosted deposit bytes.

**One upload restores either kind** — it auto-detects. A restore is staged and
applied on the next start (your running node is never overwritten in place; the
old DB is kept as `.pre_restore`). A backup from a *newer* node version is refused
(update first). Full details in [Backup & restore](../backup-and-restore.md).
