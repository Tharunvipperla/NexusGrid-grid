# Plugin System

NexusGrid loads drop-in Python modules from four folders next to the node. They're
edited from the [Plugins screen](../user/screens/plugins.md) and managed by
`nexus/runtime/plugin_files.py`. The folder, the contract, and how each is loaded:

| Kind | Folder | Loaded by | Contract |
|---|---|---|---|
| Relay | `nexus_relays/` | `runtime/local_relay.py` | Expose an ASGI `app` and a settable `GRID_KEY`. |
| Service pump | `nexus_pumps/` | `runtime/service_tunnel.py` | Call `register_pump(name, factory)`; `factory()` returns `transform(direction, chunk)`. |
| Sandbox runner | `nexus_runners/` | `runtime/replica_runner.py` | Call `register_runner(name, build, …)` to add an execution backend. |
| DB provider | `nexus_dbproviders/` | `runtime/db_provider.py` | Expose `create(admin_dsn, db, user, pw)` + `drop(admin_dsn, db, user)` (+ optional `KIND`). |

> **Safety model:** editing/saving a module only **writes + syntax-checks** it
> (`compile()`), never executes it. Running stays each subsystem's explicit,
> sandboxed action. The same applies to **plugin packages** — install writes
> files, it never runs them. Keep it that way.

---

## Service pump

Transforms bytes flowing through a hosted service's tunnel.

```python
# nexus_pumps/my_pump.py
from nexus.runtime.service_tunnel import register_pump

def _make():
    def transform(direction, chunk):
        # direction: "to_consumer" | "to_provider"; return None to drop the chunk
        return chunk
    return _transform if False else transform

register_pump("my-pump", _make)
```
Reference it from a service's **pump** field.

---

## Sandbox runner

Adds an execution backend. `build(ctx)` returns the argv to launch the run-spec.

```python
# nexus_runners/my_runner.py
from nexus.runtime.replica_runner import register_runner

def _build(spec_ctx):
    # return the argv list that launches spec_ctx in your sandbox
    return ["echo", "hello"]

register_runner("my-runner", _build, sandboxed=True, available=lambda: True)
```
Built-ins are `docker`, `podman`, `raw` (services only, local-consent). For task
execution the runtimes are `docker`/`podman`/`wasm`/`native` (see
`runtime/executor.py`).

---

## DB provider (DBaaS adapter)

Provisions a per-consumer database + login on an approved grant.

```python
# nexus_dbproviders/my_engine.py
KIND = "postgres"   # optional engine kind hint

def create(admin_dsn, database, user, password):
    ...   # idempotent: create the DB + login if absent

def drop(admin_dsn, database, user):
    ...   # idempotent: tear them down
```
**Build identifiers safely.** The framework passes hash-derived names
(`nx_<hash>`) and a generated password, but any SQL you build must use
parameterized identifiers/literals (e.g. psycopg `sql.Identifier`/`Literal`) or
proper escaping — never raw f-string interpolation of caller input.

---

## Relay

A relay is just an ASGI app with a settable grid key:
```python
# nexus_relays/my_relay.py
from fastapi import FastAPI

GRID_KEY = ""
app = FastAPI()

@app.get("/")
def root():
    return {"relay": "my-relay"}
```
Relays carry **ciphertext only** — never decrypt peer payloads in a relay. Run/bind
one from the relay admin surface; foreign relay code runs **sandboxed**.

---

## Packaging plugins

`runtime/plugin_packages.py` builds a portable JSON package
(`{format, version, modules:[{kind,name,source}]}`) and installs one (validating
kind/name/syntax, refusing a newer format, skipping existing unless `overwrite`).
A node keeps a local library under `nexus_packages/`. Share packages as files —
there's no central registry (decentralized by design).
