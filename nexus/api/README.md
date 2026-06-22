# nexus/api

The **HTTP + WebSocket routers** — the node's entire external surface. Handlers
here are thin: they authenticate, validate, and delegate to
[`runtime`](../runtime/README.md) / [`tasks`](../tasks/README.md) /
[`storage`](../storage/README.md). Routers are mounted by `register_routers()`
(called from [`nexus/app.py`](../app.py)).

Two route families (see the [API & SDK dev guide](../../docs/dev/api-and-sdk.md)):
- **`/local/*`** — management API for the control panel. Auth: `verify_local_auth`
  (the local token **and** a local/private-network source check).
- **`/peer/*`** — the peer-to-peer protocol. Auth: mostly `verify_trusted_peer`
  (per-peer token); some routes carry **in-body** signed-statement auth.

> When you add a router/module, add a line below. **Route ordering:** declare
> literal paths before a generic `/{a}/{b}` matcher in the same router.

| Module | Routes | Purpose |
|---|---|---|
| `local.py` | `/local/*` | Management API: peers, tasks/dispatch, settings, services, foreign storage, secrets, backup, plugins, webhooks, DBaaS… (the bulk of the surface). |
| `peer.py` | `/peer/*` | Core P2P protocol: profile, service requests/grants, DMs, usage receipts, attachment pull, join/callback. |
| `groups.py` | `/local/groups/*` | Group CRUD (create/join/invite/roles/members). |
| `group_peer.py` | `/peer/group/*` | Peer-to-peer group handshake — join requests, decisions, grant exchange. |
| `pair_invites.py` | `/local/pair/*` | Pair-invite token API (issue + redeem `nxg://pair#…`). |
| `relay_admin.py` | `/local/relay/*` | Control the in-process local relay + relay-module admin (all `verify_local_auth`-gated). |
| `diagnostics.py` | `/health`, metrics | Health/diagnostics router. |
| `events.py` | `/local/events/stream` | Server-Sent Events stream the UI subscribes to. |
| `websocket.py` | `/peer/ws`, `/local/ws` | Peer worker WS (token-auth) + UI live-feed WS (private-net + token). |
| `network_cache.py` | — | Single-process cache for the heavy `/local/network` response. |
| `schemas.py` | — | Pydantic request/response models shared by the routers. |
