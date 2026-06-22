# nexus — package overview

`nexus` is the modular NexusGrid backend. Every subdirectory owns exactly one
concern and exposes a documented public surface via its `__init__.py`. A developer
touching one concern should not need to read another.

## Dependency graph

Arrows go left to right. A module may import from anything to its left but not the
other way. This is enforced by convention — new code that breaks the layering
should be reworked rather than accommodated.

```
 utils  ─┐
         ├─►  core  ─►  storage  ─┐
 (pure   │                        ├─►  security  ─┐
 helpers)│                        │               │
         │                        │               ├─►  tasks  ─┐
         │                        │               │            │
         │                        │               │   caches  ─┤
         │                        │               │            │
         │                        │               │  telemetry ┤
         │                        │               │            │
         │                        │               │            ├─►  runtime  ─►  scheduler  ─►  networking  ─┐
         │                        │               │            │                                             │
         │                        │               │            │                                             ├─►  api ─┐
         │                        │               │            │                                             │         ├─►  app / __main__
         │                        │               │            │                                             └─►  ui  ─┘
```

### Reading this graph

- `utils` and `core` are foundation — they depend on no other `nexus.*` module.
- `security`, `tasks`, `caches`, `telemetry` form the shared services layer.
  Order inside this layer is intentionally flexible.
- `runtime` is a layer on its own because Docker/native execution pulls in heavy
  dependencies that lower layers should not see.
- `scheduler` sits above `runtime` because scheduling decisions may interrogate
  runtime state (e.g. current worker capacity).
- `networking` is the integration layer: it uses `scheduler`, `runtime`,
  `telemetry`, and `security`.
- `api` and `ui` are the *outermost* layer. Nothing imports from them.
- `sdk` is a standalone thin client of the local API (used externally and by the
  `python -m nexus.sdk` CLI); it doesn't import the rest of the node.

## Subdirectory index

| Directory    | One-liner                                                                |
|--------------|--------------------------------------------------------------------------|
| `utils/`     | Leaf helpers (time, hashing, text, net). Stdlib only.                    |
| `core/`      | Config, constants, identity, shared state, pub/sub bus.                  |
| `storage/`   | SQLAlchemy models, engine, session factory, repositories.                |
| `security/`  | Auth deps, Ed25519/ECIES/AEAD crypto, signed update chain, tokens, threat scanner, profiles, input guards. |
| `tasks/`     | Task lifecycle, queue, lease, metadata, step targeting, DAG resume.      |
| `caches/`    | venv / pip / node caches + workspace dependency scanning.                |
| `telemetry/` | Logs, metrics, alerts, audit (+export), presence, hardware sampling.     |
| `runtime/`   | Task/service execution, DBaaS, foreign storage, relays, groups, plugins, node features (backup/update/secrets/webhooks). |
| `scheduler/` | Worker fitness, task selection, retry, reliability, benchmark, DAG resolution. |
| `networking/`| LAN discovery, peer protocol, relay/worker-client loops, tunnels, log forwarding. |
| `api/`       | FastAPI routers (/local, /peer, groups, events/SSE, WebSockets, diagnostics, /health). |
| `ui/`        | SPA serving (token-injected), avatar endpoint, WebSocket/event broadcaster. |
| `sdk/`       | Thin Python client + OpenAPI-driven CLI for a node's local API.          |

Each subdirectory has its own README with a public API list, dependency arrows,
and an "extending" section.

## Adding a new subpackage

1. Create the directory with an `__init__.py` and a `README.md` following the
   template in any existing subpackage.
2. Add it to the dependency graph above. If the new subpackage breaks the layering
   rule, rework it — the rule exists so future contributors don't have to untangle
   a cycle.
3. Re-export the public surface from `__init__.py`. Symbols that aren't in
   `__init__.py` are internal and may be renamed freely.
4. Add the new module to `collect_submodules('nexus')` implicitly via
   PyInstaller — `NexusGrid.spec` already uses `collect_submodules` so no edit is
   usually needed.

## External dependencies

Pinned in `../requirements.txt`. The runtime layer:

- `fastapi` + `uvicorn[standard]` — HTTP/WebSocket server.
- `sqlalchemy` + `aiosqlite` — async SQLite ORM (see `storage/`).
- `httpx` — outbound HTTP (relay client, cloud-relay deposit pulls).
- `websockets` — peer-to-peer relay/master transport.
- `cryptography>=48` — TLS certs (`security/tls.py`), Ed25519 signing
  (`security/group_grant.py`), X25519+ChaCha20Poly1305 ECIES
  (`security/group_ecies.py`), AES-256-GCM
  for foreign-storage chunk encryption (`security/deposit_crypto.py`),
  and HKDF-SHA256 for cloud-credential wrapping
  (`security/cred_crypto.py`).
- `argon2-cffi>=23` — Argon2id password KDF for foreign-storage
  deposits (; `security/deposit_crypto.py`).
- `psutil` — hardware sampling for telemetry.
- `pydantic` — request/response models.

**optional extras** — `google-api-python-client>=2.150.0`
+ `google-auth>=2.40.0` are imported lazily by
`storage/cloud/gdrive.py` only when a depositor exercises the
external-cloud eviction tier. The app boots without them. See
`../README.md` for the install command.

**depositor in-browser preview.** No new external deps. Adds
`runtime/foreign_storage_keys.py` (typed session-key cache with idle
TTL + in-place zeroization) and `runtime/preview_pump.py` (LRU
plaintext cache + in-flight Future map for the streaming preview
endpoint). Five new endpoints under `/local/foreign_storage/`:
`unlock/{id}`, `lock/{id}`, `unlocked`, `manifest/{id}`,
`preview/{id}` (Range-aware). PDF rendering uses the browser's native
viewer; no PDF.js bundle is shipped.

`pyinstaller` is build-time only.

## Running

See `../README.md` for the quick-start and build instructions.
