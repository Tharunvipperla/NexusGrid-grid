# Deploying a Nexus Relay

A **relay** is a small public WebSocket server that lets Nexus nodes on
different networks reach each other. Nodes always connect *outbound* to
it, so a relay on a public host works through home NAT, CGNAT, and
corporate firewalls alike — that is what makes group traffic "just work"
across regions.

You need a relay that is **publicly reachable**. A relay running on a
laptop behind NAT can only be reached on its own LAN. For production,
run it on a host with a public address (a cloud VM, a DMZ box, or a
PaaS) — or expose a local one with a tunnel.

The relay only ever sees opaque, AEAD-encrypted frames (see
`RELAY_ARCHITECTURE_PLAN.md`); it cannot read group content.

## What you need

- A host reachable from the internet (cloud VM, DMZ server, or PaaS).
- A **grid key** — a shared secret. Every node that uses this relay,
  and the relay itself, must be configured with the *same* key.
- Ideally TLS on port **443** so the connection looks like normal HTTPS
  and traverses corporate firewalls / CGNAT.

## Option A — Docker (recommended)

Run these from the repository root (the build context must include
`nexus/relay/server.py`):

```sh
# Build + run with compose:
NEXUS_GRID_KEY=your-shared-secret \
  docker compose -f deploy/docker-compose.relay.yml up -d

# …or plain docker:
docker build -f deploy/Dockerfile.relay -t nexus-relay .
docker run -d -p 9000:9000 -e NEXUS_GRID_KEY=your-shared-secret nexus-relay
```

## Option B — directly with Python

```sh
pip install "fastapi>=0.110" "uvicorn[standard]>=0.29"
# from the repository root:
NEXUS_GRID_KEY=your-shared-secret PORT=9000 python nexus/relay/server.py
```

## Putting it on 443 (recommended for production)

`nexus/relay/server.py` serves plain HTTP/WS. Terminate TLS in front
of it so nodes connect as `wss://relay.example.com` (port 443):

- **Reverse proxy** — nginx / Caddy / Traefik terminating TLS on 443 and
  proxying to the relay's port. Caddy example:

  ```
  relay.example.com {
      reverse_proxy localhost:9000
  }
  ```

- **PaaS** — Render, Fly.io, Railway, etc. give you an HTTPS URL
  automatically; just deploy the container and set `NEXUS_GRID_KEY`.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `NEXUS_GRID_KEY` | `nexus-beta-key` | Shared secret — **must** be set to a real value. |
| `NEXUS_RELAY_REQUIRE_NON_DEFAULT_KEY` | `false` | If `true`, the relay refuses to start with the default key. |
| `PORT` | `9000` | Port the relay listens on. |
| `NEXUS_RELAY_HEARTBEAT_TIMEOUT` | `30` | Seconds before an idle node is evicted. |
| `NEXUS_RELAY_RATE_LIMIT_MAX_MSGS` | `60` | Messages per window per node. |
| `NEXUS_RELAY_CORS_ORIGINS` | _(none)_ | Comma-separated allowed CORS origins. |

## Pointing a group at the relay

1. In each node: **Settings → Internet Relay** → set the **Relay Server
   URL** (`wss://relay.example.com`) and the matching **Grid Key**.
2. When **creating a group**, list one or more relay URLs in the
   *Relay servers* field — or add them later from the group's **Relays**
   tab (**+ Add Relay**).
3. The **Relays** tab probes each binding and shows green/red status.

## Multi-region / high availability

A group can be bound to **several relays at once**. Members fan every
frame out to all of them and de-duplicate on receipt, so one relay going
down does not interrupt the group. For production, run 2–3 relay
instances in different regions and add all of their URLs to the group.
