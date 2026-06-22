# NexusGrid Documentation

NexusGrid is a **peer-to-peer compute and storage grid**. You run a *node* on
your machine; it connects directly to other nodes you trust (no central server),
and together they form a private grid you can use to **run tasks, host services,
and store encrypted data** across each other's hardware.

There is no cloud account and no central authority. Your node holds its own
identity (a cryptographic keypair), talks to peers over authenticated,
encrypted channels, and only ever runs work you've consented to.

> **The core idea in one line:** pool compute and storage across machines you and
> people you trust already own — privately, with no middleman.

---

## Pick your path

### 👤 I want to *use* NexusGrid → [User Guide](user/getting-started.md)
- **[Getting started](user/getting-started.md)** — install, first launch, run your first task.
- **[Core concepts](user/concepts.md)** — nodes, peers, trusted peers, groups, the grid key, tasks, DAGs, services, foreign storage, relays.
- **[The interface](user/interface.md)** — the sidebar, top bar, notification bell, and profile menu.
- **Screen-by-screen reference** ([all screens](user/screens/)) — every screen, button, and control explained:
  - My node: [Overview](user/screens/overview.md) · [Live Topology](user/screens/topology.md) · [Security Center](user/screens/security-center.md) · [Diagnostics](user/screens/diagnostics.md) · [Local Config](user/screens/local-config.md) · [Plugins](user/screens/plugins.md) · [API & docs](user/screens/api-and-docs.md)
  - Use the grid: [Dispatcher](user/screens/dispatcher.md) · [Task Telemetry](user/screens/telemetry.md) · [Foreign Storage](user/screens/foreign-storage.md) · [Services](user/screens/services.md)
  - My people: [Groups](user/screens/groups.md) · [Messages](user/screens/messages.md) · [Network Web](user/screens/network-web.md)
- **[Backup & restore](user/backup-and-restore.md)** · **[Updating](user/updating.md)** · **[Troubleshooting](user/troubleshooting.md)**
- **In-depth feature guides** ([`guides/`](guides/)) — [Hosting services](guides/service-hosting.md) · [DBaaS](guides/dbaas.md) · [Plugins](guides/plugins.md) · [Deploying a relay](guides/relay-deploy.md)

### 🛠️ I want to *develop / extend* NexusGrid → [Developer Guide](dev/README.md)
Architecture, the plugin system, the local REST API + SDK, build & packaging,
and how to extend each layer.

---

## Is NexusGrid for me?

NexusGrid shines when you want to **pool resources across machines you trust** —
your own homelab boxes, a small research group's GPUs, or a circle of friends:

- **Run compute** that's too big for one machine, or just to use idle hardware.
- **Host services** (web apps, databases, APIs) on a peer's machine and reach them securely.
- **Store data** redundantly on peers' disks — always encrypted; only you hold the key.

It is **not** a public marketplace of strangers and there is **no token or
payment** — by design. Trust is established explicitly (pairing, group invites),
and every node chooses exactly what work and storage it accepts.

---

## A note on safety

Running other people's tasks is **opt-in and sandboxed** (containers, or an
explicitly-enabled native sandbox), gated by your **image allowlist** and a
**consent** step. Stored data is **encrypted at rest** with a key only the
depositor holds. Peer messages are **cryptographically signed**. See the
[Security Center](user/screens/security-center.md) and
[Core concepts → Trust & security](user/concepts.md#trust--security) for the full model.
