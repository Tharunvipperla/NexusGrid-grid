# API & docs

**Sidebar → My node → API & docs.** Your node serves a full local REST API — this
screen is its live reference, plus SDK/CLI snippets and **webhooks**. Use it to
build your own UI, scripts, or integrations against your node.

---

## Quickstart

The essentials to call the API:
- **Base URL** — your node's origin (copyable).
- **Auth header** — every `/local/*` call carries `X-Local-Token: <token>` (the
  local API token). Copy the token here.
- **Example** — a ready-made `curl` you can paste.

> Management calls are restricted to local/private-network clients as well as the
> token. CORS is restricted to the node's own origins by default; set the
> `NEXUS_CORS_ORIGINS` env var to allow a custom UI from elsewhere.

---

## SDK & CLI

Drive the API without hand-writing requests:
- **Built-in CLI** — `python -m nexus.sdk ops` lists live operations;
  `... call GET /local/network` calls one. It reads `.nexus_local_token`
  automatically.
- **Python SDK** — `NexusClient.from_local(...)` for scripts.
- **Generate a typed client** — one-liners to generate a typed TS/Python client
  from the live `/openapi.json` with standard tools (no heavy generator bundled).

---

## Endpoints

A searchable list of **every endpoint** the node serves, grouped by tag, with
method + path + summary. Filter by path, tag, or method. Links to the interactive
**Swagger** and **ReDoc** docs and the raw **openapi.json** are at the top — these
never drift from the real surface because they're generated from the live spec.

---

## Webhooks

Make external systems react to grid events — your node POSTs a small JSON payload
to a URL you choose whenever a subscribed event fires.

- **Add webhook** — set a **Payload URL**, tick the **Events** to subscribe to
  (e.g. `task.completed`, `task.failed`, `dag.released`, `storage.deposit_completed`),
  an optional **Signing secret**, and a description.
- **Signing** — if you set a secret, each delivery includes an
  `X-NexusGrid-Signature: sha256=HMAC(secret, body)` header so your receiver can
  verify the payload really came from your node. The secret is never shown back
  (only "signed: yes/no").
- **Send test event** — fire a test delivery to confirm your endpoint works.
- **Recent deliveries** — the last deliveries with their HTTP status, so you can
  see failures.

Delivery is best-effort and non-blocking (per-event, short timeout). Webhooks are
configured locally (token-gated), so only you can add them.
