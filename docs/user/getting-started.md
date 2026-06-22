# Getting Started

This guide takes you from zero to a running node, connected to a peer, running
your first task.

---

## 1. What you're installing

A **node** is a single program: a small local web server (your control panel) +
the peer-to-peer engine. You open the control panel in your browser; the node
does the networking, scheduling, storage, and sandboxed execution behind it.

You can run one node per machine (or several on one machine using different
ports, for testing).

---

## 2. Install & run

### Requirements
- Python 3.10+ (3.11+ recommended).
- For running container tasks/services: Docker or Podman installed and running.
  (You can use NexusGrid without them — you just won't be able to *run* container
  tasks locally; you can still dispatch to peers, store data, and chat.)

### Install
```bash
git clone <your-fork-or-release-url> nexusgrid
cd nexusgrid/Phase-2
pip install -r requirements.txt
```

### Run
```bash
python -m nexus
```
On start, the node:
- generates its identity (keypair) and a **local API token** the first time,
- picks up TLS (self-signed) for peer connections,
- prints the control-panel URL and opens your browser.

You'll see two lines like:
```
[nexus] UI:    https://127.0.0.1:8000/
[nexus] Bound: https://0.0.0.0:8000 (listen address)
```
Open the **UI** URL. (The "Bound" line is the network listen address — see
[networking](#5-networking--reachability) below.)

> **Run options:** `python -m nexus --port 8001` to pick a port,
> `--host 127.0.0.1` to bind loopback only, `--no-browser` to skip auto-open.

### The local API token
Everything in the control panel is protected by a **local API token** (a 64-char
secret stored in `.nexus_local_token`). The browser page is served with the token
already injected, so you normally never see it. Management requests are *also*
restricted to local/private-network clients — a stranger on the internet can't
reach your control panel even though the node may listen on all interfaces. See
[Security](concepts.md#trust--security).

---

## 3. First launch: set up your identity

Open **Local Config** (sidebar → *My node → Local Config*) and set:
- **Display name** — how peers see you (a friendly label; you get a random one by default).
- **About me** *(optional)* — a short blurb shown on your node profile.

That's all that's required to start. Everything else has sensible defaults.

---

## 4. Connect to a peer

NexusGrid is peer-to-peer, so the grid is "you + the peers you connect to."
There are two ways to connect, both **explicit and consented**:

### Option A — Pair with one peer (1:1 trust)
Best for "I want to link my two machines" or "connect with a friend."
1. On node A, go to **Network Web** and create a **pair invite link** (`nxg://pair#…`).
2. Share the link with node B (any channel — chat, email).
3. On node B, redeem the link. Node A gets a request in its **notification bell**;
   approve it. Now A and B are **trusted peers**.

> On the same LAN, nodes also discover each other automatically via a broadcast
> beacon — you'll see them appear, but you still pair to establish trust.

### Option B — Join a group (many-to-many trust)
Best for a team/lab/friend-group sharing a pool.
1. A founder creates a **group** (sidebar → *My people → Groups*) and generates a **join link**.
2. You redeem the link to join. Members of a group can use each other's
   advertised services and (with consent) run each other's tasks.

See [Core concepts](concepts.md) for the difference between **trusted peers** and
**group members**, and what the **grid key** does.

---

## 5. Networking & reachability

- **Same LAN:** nodes find and reach each other directly. Easiest case.
- **Across the internet / behind NAT:** nodes connect through a **relay** (a
  light message-forwarding hop). You can use a shared relay or host your own; a
  **grid key** (a shared secret) gates who may use a relay together. See
  [relays](concepts.md#relays).
- By default the node binds all interfaces so peers can reach it, but the
  **admin control panel is still restricted to local/private-network clients +
  the token.** To bind loopback only, run with `--host 127.0.0.1`.

---

## 6. Run your first task

1. Go to **Dispatcher** (sidebar → *Use the grid → Dispatcher*).
2. Choose a workload — e.g. a container image on your allowlist (like
   `python:3.11-slim`) and a command, or upload a small workspace.
3. Pick where it runs (your own node, or a trusted peer/group) and dispatch.
4. Watch it in **Task Telemetry** — live logs, status, and (when done) output
   artifacts you can browse and download.

> The very first time a peer is asked to run your task, **they** see a consent
> prompt — running others' code is always opt-in on the receiving side.

The full Dispatcher walkthrough (single tasks, multi-step DAGs, targeting,
scheduling options) is in the [Dispatcher screen guide](screens/dispatcher.md).

---

## 7. Where to go next
- **[Core concepts](concepts.md)** — understand the moving parts before going deep.
- **[The interface](interface.md)** — the sidebar, bell, and profile menu.
- **[Foreign Storage](screens/foreign-storage.md)** — store encrypted data on peers.
- **[Services](screens/services.md)** — host an app/DB on the grid.
- **[Backup & restore](backup-and-restore.md)** — protect your node's identity + data.
