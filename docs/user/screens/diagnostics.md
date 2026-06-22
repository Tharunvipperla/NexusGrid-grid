# Diagnostics

**Sidebar → My node → Diagnostics.** Live health of your node and the work it
coordinates, plus the audit log and disk-usage breakdown.

---

## Health KPIs

Top-line counters:
- **Queue depth** — tasks waiting.
- **Processing** — tasks running.
- **Active workers** — workers currently executing.
- **Tasks completed** / **Tasks failed** — lifetime outcome tallies.

An **Issues** panel surfaces current problems (scrollable within a fixed height so
it never overwhelms the page). These metrics are also exposed at the
`/local/metrics` endpoint for external monitoring.

---

## Audit feed

A chronological, **filterable** log of security- and operations-relevant events
(consent decisions, grants, tripwires, deletes, settings changes, …). Filter by
severity/time, and **Export CSV** (or JSON) for record-keeping. Export and other
confirmations route to the notification bell rather than pop-ups.

---

## Storage usage

An interactive breakdown of your node's on-disk footprint by category — database,
identity, result artifacts, hosted deposits, build caches, backup leftovers, stale
per-port DBs, etc. — drawn as a donut with a colour-keyed legend (click a slice to
spotlight it).

- **Delete…** opens a per-file browser for a deletable category, so you can remove
  individual files or clear a whole category. Protected categories (identity, live
  DB) can't be deleted, and the path is validated to stay inside the category.
- **Refresh** re-scans.

This is where the disk used by [foreign storage](foreign-storage.md) you host and
[task artifacts](telemetry.md) shows up.

---

## Worker caches

Manage the caches your worker uses to run tasks faster:
- **Pre-warm a venv** — build a Python virtualenv ahead of time so the first task
  using it starts quickly.
- Inspect and clear cached environments/images.
