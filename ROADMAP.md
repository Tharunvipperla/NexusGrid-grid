# NexusGrid Roadmap

*A living document. It describes direction and priorities, not commitments or
dates.* Anything here can change as we learn — if something matters to you,
[open an issue](https://github.com/Tharunvipperla/NexusGrid-grid/issues) or start
a discussion and help us weigh it.

> **How to read this.** The roadmap is organized into **horizons**, not calendar
> quarters. Horizon 1 is what we're actively working toward; later horizons get
> progressively less certain. A **Non-goals / deferred** section at the end is
> just as important as the plan — it records the things we've consciously decided
> *not* to chase yet, and why.

---

## Guiding principles

Every item below is judged against these. When a feature fights a principle, the
principle usually wins.

- **No central server, no account, no middleman.** The grid is peers who trust
  each other. Any global feature must work *without* a mandatory central
  authority; at most it may use an **optional** aggregator that nodes can ignore
  and still be fully functional.
- **Zero-config by default.** Identity, discovery, encryption, and updates should
  "just work" on first run. New power should not cost new setup.
- **Low footprint.** One self-contained binary, no runtime dependencies for end
  users, a lean UI. We add capability without bloating the baseline.
- **Local-first and end-to-end encrypted.** Your data and keys stay yours. We
  design for the case where the network is hostile and peers are semi-trusted.
- **Honest about security.** We document what we *cannot* protect against (e.g. a
  host with root over their own machine) instead of pretending. Mitigations ship
  when they're real, not as theater.
- **Earned status, never pay-to-win.** If we add reputation or rewards, they
  reflect *verified* contribution. Capability stays equal for everyone; the core
  grid is never gated behind payment.

---

## Where we are today

NexusGrid is already a deep peer-to-peer compute **and** storage grid. Shipped
and stable:

- **Compute** — dispatch tasks and multi-step DAG workflows to worker nodes with
  sandboxing, leases, retries, fitness-scored worker selection, per-step
  targeting, anti-affinity, DAG templates, an interactive graph editor, per-level
  approval gates, and resume-from-failed-step.
- **Reliability-aware scheduling** — workers are ranked partly by their verified
  finished-to-failed history, with a per-dispatch override.
- **Idle-aware participation** — opt-in: a node only accepts new work once the
  machine has been idle (no user input) for a configurable interval — cross-platform,
  and without overriding the user's explicit online/offline choice.
- **Services & databases** — long-running services over a generic
  TCP-over-WebSocket tunnel (connect to `localhost:<port>` as if it were local),
  with **per-service access control** — each service is owned by the node that
  provisioned it and gated independently, and access-requests are asynchronous (they
  work before two peers have ever connected directly) — plus one-click managed
  database engines and per-grant on-demand provisioning (DBaaS).
- **Foreign storage** — deposit end-to-end-encrypted data on peers' spare disk
  (Argon2id + AEAD), with an auto-recovery suite (encrypted local download,
  decrypt-later, cloud overflow via rclone).
- **Private connectivity & group governance** — groups with Ed25519 identity,
  editable role/permission bags (founder / admin / member + custom roles), signed
  short-TTL membership grants verified by challenge-response (a stolen grant blob
  alone is useless), private messaging, and member-run relays that punch through NAT.
- **Extensibility** — drop-in plugins (relays, runners, pumps, DB providers) with
  an in-app editor and one-click shareable plugin packages; a local REST API +
  OpenAPI; a thin Python SDK + CLI; outbound webhooks.
- **Operability** — live telemetry, a result/artifact browser, live log tailing,
  storage-usage insight, secrets vault, audit log + export, encrypted
  backup/restore (with forward-compatible migrations), an in-app changelog, and
  cryptographically **signed auto-updates**.

The capability spine is largely built. The roadmap below is about **hardening it,
making it approachable, and extending it** — not rebuilding it.

---

## Horizon 1 — Launch readiness (active)

The goal here is a confident, well-documented first public release. Features take
a back seat to trust and clarity.

### Rigorous testing pass
- **Functional hardening** — exercise every surface end to end (groups, relays,
  services, DBaaS, DAG, foreign storage, backup/restore, updates) and fix what
  breaks. Every fix lands with a committed regression test, never a throwaway.
- **Security review** — a focused adversarial pass over the peer protocol, auth,
  tunnels, sandboxing, and the threat scanner, with findings logged and closed
  before release.

### Documentation & onboarding gate (done last, once)
A single comprehensive pass so nothing is documented twice as features settle:
- **First-run "Get started" wizard** — walks identity → connectivity → first
  peer/group → first task, and acts as the front door to the full docs.
- **Complete user documentation** — every screen, button, and control explained
  (what it is, what it does, when to use it), paired with short demo screencasts.
- **Complete developer documentation** — architecture, the plugin directories,
  the local REST API / OpenAPI / SDK, and how to extend each layer.

### Distribution & front door
- **Official website** — what it is, downloads, screenshots, links to docs;
  stable download links and a real landing page.
- **Versioned documentation site** with a per-release changelog tied to release
  tags.
- **Signed, trusted installers** — work toward code-signing/notarization so
  first-run friction (SmartScreen / Gatekeeper warnings) goes away.

---

## Horizon 2 — Depth & developer experience

Make the grid more powerful for the people already using it, and easier to build
on.

- **GPU passthrough for tasks & services** *(next up)* — forward the host GPU into
  task and service containers (opt-in per node) so self-hosted LLMs, training, and
  other accelerated workloads run on hardware instead of CPU. We provide the
  framework; you bring the image and the model.
- **IDE extensions (VS Code + JetBrains)** — submit tasks/DAGs, browse results,
  and manage services without leaving the editor, built as thin clients over the
  existing local API and generated SDK.
- **`nexus run` ergonomics** — package the current directory, dispatch it to the
  best worker, and stream stdout/stderr + exit code back as if it ran locally.
- **True streaming logs** — move live log tailing from polling to server-sent
  events, and extend tailing to relay logs.
- **Cloud connector fast-follows** — reuse the existing connector for
  foreign-storage sources, DAG step inputs/outputs, and result upload, so
  "everything must be local/peer" friction disappears.
- **DBaaS depth** — more managed engines beyond the shipped four
  (postgres / mysql / redis / mongo) and a smoother "I have data" → "it's queryable
  from my laptop" provisioning flow.
- **Built-in benchmarking** — standard benchmark tasks (compute, I/O, network) to
  establish baseline worker scores that feed scheduling decisions.

---

## Horizon 3 — Service governance depth & durability

The "worker-as-a-service" spine — editable roles, signed short-TTL grants, and
per-service ACLs — has **already shipped** (see *Where we are today*). This horizon
hardens what surrounds it: durable group state that survives nodes going offline,
more flexible data hosting, and making hosted data tougher to lose.

### Group & service governance — remaining pieces
- **Replicated group state + multi-relay durability** — group roster, roles, and
  service registry replicate across members as a signed append-only log, and a
  group can run several relays at once so it survives any single node (even the
  founder) going offline. Admins get a low-relay alarm.
- **File-mode DB hosting** — beyond the shipped engine mode, host a database as a
  bare file (SQLite / Parquet / DuckDB / CSV via an adapter layer); the system
  shouldn't care which.
- **User-extensible service kinds** — add new service templates via a config file
  or a UI form, not by editing source.

### Distributed data access
- **Remote data viewer** — dispatch a huge file (Excel/CSV/SQLite) to a capable
  worker and page through it row-by-row from a laptop, with server-side
  search/sort.
- **Remote SQL interface** — a query editor that runs against a worker-hosted
  database and streams paginated results back, sandboxed to the database
  container.

### Durability for foreign storage
- **DB-grace recovery window** — a real grace period after eviction during which a
  depositor can still recover their data.
- **Multi-host striping** — overflow a deposit too big for any single host by
  splitting chunks across peers (with the failure modes designed for, not
  hand-waved).
- **Redundancy / erasure coding** — survive host loss, not just overflow it (a
  harder follow-up to striping).
- **Host accountability** — a clear, enforced policy and tripwire for hosts that
  delete deposited data outside the sanctioned eviction flow.

---

## Horizon 4 — Engagement & identity ("skins, not stronger guns")

Deferred behind the capability and developer-experience core. The model:
capability stays equal for everyone; we layer on **status, identity, and
collectibles people earn by genuinely contributing** — like cosmetics, never
power.

- **Contribution ranks/tiers** from *verified, consumed* contribution
  (compute served, storage hosted, uptime, tasks completed).
- **Badges / achievements** off existing events ("first relay hosted", "helped 50
  peers", streaks).
- **Cosmetic node skins / identicon themes / frames** unlocked at higher tiers.
- **Collectible "node cards"** — a shareable, node-signed card of a node's specs,
  tier, and top badges. Local and portable — **never sellable, not an NFT.**
- **Leaderboards** (global, per-group, seasonal) and **streaks/quests** that also
  teach features.

**Guardrails:** the cosmetic ledger stays *separate* from the scheduler's
reliability signal — nothing buyable or cosmetic may ever leak into scheduling
fitness — rewards track counterparty-verified work (not self-reported uptime),
and there is no cash-out.

---

## Horizon 5 — Frontier & research

Longer-range, lower-certainty. Tracked so the ideas aren't lost.

- **Hardened compute mode** — a partial mitigation for blinding a worker host to
  the data it processes. We are upfront that *true* secrecy needs TEE hardware; a
  research spike on worker/relay attestation would let the honesty model someday
  become enforcement.
- **Opt-in discovery directory** — a consented listing so strangers can find
  offered nodes/services/relays, beyond manual invites.
- **Federated grid discovery** — let separate grids discover each other through a
  relay and dispatch across trust boundaries (e.g. a lab borrowing GPU capacity
  from a partner org).
- **Checkpoint resume across sessions** — long tasks checkpoint periodically and
  resume on another worker if one goes offline (pending a clean checkpoint
  primitive for black-box container tasks).
- **Task replay & debugging** — record a task's inputs/environment/outputs and
  replay on a different worker to diagnose environment-specific failures.
- **Task template gallery** — a curated, community-contributed library of
  shareable task/DAG configs.
- **Mobile / PWA monitoring** — a phone-friendly, monitoring-only view (no task
  submission) wrapping the existing UI as a PWA.
- **Accessibility pass** — keyboard navigation, ARIA, and contrast.
- **Desktop app** — an installable native-window build (via an embedded webview +
  the existing PyInstaller pipeline, *not* an Electron/Tauri rewrite). Deferred:
  the browser experience comes first.

---

## Non-goals & deferred (on purpose)

These aren't oversights — they're decisions.

- **A mandatory central server.** Global features must degrade to "works fully
  without it." The most we'll consider is an *optional* aggregator/registry, and
  only where verifiable, counterparty-signed receipts can't already compute the
  answer via gossip. Tracked, not decided.
- **Cash settlement between peers.** Possibly indefinitely deferred. If it ever
  ships, it would be **opt-in, non-custodial** (we never hold funds), only for
  *objectively verifiable* work, with the free/collaborative tier first-class
  forever. The status/engagement layer above is the intended reward mechanism —
  not money.
- **Pay-to-win of any kind.** Cosmetics never affect scheduling or capability.
- **A heavy frontend framework.** The UI stays lean; we add reactivity with
  targeted patterns, not a large runtime.
- **Pretending container isolation equals secrecy.** A host with root sees inside
  its own containers. We document this rather than implying otherwise (see
  *hardened compute* above for the realistic mitigation path).

---

## How to influence the roadmap

- **File an issue** for a bug, a gap, or a feature request — concrete use cases
  move things up the list.
- **Start a discussion** for bigger directional ideas (anything in Horizons 3–5).
- **Open a PR.** Every change ships with a regression test and stays surgical (see
  [`CONTRIBUTING.md`](CONTRIBUTING.md)). Good first targets are documentation,
  guides, and the developer-experience items in Horizon 2.

Priorities shift as we learn what people actually use. Thanks for helping build
it.
