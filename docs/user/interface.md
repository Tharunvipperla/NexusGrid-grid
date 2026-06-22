# The Interface

The control panel has three fixed parts that are always present: the **sidebar**
(left), the **top bar** (top), and the **notification bell**. Everything else is
the screen you've selected.

---

## Sidebar (navigation)

The sidebar lists every screen, grouped by purpose:

**My node** — your own node's settings and tools
| Item | What it's for |
|---|---|
| **Overview** | At-a-glance dashboard: your node's health, capacity, and recent activity. |
| **Live Topology** | A live graph of the nodes you can see and how they connect. |
| **Security Center** | Your current security posture and controls. |
| **Diagnostics** | Health KPIs, storage-usage breakdown, and the audit log. |
| **Local Config** | All node settings: identity, scheduling, networking, backup, secrets. |
| **Plugins** | Edit/validate/run your drop-in modules; share & install plugin packages. |
| **API & docs** | The node's REST API reference, SDK/CLI snippets, and webhooks. |

**Use the grid** — getting work done
| Item | What it's for |
|---|---|
| **Dispatcher** | Build and launch tasks and multi-step DAG workflows. |
| **Task Telemetry** | Watch running/finished tasks; logs, status, output artifacts. |
| **Foreign Storage** | Deposit encrypted data on peers; host data for them; recover. |
| **Services** | Host long-running services/databases; manage access grants. |

**My people** — connections
| Item | What it's for |
|---|---|
| **Groups** | Create/join groups; manage members and invites. |
| **Messages** | Direct messages and chat. |
| **Network Web** | Pair with peers; manage trusted-peer connections. |

**Collapse the sidebar** by clicking the brand/logo at the top-left — handy on
small screens or for more table space. A badge on a nav item means there's
something needing attention on that screen.

---

## Top bar

- **Brand / logo (left)** — click to collapse/expand the sidebar.
- **Notification bell** — see below.
- **Profile button (right)** — shows your node's display name; opens the
  **profile menu**.

---

## Notification bell

The bell is your **single inbox for things that need a decision or are worth
knowing**. It aggregates events from across the node so you never miss one:

- Incoming **foreign-storage offers** and deposit lifecycle (accepted, completed,
  eviction requested, rescued).
- **Pair requests** from peers wanting to connect.
- **Group/message invites**.
- **Service access grants** to approve.
- Security **tripwires** (e.g. unauthorized-access attempts on your hosted data).
- An **update available** flag and a **"What's new"** flag for new releases.

**Click any notification to jump to the screen where you can act on it.** This is
deliberate: NexusGrid routes confirmations and alerts to the bell rather than
using disruptive pop-ups. (You can silence the bottom-right pop-ups and the bell
badge in Interface settings — see below.)

---

## Profile menu

Click your node name (top-right) to open it. From here you can:
- **Edit profile** — your display name and "About me" (what peers see).
- **Interface settings** — appearance and per-browser preferences (next section).
- **Theme** — switch light/dark.
- **What's new** — the in-app changelog (also flagged by the bell on a new release).
- **Update now** — when an update is available; it **confirms first** and, for a
  release flagged *breaking*, warns you to take a Full backup before applying.

---

## Interface settings

Opened from the profile menu. These preferences are **per-browser** (they don't
change the node itself or other people's view):

- **Theme** — light / dark.
- **Notifications** — turn the bottom-right pop-ups and the bell badge on/off.
- **Topology — nodes to draw** — cap how many nodes the Live Topology graph
  renders (large counts can stutter; it warns you and lets you apply anyway). The
  graph always draws the most-relevant N; search/filters reach the rest.
- **Compact mode** — tighten tables and cards to fit more rows on screen.
- **About / update** — your version, an update banner (with *Update now* + patch
  notes), and the **What's new** button.

---

## Powering the node off

Use the **power control** to shut the node down gracefully (it finishes cleanly
rather than being killed). The node is a normal program — you can also stop it
from the terminal where you launched it.

---

Next: the per-screen guides, starting with **[Overview](screens/overview.md)**.
