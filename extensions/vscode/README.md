# NexusGrid for VS Code

Drive a local [NexusGrid](https://github.com/Tharunvipperla/NexusGrid-grid) node
from your editor — dispatch tasks, run multi-step pipelines, deploy services, and
deposit/retrieve encrypted files, all from the files you're already working in.

It's a thin client of the node's management API (`/local/*`). The web control panel
is just another client of that same API — so this extension and the web UI stay in
sync automatically (see [One node, two windows](#one-node-two-windows)).

> Tracking issue: [#8](https://github.com/Tharunvipperla/NexusGrid-grid/issues/8).
> Collaboration welcome.

## Features

- **Run on NexusGrid** — right-click any file, several selected files, or a folder
  in the Explorer and run it on the grid. The selection is zipped and sent as the
  task's workspace, just like a build context in the web UI.
- **Pipelines (DAG)** — define a multi-step workflow in **`nexus.dag.json`** and run
  it with one click; steps fan out and respect their `depends_on` edges.
- **Services** — define a long-running service (redis, postgres, your own image) in
  **`nexus.service.json`** and deploy it; running services get **Start/Stop** tunnel
  actions in the tree.
- **Foreign storage** — right-click a file to **deposit** it (encrypted) onto a
  peer's disk, and **retrieve** your deposits back from the tree. Only you hold the
  password — the host never sees it.
- **Set Target…** — pick a real worker or group (or Auto) from a list; the choice is
  written into your config so you can see and reuse it.
- **Toggle Node Setting…** — flip node settings (online, offer GPU, cache venvs,
  accept deposits) from the editor; they reflect in the web UI live.
- **Inline dispatch (CodeLens + Nexus Lens hover)** — files with a `@nexus:` directive
  get a clickable **Dispatch to …** above the line, and hovering shows the resolved
  requirements and which connected workers fit.
- **NexusGrid view** — connection status, your recent tasks (with live **Logs**,
  **Stop**, **Requeue**, **Open Artifacts**), and a **Deposits** section.
- **Open Control Panel** — open the full web UI in your browser, pre-authenticated,
  for everything that belongs on a dashboard rather than in an editor.

## Config profiles

Each kind of work is a small JSON file at your workspace root. The first time you
trigger one, the extension creates it with an example and opens it for review (it
won't run until you trigger it again). They're plain JSON — edit by hand or from a
terminal.

### Dispatch — `nexus.json`

```json
{
  "image": "python:3.11-slim",
  "runtime": "docker",
  "command": "python main.py",
  "target": "auto",
  "gpu": false
}
```

| Field | Values |
|---|---|
| `image` | container image |
| `runtime` | `docker` · `native` · `wasm` |
| `command` | what to run. Right-clicking a single `.py`/`.js`/`.sh` derives the command from that file. |
| `target` | `auto` (best fit) · a worker IP · `group:<id>` — or set it with **Set Target…** |
| `gpu` | `false` · `true` (whole GPU) · a GPU count |

#### Per-file directives — `@nexus:`

Add a comment to the file you dispatch to override `nexus.json` for that run:

```python
# @nexus: gpu, ram>=16, cpu=50, runtime=docker, isolation
def train(): ...
```

| Directive | Effect |
|---|---|
| `gpu` / `gpu=N` | require a GPU worker + pass through the GPU (count `N` or whole card) |
| `ram=16` / `ram>=16` | container memory limit, in GB (enforced) |
| `cpu=50` | container CPU limit, percent |
| `image=...` | override the image |
| `runtime=docker\|native\|wasm` | override the runtime |
| `target=auto\|<ip>\|group:<id>` | override where it runs |
| `priority=N` | dispatch priority (0–100) |
| `isolation` · `no-cache` · `scan` | per-task venv isolation / skip cache / scan |

Anything unrecognized is reported, never silently ignored.

### Pipeline — `nexus.dag.json`

A multi-step DAG. Either a bare array of steps, or an object that also carries
workflow-level targeting. A **Run pipeline (N steps)** CodeLens sits at the top.

```json
{
  "preferred_workers": [],
  "target_groups": [],
  "require_gpu": false,
  "steps": [
    { "id": "prep",      "image": "python:3.11-slim", "entrypoint": "python prep.py",      "depends_on": [] },
    { "id": "aggregate", "image": "python:3.11-slim", "entrypoint": "python aggregate.py", "depends_on": ["prep"] }
  ]
}
```

Each step honors `id`, `image`, `runtime`, `entrypoint`, `depends_on`, and the same
resource keys as the dispatcher (`ram_limit`, `cpu_limit`, `gpu`, `setup_cmd`, …).

### Service — `nexus.service.json`

One long-running service. A **Deploy `<image>`** CodeLens sits at the top; once a
worker runs it, it appears in the NexusGrid view with Start/Stop.

```json
{
  "image": "redis:7",
  "entrypoint": "",
  "expose_ports": [6379],
  "service_kind": "tcp",
  "environment": {},
  "target": "auto"
}
```

## One node, two windows

The extension and the web control panel are both clients of the **same node**, so
they stay in sync automatically: a task you dispatch from VS Code appears in the web
UI, a setting you toggle from VS Code shows there too — and changes made in the web
UI refresh the VS Code view live (the extension subscribes to the node's event
stream). The extension never keeps its own state; it only drives the node's real
API, so everything flows through one source of truth.

For everything that belongs on a dashboard rather than in an editor — chat, peers,
the full storage lifecycle, backups, plugins — use **Open Control Panel**.

## Setup

1. Start a node: `python -m nexus` (defaults to `https://127.0.0.1:8000`).
2. The extension authenticates with the node's local API token — **usually with no
   setup**. It finds `.nexus_local_token` automatically by searching your workspace
   folder(s) and their parents (and the node's working directory). Only if your node
   lives somewhere unrelated do you need to set **`nexusgrid.nodeDir`** (the node's
   directory) or paste the token into **`nexusgrid.token`**.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `nexusgrid.baseUrl` | `https://127.0.0.1:8000` | Node base URL. |
| `nexusgrid.token` | _(empty)_ | Local API token; if empty, read from `.nexus_local_token`. |
| `nexusgrid.nodeDir` | _(empty)_ | Directory holding `.nexus_local_token`; defaults to the first workspace folder. |
| `nexusgrid.codeLens` | `true` | Show the inline CodeLens on `@nexus:` lines and profile files. |

## Develop

```bash
cd extensions/vscode
npm install
npm run compile      # or: npm run watch
```

Press **F5** in VS Code to launch an Extension Development Host. The build bundles
with [esbuild](https://esbuild.github.io/) into a single `out/extension.js`. Package
with [`vsce`](https://github.com/microsoft/vscode-vsce):
`npx @vscode/vsce package --no-dependencies` (deps are already bundled).

## Notes

- The loopback node serves a self-signed TLS cert, so the client does not verify
  the cert for the local node. Point it only at nodes you control.
