# Core Concepts

Read this once and the rest of NexusGrid makes sense. Each concept links to the
screen where you act on it.

---

## Node & identity

A **node** is one running instance of NexusGrid. It has:
- A **node UUID** — a stable public identifier, broadcast in discovery and shown
  to peers. *Not secret.*
- An **Ed25519 group keypair** — the node's cryptographic identity. The private
  half (in `.nexus_group_key`) signs everything the node says; the public half is
  how peers verify it's really you. **This is the thing trust binds to** — not the
  UUID.
- A **local API token** (`.nexus_local_token`) — the secret that protects your
  control panel. Anyone with it controls your node, so keep it on the machine.
- A **node secret** (`.nexus_secret`) — used to encrypt data at rest (e.g. the
  secrets vault, stored credentials).

> **Back these up.** Your identity files (`.nexus_*`) *are* your node. Losing them
> means a new identity; peers would have to re-trust you. See
> [Backup & restore](backup-and-restore.md).

---

## The grid, peers, and trust

NexusGrid has **no central server**. "The grid" is simply your node plus the
nodes it's connected to. There are two kinds of connection, and the difference
matters:

### Trusted peers (1:1)
A **trusted peer** is a node you've **paired** with directly (via an
`nxg://pair#…` link, approved on both sides). Pairing exchanges per-peer secret
tokens and records each other's identity. Trusted peers can exchange tasks,
storage, and direct messages. Manage them on **[Network Web](screens/network-web.md)**.

### Group members (many-to-many)
A **group** is a named set of nodes that all trust each other. A founder creates
the group and hands out **join links**; members can use each other's advertised
**services**, run group-targeted **tasks**, and chat. Membership is proven by an
admin-signed *grant* tied to your group pubkey. Manage groups on
**[Groups](screens/groups.md)**.

> **Trust binds to your group pubkey, not your UUID.** UUIDs are broadcast openly,
> so authorization always checks the cryptographic key your signature proves —
> never just a claimed UUID.

### The grid key
A **grid key** is a *shared secret* that gates **relay** use (see below) — it's
how a set of nodes agree "we're one cluster on this relay." It's optional: with
no grid key, relay join is open (opt-in security model). Set it in
**[Local Config](screens/local-config.md)**.

---

## Tasks & workflows (DAGs)

A **task** is a unit of work you dispatch to the grid: a container image + command,
or a small uploaded workspace, with resource and scheduling preferences.

- **Where it runs:** your own node, a specific trusted peer, or any member of a
  target group — your choice per dispatch.
- **Runtimes:** `docker` / `podman` (sandboxed containers, the default), `wasm`
  (wasmtime sandbox), or `native` (host subprocess — **off by default**, opt-in
  per node because it has weaker isolation).
- **Image allowlist:** container tasks may only use images on the receiving
  node's allowlist — a safety control so a peer never pulls arbitrary images.
- **Consent:** the first time a peer is asked to run your work, *they* approve it.

A **workflow / DAG** (Directed Acyclic Graph) is multiple tasks wired by
dependencies — step B runs after step A, branches run in parallel, results flow
between steps. The **[Dispatcher](screens/dispatcher.md)** has a builder, a
JSON/code editor, a visual graph editor, templates, per-step targeting, and a
"verify each step" gate. Watch everything in
**[Task Telemetry](screens/telemetry.md)**.

---

## Services & DBaaS

A **service** is a long-running thing you host (a web app, an API, a database) on
your node or a peer's, reachable over a secure tunnel. Other members request
**access grants**; you choose **free** (auto-approved), **permission** (you
approve each), or keep it private.

**DBaaS** is a one-click managed database: NexusGrid can start a Postgres / MySQL
/ Redis / MongoDB engine and provision a per-consumer database + login on an
approved grant. Manage all this on **[Services](screens/services.md)**.

---

## Foreign storage

**Foreign storage** lets you store data **on peers' disks**, and host data for
them, with strong guarantees:
- **Encrypted at rest** — the host stores ciphertext only; the depositor holds the
  key. A host (or anyone else) can never read your data.
- **Consent-gated** — a host explicitly accepts each deposit (and how much space).
- **Auto-recovery** — local-first encrypted rescue with optional cloud overflow
  (via rclone), so your data survives a host going away.

Deposit, host, share (view-grant), and recover on
**[Foreign Storage](screens/foreign-storage.md)**.

---

## Relays

A **relay** is a lightweight message-forwarding hop that lets nodes reach each
other across NAT/firewalls (when a direct connection isn't possible). Relays
**never see your plaintext** — peer payloads are signed and (for content)
encrypted end-to-end; the relay only forwards frames. A **grid key** decides which
nodes share a relay. You can use a shared relay or host your own (in-process or as
a plugin). Configure relays in **[Local Config](screens/local-config.md)** and the
[Plugins](screens/plugins.md) screen.

---

## Plugins (extending the node)

NexusGrid is pluggable. Four kinds of drop-in Python modules live in folders next
to the node and are editable from the UI:
- **Relays** (`nexus_relays/`) — custom relay implementations.
- **Service pumps** (`nexus_pumps/`) — transform service traffic.
- **Sandbox runners** (`nexus_runners/`) — add an execution backend.
- **DB providers** (`nexus_dbproviders/`) — add a DBaaS engine adapter.

Edit, validate, and (for relays/runners) sandbox-run them from the
**[Plugins](screens/plugins.md)** screen. You can also **package** plugins into a
single file to share, and **install** packages others share — installation only
writes + syntax-checks files, it never runs them.

---

## Trust & security

A quick mental model of how NexusGrid keeps you safe:

| Concern | How it's handled |
|---|---|
| **Who can use my control panel?** | The local API token **and** a local/private-network source check. Public clients are blocked unless you opt in (`NEXUS_ALLOW_REMOTE_UI`). |
| **Are peers who they claim?** | Every peer message is **signed**; authorization binds to the **group pubkey** the signature proves, never the (public) UUID. Direct messages are signed too, so a contact can't be impersonated. |
| **Can a peer run anything on me?** | No. Running tasks is **opt-in + consent-gated + sandboxed**, and container images must be on your **allowlist**. The unsandboxed `native` runtime is **off by default**. |
| **Is my stored data private?** | Yes — **encrypted at rest**, key held only by you (the depositor). Hosts store ciphertext. |
| **Are channels confidential?** | Peer transport uses TLS with **certificate pinning**; group messages and DMs are **end-to-end encrypted** (the relay sees only ciphertext). |
| **Are software updates safe?** | Auto-update verifies a **signed chain of trust** (a baked-in root key authorizes each release key) and checks the downloaded binary's hash. |

The **[Security Center](screens/security-center.md)** surfaces your current posture;
**[Diagnostics](screens/diagnostics.md)** shows the audit log of security-relevant events.

---

## How the pieces connect

```
        You (control panel, token-protected)
                 │
            ┌────▼─────┐      signed, encrypted        ┌──────────┐
            │ Your Node │◄──────────────────────────►│  Peer /   │
            │           │   (direct, or via a relay)   │  Group    │
            └────┬──────┘                              └──────────┘
        tasks · services · foreign storage · messages
```

Next: **[The interface](interface.md)**, then the per-screen guides.
