"""Wave 9 cloud task-data sources end-to-end script.

Drives a real two-node depositor-master + worker setup:

  1. Depositor saves a GDrive service-account credential.
  2. Depositor accepts the task-data IP/copyright terms (one-time).
  3. Depositor dispatches a workflow whose only task uses ``workspace_source``
     pointing at a Drive folder containing ``main.py`` + ``data.csv``.
  4. Worker fetches the folder, extracts it as the workspace, runs
     ``python main.py`` which reads ``data.csv`` and writes
     ``result.txt`` containing the row count.
  5. Script polls the master until the task reaches ``finished``,
     downloads the result, and verifies the row count matches expectations.

Prerequisite Drive folder layout (create manually before running):

    <WAVE9_GDRIVE_FOLDER_ID>/
        main.py     # tiny script: reads data.csv, writes result.txt
        data.csv    # any small CSV; row count returned via result.txt

Suggested ``main.py`` body:

    import csv, pathlib
    rows = list(csv.reader(open("data.csv")))
    pathlib.Path("result.txt").write_text(f"rows={len(rows)}")

Required env vars:
  WAVE9_GDRIVE_SA_JSON_PATH   path to a service-account JSON file
  WAVE9_GDRIVE_FOLDER_ID      Drive folder id shared with the SA email

Usage:
  python scripts/wave9_task_data_e2e.py \
      --master  http://127.0.0.1:8000 \
      --master-token $TOKEN_A \
      --worker-ip    192.168.1.42:9000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


def _step(label: str) -> None:
    print(f"[wave9-e2e] {label}")


def _api(base: str, token: str, method: str, path: str, *, json_body=None, files=None, data=None) -> dict:
    headers = {"X-Local-Token": token}
    with httpx.Client(timeout=180.0) as cli:
        res = cli.request(
            method, f"{base}{path}",
            headers=headers, json=json_body, files=files, data=data,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {res.status_code}: {res.text}")
        return res.json() if res.content else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", required=True)
    parser.add_argument("--master-token", required=True)
    parser.add_argument("--worker-ip", required=True,
                        help="Worker peer's IP:port as known to the master")
    parser.add_argument("--workflow-id", default="wave9-e2e")
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument("--expected-rows", type=int, default=None,
                        help="Optional: assert result.txt contains rows=N")
    args = parser.parse_args()

    sa_path = os.environ.get("WAVE9_GDRIVE_SA_JSON_PATH")
    folder_id = os.environ.get("WAVE9_GDRIVE_FOLDER_ID")
    if not (sa_path and folder_id):
        print(
            "ERROR: set WAVE9_GDRIVE_SA_JSON_PATH and "
            "WAVE9_GDRIVE_FOLDER_ID before running."
        )
        return 2
    sa_json = Path(sa_path).read_text(encoding="utf-8")

    # 1. Save credential on the master (depositor).
    _step("1. master → POST /foreign_storage/cloud_credentials (gdrive SA)")
    cred = _api(
        args.master, args.master_token,
        "POST", "/local/foreign_storage/cloud_credentials",
        json_body={
            "provider": "gdrive",
            "label": "wave9-e2e",
            "credential_json": sa_json,
            "default_folder": folder_id,
        },
    )
    credential_id = cred["id"]
    _step(f"  credential_id={credential_id}")

    # 2. Accept task-data terms.
    _step("2. master → POST /task_data_terms/accept")
    _api(args.master, args.master_token, "POST", "/local/task_data_terms/accept")

    # 3. Submit a workflow with workspace_source pointing at the Drive folder.
    _step("3. master → POST /add_workflow (workspace_source = drive folder)")
    workflow = [{
        "id": "demo",
        "runtime": "docker",
        "image": "python:3.11-slim",
        "entrypoint": "python main.py",
        "workspace_source": {
            "type": "gdrive",
            "credential_id": credential_id,
            "folder_id": folder_id,
        },
        "depends_on": [],
    }]
    submit = _api(
        args.master, args.master_token,
        "POST", "/local/add_workflow",
        data={
            "workflow_id": args.workflow_id,
            "workflow_json": json.dumps(workflow),
            "preferred_workers": json.dumps([args.worker_ip]),
        },
    )
    _step(f"  {submit.get('message')}")
    task_id = f"{args.workflow_id}_demo"

    # 4. Poll the master until the task reaches `finished` or `failed`.
    _step(f"4. polling for task {task_id} to reach a terminal state")
    deadline = time.time() + args.timeout_s
    final_status = ""
    while time.time() < deadline:
        net = _api(args.master, args.master_token, "GET", "/local/network")
        tasks = net.get("tasks", []) or []
        match = [t for t in tasks if t.get("id") == task_id]
        if match:
            st = match[0].get("status", "")
            if st in {"finished", "failed", "cancelled", "preempted", "errored"}:
                final_status = st
                break
        time.sleep(3)
    else:
        print("ERROR: timed out waiting for terminal status")
        return 1
    if final_status != "finished":
        print(f"ERROR: task ended in status={final_status!r}")
        return 1
    _step("  task reached status=finished")

    # 5. Download the result and (optionally) verify row count.
    _step("5. master → GET /download_result/{task_id}")
    headers = {"X-Local-Token": args.master_token}
    with httpx.Client(timeout=120.0) as cli:
        r = cli.get(
            f"{args.master}/local/download_result/{task_id}",
            headers=headers,
        )
        if r.status_code != 200:
            print(f"ERROR: result download failed: {r.status_code} {r.text}")
            return 1
        out_path = Path(f"./{task_id}_result.zip")
        out_path.write_bytes(r.content)
    _step(f"  saved {out_path} ({len(r.content)} bytes)")

    if args.expected_rows is not None:
        # Inspect result.zip / result.txt to verify the row count.
        import zipfile
        try:
            with zipfile.ZipFile(out_path, "r") as zf:
                txt = zf.read("result.txt").decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"ERROR: could not extract result.txt: {exc}")
            return 1
        expected = f"rows={args.expected_rows}"
        if expected not in txt:
            print(f"ERROR: result.txt missing {expected!r}; got {txt!r}")
            return 1
        _step(f"  result.txt verified ({txt.strip()})")

    _step("ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
