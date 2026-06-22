# NexusGrid

**A peer-to-peer compute and storage grid for you and the people you trust.**

NexusGrid pools the compute and storage of machines *you* and people you trust own
— no central server, no account, no cloud middleman. Each machine runs one node
(a local web app on `127.0.0.1`); nodes find each other on your LAN or through a
relay you control, and authorize each other by cryptographic group identity, not
by IP. Tasks, files, services, and databases move directly between trusted peers,
encrypted end to end.

It's a single self-contained app: a Python/FastAPI backend serving a React control
panel, packaged into one binary with no runtime dependencies for end users.

## Download

[![Get the latest release](https://img.shields.io/badge/Download-latest%20release-2ea44f?style=for-the-badge)](https://github.com/Tharunvipperla/NexusGrid-releases/releases/latest)

Grab the build for your OS from the **[latest release](https://github.com/Tharunvipperla/NexusGrid-releases/releases/latest)**. Once installed, every node keeps itself current through cryptographically signed auto-updates.

| OS | Download | How to run |
|---|---|---|
| **Windows** | `NexusGrid-Setup-<ver>.exe` | Run the installer (wizard + Start-Menu entry + uninstaller). |
| **macOS** | `NexusGrid-macos` | A bare binary, no installer. See the note below. |
| **Linux** | `NexusGrid-linux` | A bare binary, no installer. See the note below. |

> **Windows:** not yet code-signed, so SmartScreen may warn "unknown publisher" — choose **More info → Run anyway**.
>
> **macOS / Linux** have **no installer** — just a single binary. Put it in its **own folder** (it creates helper files next to itself when run) and launch it from there:
> ```bash
> mkdir nexusgrid && mv NexusGrid-macos nexusgrid/NexusGrid && cd nexusgrid
> chmod +x NexusGrid          # make it executable
> ./NexusGrid --port 8000
> ```
> If macOS Gatekeeper blocks it ("unidentified developer"), right-click → **Open** once, or run `xattr -d com.apple.quarantine ./NexusGrid`. The binaries aren't notarized/code-signed yet, so this first-run friction is expected — running it from its own folder is the reliable path.

Verify any download against the `sha256` recorded in the release's `manifest.json`.

## What you can do
- **Run tasks & workflows** — dispatch jobs (incl. multi-step DAGs) to worker nodes, with sandboxing, leases, retries, and per-step worker targeting.
- **Host services & databases** — expose long-running services, and provision databases on demand (DBaaS).
- **Foreign storage** — deposit encrypted data on peers' spare disk; only you hold the keys (Argon2id + AEAD).
- **Connect privately** — groups with Ed25519 identity, private messaging, and relays that punch through NAT/firewalls.
- **Extend & integrate** — drop-in plugins (relays, runners, pumps, DB providers), a local REST API + Python SDK/CLI, and outbound webhooks.
- **Operate with confidence** — live telemetry, encrypted backup/restore, and cryptographically **signed auto-updates**.

## Quick start (from source)

```bash
pip install -r requirements.txt
python -m nexus --port 8000
```

Open the UI at `http://127.0.0.1:8000/`. A second node on the same machine:

```bash
python -m nexus --port 8001 --peers 127.0.0.1:8000
```

The node stores its data (identity keys, database, caches) in a per-user app-data
folder for packaged builds, or the working directory when run from source —
override with `--data-dir` / `NEXUS_DATA_DIR`.

**Optional extras** — only for the Google Drive cloud-eviction tier and cloud
task-data sources; the app runs fine without them (the driver imports them lazily):

```bash
pip install "google-api-python-client>=2.150.0" "google-auth>=2.40.0"
```

## Developer mode (hot reload)

```bash
scripts\run_dev.bat 8000      # Windows; cd's to the repo root, uvicorn --reload
```

Edits under `nexus/` trigger a reload — no rebuild needed.

## Building a standalone app

```cmd
build\build.bat               :: Windows  -> dist\NexusGrid.exe
./build/build.sh              # Linux / macOS
```

Then optionally wrap it in a Windows installer (wizard + uninstaller):
`build\build_installer.bat` (needs [Inno Setup](https://jrsoftware.org/isdl.php)).
See [`build/README.md`](build/README.md).

## Repository layout

| Folder | What it holds |
|---|---|
| [`nexus/`](nexus/README.md) | The application package (backend). See the per-subpackage map below. |
| [`webui/`](webui/README.md) | The React control-panel SPA (esbuild → `dist/bundle.js`). |
| [`docs/`](docs/README.md) | User guide + developer guide + in-depth feature guides. |
| [`tests/`](tests/README.md) | The pytest suite (the regression net; one test per change). |
| [`scripts/`](scripts/README.md) | Dev/ops helper scripts (`run_dev.bat`, e2e checks). |
| [`tools/`](tools/README.md) | Maintainer tooling not shipped in the app (release signing). |
| [`build/`](build/README.md) | PyInstaller spec + build/installer scripts. |
| [`deploy/`](deploy/README.md) | Deployment artifacts (relay Dockerfile + compose). |
| [`release/`](release/README.md) | Release process, manifest template (signing keys stay offline). |

Root files: `README.md`, `pyproject.toml`, `requirements.txt` (read by
`pyproject.toml`), `.gitignore`.

### The `nexus/` package (layered)

Start with [`nexus/README.md`](nexus/README.md) for the dependency map, then open
the subpackage you care about:

| Subpackage | Owns |
|---|---|
| `nexus/core/` | Config, constants, identity, paths, shared state, pub/sub bus |
| `nexus/storage/` | SQLite schema, ORM models, session factory |
| `nexus/security/` | Auth deps, Ed25519/ECIES/AEAD crypto, signed updates, tokens, threat scanner |
| `nexus/utils/` | Leaf helpers (time, hashing, text, net) |
| `nexus/tasks/` | Task lifecycle, queue, lease, metadata, step targeting, DAG resume |
| `nexus/caches/` | venv / pip / node caches + workspace dependency scanning |
| `nexus/runtime/` | Task/service execution, DBaaS, foreign storage, relays, groups, plugins |
| `nexus/scheduler/` | Worker fitness, selection, retry, reliability, DAG resolution |
| `nexus/networking/` | LAN discovery, peer protocol, relay/worker-client loops, tunnels |
| `nexus/telemetry/` | Logs, metrics, alerts, audit, presence, hardware sampling |
| `nexus/api/` | FastAPI routers (`/local`, `/peer`, groups, events, WebSockets) |
| `nexus/ui/` | SPA serving (token-injected), avatar endpoint, broadcaster |
| `nexus/relay/` | The standalone relay server (also deployable on its own) |
| `nexus/sdk/` | Thin Python client + OpenAPI-driven CLI for the local API |

## Documentation

Start at **[`docs/`](docs/README.md)**:
- **Users** → [user guide](docs/README.md) + [screen-by-screen reference](docs/user/screens/).
- **Developers** → [developer guide](docs/dev/README.md), [architecture](docs/dev/), [security model](docs/dev/security-model.md), [build & test](docs/dev/build-test.md).
- **Feature deep-dives** → [`docs/guides/`](docs/guides/README.md).

## Contributing

1. Fork and branch off `main`.
2. `pip install -e .[test]` then `python -m pytest -q` — the suite must stay green.
3. Ship a test with every change (see [`tests/README.md`](tests/README.md)).
4. Match the existing style; keep changes surgical. Open a PR against `main`.

## Releasing & security

Releases are **cryptographically signed** (root key → per-release delegation cert →
binary hash) and verified by every node before an auto-update. Signing keys are
kept offline. See [`release/RELEASING.md`](release/RELEASING.md). Report security
issues privately to the maintainer.
