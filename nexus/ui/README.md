# ui — HTML serving, avatar endpoint, UI broadcaster

## What this owns

The user-facing surface of the node:

- **serve.py** — serves `index.html` at `/`, injecting the local API token into
  a `<meta>` tag so the frontend can authenticate its own `fetch` calls.
- **avatar.py** — `/local/upload_avatar` (magic-byte validated image upload) and
  `/local/avatar` (cached image response with cache-busting timestamp).
- **broadcaster.py** — `broadcast_ui_update(payload)` — sends a state delta to
  all connected UI WebSocket clients. Called by `telemetry`, `tasks`, and
  `networking` via the event bus. also pipes the foreign-storage
  bus events (`storage.offer_incoming`, `storage.deposit_accepted`,
  `storage.deposit_completed`, `storage.eviction_requested`,
  `storage.deposit_purged`) into the UI so the bell + Foreign Storage
  tab stay live.

This package exists separately from `api` because the UI's routes have
different rules: they serve static content, they depend on token injection at
serve time, and they are the only package allowed to reach into `index.html`.

## Public surface

Exports from `nexus.ui`:

- `mount_ui(app: FastAPI) -> None` — registers `/`, `/local/avatar`, and
  `/local/upload_avatar`. The UI WebSocket itself is registered from
  `nexus.api.websocket` (`/local/ws`) since it shares the FastAPI router
  machinery.
- `broadcast_ui_update(payload: dict) -> None`
- `register_ws(ws)` / `unregister_ws(ws)` — called by the `/local/ws` handler.

## Dependencies

- Imports from: `nexus.core`, `nexus.security` (auth + avatar validation),
  `nexus.telemetry` (broadcaster payloads).
- Imported by: `nexus.app` (via `mount_ui`).

Forbidden: `api`. If the UI needs to talk to the rest of the node, it does so
through the business layer, not through HTTP calls back to `api`.

## Extending

- **New static asset**: put it next to `index.html` and register a route in
  `serve.py`. Avoid inlining large assets into `index.html`; use a separate URL
  so caching works.
- **New upload endpoint**: follow `avatar.py`'s validation pattern — magic
  bytes + size cap + `chmod 0o600` on the saved file.
- **New UI broadcast type**: add it through the event bus (`core.events`) so
  non-UI code doesn't import `ui.broadcaster` directly.

## Key files

| File            | Purpose                                                |
|-----------------|--------------------------------------------------------|
| `serve.py`      | Serve `index.html` with local-token injection          |
| `avatar.py`     | Upload + serve avatar (type + size validated)          |
| `broadcaster.py`| WS broadcast helper for live UI updates                |
