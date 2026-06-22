"""Wave 6 cloud-eviction end-to-end script.

Drives a real two-node setup through:
  1. depositor uploads a file to host (Wave 5b deposit)
  2. depositor saves a Google Drive credential
  3. host issues an eviction
  4. depositor responds with action=cloud
  5. host streams the encrypted bundle to GDrive
  6. script verifies the Drive object exists and the deposit row is purged

Required env vars:
  WAVE6_GDRIVE_SA_JSON_PATH   path to a service-account JSON file
  WAVE6_GDRIVE_FOLDER_ID      a Drive folder id shared with the SA email

Usage:
  python scripts/wave6_cloud_eviction_e2e.py \
      --depositor http://127.0.0.1:8000 \
      --host      http://127.0.0.1:8001 \
      --depositor-token $TOKEN_A \
      --host-token      $TOKEN_B \
      --file ./fixtures/100mb.bin \
      --password "session-pass"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx


def _step(label: str) -> None:
    print(f"[wave6-e2e] {label}")


def _api(base: str, token: str, method: str, path: str, json=None) -> dict:
    headers = {"X-Local-Token": token}
    with httpx.Client(timeout=120.0) as cli:
        res = cli.request(method, f"{base}{path}", headers=headers, json=json)
        res.raise_for_status()
        return res.json() if res.content else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--depositor", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--depositor-token", required=True)
    parser.add_argument("--host-token", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--ttl-days", type=int, default=30)
    parser.add_argument("--timeout-s", type=int, default=600)
    args = parser.parse_args()

    sa_path = os.environ.get("WAVE6_GDRIVE_SA_JSON_PATH")
    folder_id = os.environ.get("WAVE6_GDRIVE_FOLDER_ID")
    if not (sa_path and folder_id):
        print(
            "ERROR: set WAVE6_GDRIVE_SA_JSON_PATH and "
            "WAVE6_GDRIVE_FOLDER_ID before running."
        )
        return 2

    sa_json = Path(sa_path).read_text(encoding="utf-8")

    # 1. Deposit
    _step("1. depositor → POST /foreign_storage/deposit")
    deposit_payload = {
        "target_peer": _peer_uuid_from_url(args.host),
        "file_path": str(Path(args.file).resolve()),
        "password": args.password,
        "ttl_days": args.ttl_days,
        "transport": "stream",
    }
    deposit = _api(
        args.depositor, args.depositor_token,
        "POST", "/local/foreign_storage/deposit", deposit_payload,
    )
    deposit_id = deposit["deposit_id"]
    _step(f"  deposit_id={deposit_id} chunks={deposit['chunk_count']}")

    # 2. Wait for the deposit to land on the host as `stored`.
    _step("2. waiting for host to mark deposit stored")
    deadline = time.time() + args.timeout_s
    while time.time() < deadline:
        hosted = _api(
            args.host, args.host_token,
            "GET", "/local/foreign_storage/hosted",
        )
        match = [d for d in hosted.get("deposits", [])
                 if d["deposit_id"] == deposit_id]
        if match and match[0]["status"] == "stored":
            break
        time.sleep(2)
    else:
        print("ERROR: timed out waiting for deposit to reach status=stored")
        return 1

    # 3. Persist a GDrive credential on the depositor.
    _step("3. depositor → POST /foreign_storage/cloud_credentials")
    cred = _api(
        args.depositor, args.depositor_token,
        "POST", "/local/foreign_storage/cloud_credentials",
        {
            "provider": "gdrive",
            "label": "wave6-e2e",
            "credential_json": sa_json,
            "default_folder": folder_id,
        },
    )
    credential_id = cred["id"]

    # 4. Host issues eviction.
    _step("4. host → POST /foreign_storage/eviction")
    _api(
        args.host, args.host_token,
        "POST", f"/local/foreign_storage/eviction/{deposit_id}",
    )

    # 5. Depositor responds action=cloud.
    _step("5. depositor → POST /foreign_storage/evict_to_cloud")
    _api(
        args.depositor, args.depositor_token,
        "POST", f"/local/foreign_storage/evict_to_cloud/{deposit_id}",
        {"credential_id": credential_id, "cloud_dest": folder_id},
    )

    # 6. Wait for the host to flip status to `purged`.
    _step("6. waiting for host to mark deposit purged")
    cloud_object_id = ""
    deadline = time.time() + args.timeout_s
    while time.time() < deadline:
        hosted = _api(
            args.host, args.host_token,
            "GET", "/local/foreign_storage/hosted",
        )
        match = [d for d in hosted.get("deposits", [])
                 if d["deposit_id"] == deposit_id]
        if match and match[0]["status"] == "purged":
            cloud_object_id = match[0].get("cloud_object_id", "")
            break
        time.sleep(3)
    else:
        print("ERROR: timed out waiting for status=purged")
        return 1

    if not cloud_object_id:
        print("ERROR: host marked purged but no cloud_object_id was set")
        return 1
    _step(f"  done. cloud_object_id={cloud_object_id}")

    # 7. (Optional) verify the Drive object exists with the right size.
    _step("7. verifying Drive object via SDK")
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_service_account_info(
            __import__("json").loads(sa_json),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        meta = drive.files().get(
            fileId=cloud_object_id, fields="id,name,size,parents",
        ).execute()
        local_size = Path(args.file).stat().st_size
        # Ciphertext is slightly larger than plaintext (per-chunk nonce + tag).
        if int(meta.get("size", 0)) < local_size:
            print(
                f"ERROR: Drive object size {meta['size']} is smaller than "
                f"plaintext {local_size}; ciphertext should be at least as big."
            )
            return 1
        _step(f"  drive object {meta['id']} size={meta['size']} parents={meta.get('parents')}")
    except ImportError:
        _step("  (google SDK not installed — skipping Drive-side verification)")

    _step("ALL CHECKS PASSED ✓")
    return 0


def _peer_uuid_from_url(node_base: str) -> str:
    """Hit the node's /local/identity to learn its UUID.

    The deposit endpoint accepts either UUID or IP:port — we use whichever
    /local/identity reports.
    """
    # Best-effort: most setups don't authenticate /local/identity, but if
    # this fails the user can edit the script to pass the host UUID
    # directly via a CLI flag.
    with httpx.Client(timeout=10.0) as cli:
        res = cli.get(f"{node_base}/local/identity")
        if res.status_code == 200:
            data = res.json()
            return data.get("uuid") or data.get("ip") or ""
    return ""


if __name__ == "__main__":
    sys.exit(main())
