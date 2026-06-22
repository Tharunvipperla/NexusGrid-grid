# Dispatcher

**Sidebar → Use the grid → Dispatcher.** This is where you build and launch work
— from a single task to a multi-step DAG workflow — and choose where and how it
runs.

---

## Choosing what to run

A dispatch is built from one or more **steps**. Each step describes a unit of
work:

| Field | Meaning |
|---|---|
| **Step id** | A short name for the step (used to wire dependencies in a DAG). |
| **Runtime** | `docker` / `podman` (sandboxed container, default), `wasm` (wasmtime), or `native` (host subprocess — only if the target node enabled it). |
| **Image** | The container image to run (must be on the target node's **allowlist**). |
| **Run command** | The command to execute, e.g. `python step.py`. |
| **Setup (optional)** | A pre-step command, e.g. `pip install -r requirements.txt`. |
| **Parallel slices** | Fan the same step into N parallel copies (data-parallel work). |

You can also attach a **workspace** (a small upload of files your command needs),
a **Dockerfile build context** (build a custom image from a base on the
allowlist), and **cloud inputs** (files fetched from an http(s) URL or rclone
remote before the run).

---

## Single task vs DAG workflow

- **Single task** — one step, dispatched once.
- **DAG workflow** — several steps wired by `depends_on` so they run in the right
  order, with parallel branches and joins. The Dispatcher offers three ways to
  author a DAG, all round-tripping to the same definition:
  1. **Builder** — add/edit steps in a form.
  2. **JSON / code editor** — edit the workflow JSON directly (with a full-screen
     "Expand" editor that formats + validates).
  3. **Graph editor** — a visual canvas: click a node to edit it, drag its handle
     to wire a dependency (cycles are blocked), click an edge to remove it,
     add/delete nodes, and **zoom −/+/Fit** so large DAGs stay navigable.

### Templates
Save a DAG you've built as a **template** and reload it later, or multi-select
several templates to **merge** them into one workflow. (Managed per-node.)

---

## Targeting — where it runs

Per dispatch (and, in a DAG, per step) you choose where work lands:

| Field | Meaning |
|---|---|
| **Groups (CSV)** | Restrict to members of these groups, e.g. `groupA, groupB`. |
| **Nodes (CSV)** | Restrict to specific node addresses, e.g. `10.0.0.5`. |
| **Required tags (CSV)** | Only nodes advertising these capability tags, e.g. `gpu, highmem`. |
| **Priority (0–100)** | Higher priority is scheduled first. |
| **One step per node** | (DAG) Spread a workflow's steps across nodes — no node runs two sibling steps at once. |

Leave targeting blank to run on your own node / let the scheduler pick.

---

## Scheduling options

- **Prefer reliable workers** — a tri-state (node default / on / off): bias
  scheduling toward peers with a better finished-to-fail history.
- **Verify each step** (DAG) — a tri-state gate: hold each DAG *level* until you
  approve it before the next level is assigned, so you can stop early if something
  looks wrong. You approve from [Task Telemetry](telemetry.md).
- **Retries / lease / backoff / queue timeout** — per-dispatch overrides for how
  long a step may run, how many times it retries, and how long it waits in queue
  (blank = your node's defaults).

---

## Dispatch profiles

Save the **resources + scheduling + targeting** fields (not the workload itself)
as a named **dispatch profile**, then apply it in one click on a later dispatch.
Handy when you always dispatch with the same constraints (e.g. "GPU group, high
priority").

---

## Launching

When you dispatch, the node expands the workflow, packages each step, inserts the
task records, and enqueues the ready steps. The **first time** a target peer is
asked to run your work, *they* get a consent prompt — running others' code is
opt-in on the receiving side.

Track everything in **[Task Telemetry](telemetry.md)**.
