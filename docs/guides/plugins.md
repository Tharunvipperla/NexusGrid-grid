# NexusGrid — In-app plugin editor

Edit the drop-in plugin modules that extend a node — **relays**, **service
pumps**, **sandbox runners**, **DB providers** — straight from the UI
(**Plugins** tab) instead of digging into files on disk.

Each kind is just a folder of host-trusted `.py` files under the node's
`BASE_DIR`, loaded by its own subsystem:

| Kind | Folder | Loaded by | What a module exposes |
|---|---|---|---|
| `relays` | `nexus_relays/` | `local_relay` | an ASGI `app` + settable `GRID_KEY` |
| `pumps` | `nexus_pumps/` | `service_tunnel` | `register_pump(name, factory)` |
| `runners` | `nexus_runners/` | `replica_runner` | `register_runner(name, build, …)` |
| `dbproviders` | `nexus_dbproviders/` | `db_provider` | `create(...)` + `drop(...)` (+ optional `KIND`) |

## UI model

- **Plugins** → a card per kind → click in to a full-width list of the
  modules you've built in that kind → click a module to open the
  full-page code editor. A search box appears once a kind has more than
  ~6 modules.
- **Create** a module from a per-kind template; **Save** runs a Python
  syntax check first and refuses to write on error (shows the line).
- **Edit** or **Make a copy** of any of your modules.
- The built-in **`default`** relay is **read-only** — viewable and
  copyable (to fork it), never editable or deletable.
- Save/delete confirmations go to the **notification bell**, not popups.

Which relay code actually *runs* is chosen per-node in **Settings →
Internet relay** (a searchable dropdown of `default` + your modules);
services and groups can each reference different relay/pump/provider
code, so there is no single global "active plugin" beyond the node's own
bundled relay.

## API

All endpoints are on the local management router and gated by
`verify_local_auth`. They appear automatically in the in-app **API &
docs** screen (generated from the OpenAPI spec) under the `Plugins` tag.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/local/plugins` | All kinds + their modules (name, size, fingerprint) |
| `GET` | `/local/plugins/{kind}/{name}` | Read a module's source |
| `PUT` | `/local/plugins/{kind}/{name}` | Create/overwrite (validates syntax; does **not** run it) |
| `DELETE` | `/local/plugins/{kind}/{name}` | Delete a module |
| `POST` | `/local/plugins/validate` | Python-syntax check a source (no execution) |
| `GET` | `/local/relay/modules` | Relay modules this node can run (incl. `default`) |
| `GET` | `/local/relay/modules/{name}/source` | Read a relay module's source (`default` = the bundled relay) |

Source is normalized to LF and written as raw bytes so relay fingerprints
stay byte-stable across platforms.

## Security model

The editor is a thin, sandboxed surface — it cannot be used to change
NexusGrid's core behaviour:

- **Confined writes.** The kind→folder map is fixed; a write can only
  ever land in one of the four plugin folders, never in the `nexus/`
  package or anywhere else.
- **No path traversal.** Module names must `fullmatch`
  `[A-Za-z0-9_-]{1,40}` — no `/`, `\`, `.`, or `..`. The reserved name
  `default` is rejected, so the built-in relay can't be overwritten or
  deleted.
- **Local operator only.** Every endpoint requires `verify_local_auth`
  (loopback/RFC1918 client + the node's local API token). Remote peers
  have no path to these endpoints.
- **No execution on write.** The API only does file CRUD + a
  `compile()` syntax check. It never imports or runs plugin code —
  running a relay/runner stays an explicit, consent-gated, sandboxed
  action in its own subsystem.

Net: only the person operating a node, on that node, can edit that node's
own drop-in plugins. Editing arbitrary host-trusted Python on your own
machine is by design (same as editing the files on disk) — but it is
fenced to the plugin folders and unreachable by anyone else.

## Tests

`tests/test_wave72_plugin_files.py` (20 tests) — validation, CRUD for
every kind, CRLF→LF normalization, relay-only fingerprints, delete
guards, kind overview, and view/copy of the built-in default relay.
