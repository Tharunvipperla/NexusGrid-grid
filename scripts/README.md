# scripts/

Developer / operator helper scripts — **not** part of the shipped app or the
import graph. Run them by hand during development or testing.

| Script | What it does |
|---|---|
| `run_dev.bat` | Dev launcher: runs the backend from source with `uvicorn --reload` (auto-cd's to the repo root). e.g. `scripts\run_dev.bat 8000`. |
| `redeploy-test-nodes.ps1` | Spin up / refresh a few local test nodes (PowerShell). |
| `wave6_cloud_eviction_e2e.py` | End-to-end check of the cloud-eviction tier (needs a real GDrive credential). |
| `wave7_preview_e2e.py` | End-to-end check of the in-browser preview flow. |
| `wave9_task_data_e2e.py` | End-to-end check of cloud task-data sources. |

These are heavier, dependency-gated manual end-to-end checks; the header
comment in each script documents how to run it. The unit/integration suite
lives in [`../tests/`](../tests/).
