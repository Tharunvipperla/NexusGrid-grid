# Services

**Sidebar → Use the grid → Services.** Host long-running services (web apps, APIs,
databases) on your node or a peer's, advertise them to your groups, and manage who
may use them. Also discover and request services others host.

---

## Hosting a service

Define a service with:

| Field | Meaning |
|---|---|
| **Service name** | A label, e.g. `BigLLM`. |
| **Version (optional)** | Free-form version string. |
| **Access** | **free** (auto-approved for known peers), **permission** (you approve each request), or **paid** (reserved / not enabled). |
| **One-line description** | Shown in discovery. |
| **Tags (comma-separated)** | Capability tags, e.g. `redis, sql, gpu`. |
| **Local host / Local port** | Where the service actually runs on the host machine (this is host-only and never advertised). |
| **Pump (optional)** | A `nexus_pumps/` module that transforms the traffic. |

### How the service runs
A service is backed by a run-spec, like a task:
- **Container image** (e.g. `ollama/ollama:latest`), **Command**, **Ports (CSV)**,
  **Env (CSV, KEY=VAL)**.
- **GPU** — a toggle that gives the service the host's GPU (NVIDIA, via `--gpus`);
  on a multi-GPU host it becomes a slider to pick how many. Disabled when the host
  has no GPU. **Sharing is not throttled** — the service gets the full card; there
  is no enforced GPU % cap on consumer hardware (the "Advertised GPU VRAM" setting
  in Local Config is a scheduling hint, not a runtime cap). This is what lets one
  person host an LLM/render/transcode service on their GPU that everyone else uses
  without a GPU of their own.
- **Custom build — Dockerfile (optional)** — build the image from a base on your
  allowlist instead of pulling a prebuilt one.
- **Cloud inputs (optional)** — fetch files (http(s) / rclone) before launch.
- **Components** — compose a multi-part service (name, protocol, local port, tags).
- **Details (markdown)** — a readme shown to consumers.

Use `secret://NAME` in env to inject a value from your [secrets vault](local-config.md)
rather than putting a secret in the spec.

---

## DBaaS — one-click databases

For a **database service**, NexusGrid can run the engine for you:
- **Service kind** / **Provider engine** — `postgres`, `mysql`, `redis`, `mongo`.
- **Start a local engine** — one click brings up the engine container on a free
  loopback port and fills in the **Admin DSN**
  (`postgresql://admin:pw@127.0.0.1:5432/postgres`).
- On an approved access grant, NexusGrid **provisions a per-consumer database +
  login** automatically and hands the consumer its connection — each consumer is
  isolated to its own database/user.

---

## Access grants

When another member requests your service:
- **free** services auto-approve.
- **permission** services queue the request (you'll see it in the bell) for you to
  **approve** or **deny**.
- You can **revoke** a grant at any time; revocation tears down the consumer's
  tunnel and (for DBaaS) drops the provisioned database + login.

Access is enforced cryptographically — a request is bound to the requester's group
pubkey (not a guessable identifier), and the data-plane tunnel checks for a valid
grant before carrying any traffic.

---

## Discovering & using others' services

Discover services your peers/groups advertise, request access, and — once granted
— reach them over a secure tunnel. Services you've been granted also appear under
**Services in use** in [Task Telemetry](telemetry.md).

---

## Replication (cookbooks)

A provider can mark a service **replicable**; a consumer can then copy its public
**cookbook** (a recipe describing the service) to run their own instance. The
cookbook contains only public fields — never the host's local target — and you run
and sandbox it yourself.
