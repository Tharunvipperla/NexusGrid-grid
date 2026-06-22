# Hosting Services on NexusGrid

This guide is for a **node operator** who wants to expose a service (a local LLM,
a database, an API, a web app — anything that speaks TCP) so other nodes on the
grid can use it, safely and on your terms.

It covers, end to end: what a service is, how to deploy one, how the data plane
(the tunnel + pump) works, **how to run a service in a sandbox**, how to make the
access (and the link itself) more secure, how to write your own pump, and how to
build pipelines out of services.

---

## 1. What a service is

A service is a few structured fields plus one free-form doc:

| field | purpose |
|-------|---------|
| `name` | shown in the registry |
| `description` | one-line summary for the list |
| `version`, `tags` | metadata / search (e.g. `redis`, `gpu`, `openai`) |
| `access` | `free` (auto-approve), `permission` (you approve each user), `paid` (reserved) |
| `readme` | **markdown** — you define *everything else* here (how to connect, what it's built on, a recipe to self-host, links, license) |
| `local_host` / `local_port` | **host-only** routing target the tunnel dials. **Never leaves your machine.** |
| `pump` | optional name of a custom byte processor (see §6). Blank = the default forwarder. |

Only `local_host`/`local_port` are private; everything else is what peers see.

**Two ways to back a service:**
1. **You run it, NexusGrid tunnels to it.** Start the process yourself bound to
   loopback and point `local_host`/`local_port` at it (§2). NexusGrid only moves
   bytes — how the process runs (including any GPU) is entirely your own setup.
2. **NexusGrid launches it from a run-spec.** Instead of running it yourself, give
   the service a **run-spec** and NexusGrid (or a peer allowed to replicate it)
   starts it in a sandbox:

   | run-spec field | purpose |
   |---|---|
   | `image` / `command` | container image to pull + command to run |
   | `ports` | container ports to expose (mapped to a loopback port) |
   | `env` | `KEY=VAL` pairs; use `secret://NAME` to pull from your vault |
   | **`gpu`** | give the container the host GPU(s). **NVIDIA** via `--gpus`; **AMD/ROCm** via `/dev/kfd` + `/dev/dri` device mounts + render/video groups (chosen automatically from the host's GPU vendor). In the UI it's a toggle — a count slider on multi-GPU NVIDIA hosts (AMD exposes all). **Not throttled**: the container gets the full card; there's no enforced GPU % cap on consumer hardware. Blank = CPU-only. |
   | `build` | a Dockerfile to build locally (FROM a base on your allowlist) instead of pulling |
   | `inputs` | files (http(s)/rclone) fetched before launch |

The `gpu` field is what makes the **self-hosted-LLM-for-a-group** pattern work:
one person with a GPU hosts the model; everyone they trust uses it over the tunnel
with **no GPU and nothing to install** (see the examples in §2).

---

## 2. Deploy a service (the happy path)

1. **Run your service locally**, bound to loopback. Example — a local LLM:
   ```
   ollama serve            # listens on 127.0.0.1:11434
   ```
2. In NexusGrid → **Services → Deploy a service**:
   - **Name:** `LlamaServe`  ·  **Access:** `Permission`  ·  **Tags:** `llm, gpu, openai`
   - **Local host / port:** `127.0.0.1` / `11434`
   - **Details (markdown):**
     ```
     ## How to connect
     Point any OpenAI-compatible client at the local address you get after Connect.
     Model: llama3

     ## Recipe (self-host instead)
     ```
     docker run -d -p 11434:11434 ollama/ollama
     ```
     ```
   - **Save.**

A connected peer can now find it under **Discover**, request access, and — once
you **Approve** it in *Access requests* — open a tunnel and use it.

### Consuming it (the other node)
Discover → click the service → **Connect** tab → **Request access** → (host
approves) → **Connect**. You get a local address like `127.0.0.1:52311`; point
your own client at it:
```
curl http://127.0.0.1:52311/v1/chat/completions \
  -d '{"model":"llama3","messages":[{"role":"user","content":"hi"}]}'
```
Disconnect when done — the session (seconds + bytes) is billed as
counterparty-signed receipts visible in both nodes' **Usage** tab.

---

## 2b. Example services — deploy & use

Each example shows **what the host fills in** (Services → Deploy a service) and
**what the consumer runs** after Connect gives them a local `127.0.0.1:<port>`.
The first two use the run-spec's **GPU** field; the rest are CPU services.

### Example 1 — A shared LLM on your GPU (Ollama)
*The flagship case: you have the GPU, your group doesn't need one.*

**Host deploys** (run-spec):
| field | value |
|---|---|
| Container image | `ollama/ollama` |
| Ports | `11434` |
| **GPU** | `all` |
| Access | `permission` · Tags | `llm, gpu` |

Load a model once after it starts (from the host, against the running container):
```
ollama pull llama3        # or bake it in with a Dockerfile + entrypoint
```

**Consumer uses** (after Connect → `127.0.0.1:<port>`):
```
curl http://127.0.0.1:<port>/api/generate \
  -d '{"model":"llama3","prompt":"Explain RAID 5 in two lines","stream":false}'
```
…or point any OpenAI-compatible app (Open WebUI, Continue, your code) at
`http://127.0.0.1:<port>/v1`. The model runs on the host's GPU; the consumer
never installs a model or owns a GPU.

### Example 2 — Image generation on your GPU (ComfyUI / Stable Diffusion)
**Host deploys:** image = your ComfyUI/SD image · Ports `8188` · **GPU** `all`.
**Consumer uses:** Connect, then open `http://127.0.0.1:<port>` in a browser —
the whole web UI is rendering on your peer's GPU.

### Example 3 — A PostgreSQL database (no GPU, one-click)
**Host deploys:** Service kind `postgres` → **Start a local engine** (auto-fills
the Admin DSN) · Access `permission`. On each approved grant NexusGrid provisions
a **per-consumer database + login** automatically.
**Consumer uses:**
```
psql -h 127.0.0.1 -p <port> -U <issued-user> <issued-db>
```
Each consumer is isolated to its own database/login; revoke drops both.

### Example 4 — A Redis cache (no GPU)
**Host deploys:** image `redis` · Ports `6379` (or Service kind `redis` via DBaaS).
**Consumer uses:**
```
redis-cli -p <port>
```

### Example 5 — Your own web API (no GPU, custom build)
*Ship code, not just a prebuilt image.*
**Host deploys:** paste a Dockerfile in **Custom build** (its `FROM` base must be
on your allowed-images list) · Ports `8000` · Env `API_KEY=secret://my_api_key`
(pulled from your vault, never written in the spec).
**Consumer uses:**
```
curl http://127.0.0.1:<port>/health
```

> In every case the consumer talks to `127.0.0.1:<port>` as if the service were
> local — NexusGrid tunnels it to the host over the authenticated peer link. They
> never learn the host's real address, and access stops the instant you revoke.

### GPU sharing — what's capped (and what isn't)
NVIDIA and AMD are both supported: the node picks the right mechanism from the
host's GPU vendor (NVIDIA `--gpus`, or AMD/ROCm `/dev/kfd` + `/dev/dri` mounts).
The **native** runtime works for either with no flags — a host process sees the
card directly. AMD ROCm containers may need extra host setup (driver group
membership, sometimes `seccomp=unconfined`); that's the host's to configure.

Be clear-eyed about this: GPU passthrough gives the service the **whole card**.
There is **no enforced GPU limit** — Docker has no GPU-% flag, and hardware
partitioning (MIG) is datacenter-only. The "Advertised GPU VRAM" setting in Local
Config is a *scheduling hint*, **not** a runtime cap. RAM and CPU ceilings *are*
enforced (Docker `mem_limit`/`cpu_quota`; native via OS limits) — GPU is the
exception. To keep headroom on a personal machine, cap at the **framework** level
(set the model server's VRAM env in the service's **Env** field, or run a smaller /
quantized model), limit concurrency, or lower the card's power cap (`nvidia-smi -pl`).

---

## 3. How the data plane works (tunnel + pump)

When a consumer connects, NexusGrid stands up a `127.0.0.1:<port>` listener on
*their* machine and tunnels raw bytes to *your* service:

```
their client → their 127.0.0.1:<port> ──svc_data frames──→ your node → 127.0.0.1:<your port>
                                       ←──svc_data frames──
            (frames ride the authenticated peer link: LAN /peer/ws or relay)
```

The **pump** is the loop that moves bytes between the two TCP sockets, 32 KB at a
time. Because it forwards bytes *beneath* the application protocol, the tunnel is
protocol-agnostic — HTTP, HTTPS, gRPC, Postgres, Redis, SSH all just flow.

**Built-in guarantees (you don't have to configure these):**
- Every byte requires an **approved grant issued to that exact peer** — nobody
  can ride someone else's access.
- Your node only ever dials the **named target you configured** — never anything
  the consumer sends, so a peer can't turn your node into an open proxy / reach
  into your LAN (no SSRF).
- **Revoke cuts live connections instantly.**
- Usage is **consumer-signed**, so the numbers can't be faked.

---

## 4. Recommended ways to host — security

NexusGrid secures the *transport and access*. **You** are responsible for the
service behind it. Treat an exposed service like exposing it to the internet.

### 4.1 Bind to loopback, sandbox the process
Run the service as an unprivileged, isolated process. Docker is the easy button:

```
# Read-only FS, no extra caps, memory/CPU caps, loopback-only publish,
# no outbound network unless the service truly needs it.
docker run -d --name llama \
  --user 1000:1000 \
  --read-only --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 --memory 8g --cpus 4 \
  --network none \                 # or a locked-down bridge if it needs egress
  -p 127.0.0.1:11434:11434 \       # publish ONLY to loopback
  ollama/ollama
```
Key points:
- **Publish to `127.0.0.1` only** (`-p 127.0.0.1:PORT:PORT`). The tunnel reaches
  it from your own node; the wider network never sees the port.
- **Drop privileges and capabilities** (`--user`, `--cap-drop ALL`,
  `--no-new-privileges`), make the root FS read-only, cap memory/CPU/PIDs.
- **Cut the network** (`--network none`) unless the service needs egress; if it
  does, use an allow-listed egress proxy, not the host network.
- Without Docker: a dedicated low-priv user + `systemd` hardening
  (`ProtectSystem=strict`, `PrivateTmp=yes`, `NoNewPrivileges=yes`,
  `IPAddressDeny=any` + an allow-list, `MemoryMax=`), or a firejail/nsjail wrapper.

### 4.2 Use `permission` access + least privilege
- Default to **`permission`** so you approve each consumer; reserve `free` for
  genuinely public, low-risk services.
- Expose the **narrowest service** possible — a read-only replica, a scoped API
  key baked into the service, a model with no tool access. Assume a consumer can
  send *anything* the protocol allows to the port you opened.
- **Revoke** the moment a grant is no longer needed; it kills live streams.

### 4.3 Make the link itself more secure
The tunnel forwards bytes; it does not add auth or encryption *to your service*.
So:
- **Put auth in front of the service.** Require an API key / token at the app
  layer (the consumer's client sends it). Don't rely on "it's only on the grid".
- **Terminate TLS at the service** if you want the consumer↔service payload
  encrypted independently of the grid link (the grid link is already over the
  authenticated peer channel, but app-level TLS protects against a buggy pump or
  a future multi-hop path). Run the service with HTTPS and have consumers use
  `https://127.0.0.1:<port>` (self-signed is fine; document the fingerprint in
  the readme).
- **Scope and rate-limit** at the service or via a custom pump (§6) — cap request
  rate, body size, or restrict paths.
- **Never bake real secrets** into the `readme` (it's public). Put credentials in
  the running service; share only what a user needs to connect.

### 4.4 Trust boundaries — know what's guaranteed
- **Guaranteed by NexusGrid:** only approved, correctly-scoped peers reach your
  named port; usage is signed; revoke is immediate; no SSRF.
- **Your job:** the service is hardened/sandboxed, authenticated, and assumes
  hostile input. A consumer with access can do to your service whatever the
  protocol on that port allows.

---

## 5. Cookbook / "copy it to my machine"

Sometimes you'd rather a peer **run their own copy** than use yours. NexusGrid
doesn't auto-deploy anything — instead, put the recipe in the `readme`
(a `docker run`, a compose file, a GitHub link). The consumer sees it on the
detail page and runs it **at their own responsibility**, in their own sandbox.
This keeps "replication" simple and safe: it's a shared recipe, not remote code
execution.

---

## 6. Writing your own pump (the editable byte processor)

The default pump forwards bytes unchanged — the best general default. For your
own service you can ship a smarter one: compress, redact, inject/strip headers,
rate-limit, or log. A pump runs **only your code, on your machine**, on the
provider side of your service.

Drop a file in a `nexus_pumps/` directory next to your node and register a pump:

```python
# nexus_pumps/redact.py
from nexus.runtime.service_tunnel import register_pump

def _factory():
    # direction is "to_consumer" (bytes leaving your service) or
    # "to_provider" (bytes going in). Return bytes, or None to drop the chunk.
    def transform(direction, chunk: bytes) -> bytes:
        if direction == "to_consumer":
            return chunk.replace(b"internal-secret", b"[redacted]")
        return chunk
    return transform

register_pump("redact", _factory)
```

Then set the service's **Pump** field to `redact`. Leave it blank (or `default`)
for the plain forwarder.

Guidelines:
- Keep `transform` fast and non-blocking — it runs in the byte path.
- It's per-chunk; don't assume a chunk is a whole request/line (buffer in a
  closure if you need framing).
- On any exception the node falls back to the default and logs it.
- This is **host-trusted code**; only put files you wrote/reviewed in
  `nexus_pumps/`.

Good uses: gzip/deflate, header injection (`Authorization`), redaction of
sensitive fields, simple per-stream rate-limit/quota, audit logging.

---

## 7. Pipelines (composing services)

You can chain services today by pointing one service's target at another local
service, or by having a small local gateway that fans out:

```
consumer → [your "Gateway" service :8080] → nginx/your code →┬→ 127.0.0.1:11434 (LLM)
                                                             ├→ 127.0.0.1:5432  (db)
                                                             └→ 127.0.0.1:6379  (cache)
```

Expose only the gateway; keep the components on loopback. A **composite service**
type (one service that bundles several sub-services) is the next first-class
feature — until then, a gateway service is the recommended pattern.

---

## 8. Checklist before you publish a service

- [ ] Service bound to `127.0.0.1` only.
- [ ] Running sandboxed (container/dedicated user, dropped privileges, FS + net
      + resource limits).
- [ ] Access set to `permission` (unless intentionally public).
- [ ] App-level auth in front of the service; no reliance on grid-only reachability.
- [ ] No secrets in the `readme`.
- [ ] Considered a custom pump for rate-limit / redaction / TLS-pinning hints.
- [ ] Know how to **revoke** and watch **Usage**.

---

## 9. Metering

On disconnect, the consumer signs `kind=service` (connection-seconds) and
`kind=service_bytes` receipts and pushes them to you. Both nodes' profiles show
the verified totals — *service time served/used*, *bytes served/used*, and the
*number of distinct users* you served. Numbers are counterparty-attested, so
neither side can inflate them.
