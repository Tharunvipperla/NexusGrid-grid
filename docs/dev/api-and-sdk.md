# Local API, SDK & Webhooks

The node serves a full REST API (FastAPI). The UI is just a client of it, so
anything the UI does, you can script.

---

## Two route families

| Prefix | Auth | Purpose |
|---|---|---|
| `/local/*` | `verify_local_auth` — the local API token **and** a local/private-network source-IP check (real socket IP, not `X-Forwarded-For`); public IPs need `NEXUS_ALLOW_REMOTE_UI`. | Management API used by the control panel. |
| `/peer/*` | Most routes: `verify_trusted_peer` (per-peer secret `X-Cluster-Key` token). Some carry **in-body** auth instead (signed statements / HMAC) — e.g. `service_request`, `dm`, `join_request`. | The peer-to-peer protocol. |

Plus `/app` (UI), `/local/events/stream` (SSE), `/openapi.json`, `/docs`, `/redoc`,
`/health`.

The token is injected into the served page as `<meta name="nexus-token">`; SSE and
browser tags that can't set headers accept `?local_token=`.

---

## OpenAPI is the source of truth

The endpoint list, Swagger/ReDoc, and the SDK/CLI all read the **live**
`/openapi.json`, so they never drift. Browse them on the
[API & docs screen](../user/screens/api-and-docs.md).

---

## SDK & CLI (`nexus/sdk/`)

```python
from nexus.sdk import NexusClient
c = NexusClient.from_local("https://127.0.0.1:8000")  # reads .nexus_local_token
print(c.get("/local/network"))
```
```bash
python -m nexus.sdk --base https://127.0.0.1:8000 ops               # list operations
python -m nexus.sdk --base https://127.0.0.1:8000 call GET /local/network
```
Generate a typed client from the spec with any standard tool (e.g.
`npx openapi-typescript .../openapi.json`); nothing heavy is bundled.

---

## Adding an endpoint

1. Add the route to the right router in `nexus/api/` with
   `dependencies=[Depends(verify_local_auth)]` (management) or the appropriate peer
   auth.
2. Keep handlers thin — validate + delegate to a `runtime/`/`tasks/` function.
3. **Route ordering matters:** declare literal paths *before* a generic
   `/{a}/{b}` matcher in the same router, or the literal gets captured as params
   (this bit us once — see the plugin-packages routes).
4. Add a pytest. The new endpoint shows up in `/openapi.json` automatically.

---

## Webhooks (`runtime/webhooks.py`)

Outbound integration: the node POSTs a signed JSON payload to user-configured URLs
when a domain event fires.

- `install_webhook_dispatcher()` subscribes once (at startup) to a curated set of
  `nexus.core.events` (`task.status_changed` + synthesized `task.completed`/
  `task.failed`, `dag.released`/`gated`, `scheduler.requeued`,
  `storage.deposit_completed`/`offer_incoming`).
- Payload: `{event, ts, node, data}`, optionally signed
  `X-NexusGrid-Signature: sha256=HMAC(secret, body)`.
- Managed via `/local/webhooks` (+ `/test`); subscriptions live in
  `LOCAL_SETTINGS["webhooks"]` (http(s)-only, capped), read at fire-time.

To surface a **new** event to webhooks/UI: publish it on `nexus.core.events`, then
subscribe to it in `install_webhook_dispatcher` (and the UI broadcaster if the
panel should react).
