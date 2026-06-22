# Plugins

**Sidebar → My node → Plugins.** Edit, validate, and run the drop-in Python
modules that extend your node — without leaving the UI — and share/install plugin
**packages**.

---

## The four plugin kinds

| Kind | Folder | What it does |
|---|---|---|
| **Relays** | `nexus_relays/` | Custom relay implementations (run/bind from a group's relay settings). |
| **Service pumps** | `nexus_pumps/` | Transform service traffic (reference one from a service's *pump* field). |
| **Sandbox runners** | `nexus_runners/` | Add an execution backend used when running services/tasks. |
| **DB providers** | `nexus_dbproviders/` | Add a DBaaS engine adapter. |

The screen is a gallery of these categories; click a category, then a module to
open it.

---

## Editing a module

A full-page code editor lets you:
- Write/edit a module's Python (a **New** module starts from a template).
- **Validate** — a Python syntax check (it never runs the code).
- **Save** — writes the file (LF-normalized for stable fingerprints).
- **Make a copy** — fork an existing or built-in module.
- **Delete** — remove your module.

**Built-in reference** implementations are shown read-only so you can see what the
app ships by default; copy one to customize it.

> **Important:** saving only writes + syntax-checks the file. **Running** a module
> stays each subsystem's explicit, sandboxed action (relays/runners run in a
> sandbox; you trigger it deliberately). Editing host-trusted Python — do it with
> care.

---

## Plugin packages (share & install)

Click **Packages** to bundle your modules into one portable file, install one
someone shared, or keep a local library.

- **Export a package** — tick the modules to include, give it a **name** and
  **description**, then **Download package** (a `.json` file) or **Save to
  library**.
- **Install from file** — upload a package someone gave you. Installing **only
  writes + syntax-checks** the modules — it **never runs** them. Existing modules
  are skipped unless you tick **Overwrite existing**.
- **Library** — your saved packages: **Install**, **Download**, or **Delete** each.

Packages are plain files with no central registry — share them however you like. A
package from a newer node format than yours is refused (update first).
