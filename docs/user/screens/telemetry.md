# Task Telemetry

**Sidebar → Use the grid → Task Telemetry.** Watch everything you've dispatched —
running and finished — with live logs, status, controls, and output artifacts.

---

## The task list

A searchable table of tasks (use the **Search…** box to filter). Standalone
dispatches and DAG workflows are shown; click a row (or **Open task detail**) to
expand the detail modal.

For each task you'll see its status (queued, processing, completed, failed,
cancelled, disrupted, awaiting-approval…), timing, and where it ran.

---

## The task detail modal

Opening a task gives you, in one bounded, scrollable panel:
- **Logs** and **Live logs** — the captured output, streaming while it runs.
- **Dependencies** and the **workflow graph** — for DAG steps.
- **Why queued** — the scheduler's explanation of why a task is waiting and what
  it's waiting for (QueueInsight).

This consolidation keeps a long log from stretching the page.

---

## Per-task actions

Depending on a task's state:

| Button | What it does |
|---|---|
| **Logs** | Open the log view. |
| **Clone** | Pre-fill the Dispatcher with this task's configuration to run it again. |
| **Disrupt** | Interrupt a running task (simulate a failure / stop it). |
| **Preempt** | Stop a task the local worker is running to free capacity. |
| **Delete** | Remove the task record. |
| **Save as artifact** | (In the log pane) Write the current live-log buffer into the task's result artifacts — useful for services that have no completed-task bundle. |

### DAG-specific actions
- **Resume DAG** — re-queue a workflow's **failed** steps and continue (it
  re-arms blocked descendants).
- **Approve & continue** — when a workflow is gated by "Verify each step," release
  the steps waiting for your approval and continue to the next level.

---

## Result artifacts

For a finished task, the **Result artifacts** browser lets you:
- Expand a result **bundle** to see its files and per-file sizes.
- **Preview** text files inline.
- **Download** any output file.

This is filesystem-backed (decoupled from the task DB), so artifacts remain
browsable even after the task record is cleared.

---

## Services in use

A panel shows **service grants you hold** (services on other nodes you've been
granted access to), so you can see and reach what you're consuming.

---

## Clear database

**Clear database** wipes all task records from your node (and reclaims the disk
space). Use it to tidy up after a lot of runs — it removes history, not your
identity or settings.
