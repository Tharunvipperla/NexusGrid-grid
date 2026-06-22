"""Wave 7 preview end-to-end script.

Drives a real two-node setup through:

  1. depositor uploads a file to host (Wave 5b deposit)
  2. depositor unlocks the deposit with the password
  3. depositor fetches the manifest endpoint
  4. depositor GETs the full preview stream + verifies SHA matches
  5. depositor GETs a partial Range and verifies the slice matches
  6. depositor locks the deposit
  7. follow-up preview without re-unlocking returns 401

Usage:
  python scripts/wave7_preview_e2e.py \\
      --depositor http://127.0.0.1:8000 \\
      --host      http://127.0.0.1:8001 \\
      --depositor-token $TOKEN_A \\
      --host-token      $TOKEN_B \\
      --file ./fixtures/sample.jpg \\
      --password "session-pass"
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import httpx


def _step(label: str) -> None:
    print(f"[wave7-e2e] {label}")


def _api(base: str, token: str, method: str, path: str, json=None) -> dict:
    headers = {"X-Local-Token": token}
    with httpx.Client(timeout=120.0) as cli:
        res = cli.request(method, f"{base}{path}", headers=headers, json=json)
        res.raise_for_status()
        return res.json() if res.content else {}


def _raw(base: str, token: str, path: str, *, headers: dict | None = None) -> httpx.Response:
    h = {"X-Local-Token": token}
    if headers:
        h.update(headers)
    with httpx.Client(timeout=120.0) as cli:
        return cli.get(f"{base}{path}", headers=h)


def _peer_uuid(base: str, token: str) -> str:
    info = _api(base, token, "GET", "/local/identity")
    return info.get("uuid") or info.get("node_uuid") or ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--depositor", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--depositor-token", required=True)
    p.add_argument("--host-token", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--ttl-days", type=int, default=30)
    p.add_argument("--accept-timeout-s", type=int, default=60)
    args = p.parse_args()

    file_path = Path(args.file).resolve()
    if not file_path.is_file():
        print(f"ERROR: --file {file_path} does not exist")
        return 2

    plaintext = file_path.read_bytes()
    expected_sha = hashlib.sha256(plaintext).hexdigest()
    total = len(plaintext)

    host_uuid = _peer_uuid(args.host, args.host_token)
    if not host_uuid:
        print("ERROR: could not resolve host UUID via /local/identity")
        return 2

    # 1. Deposit (Wave 5b path).
    _step(f"1. depositor -> POST /foreign_storage/deposit (target={host_uuid})")
    deposit = _api(
        args.depositor, args.depositor_token, "POST",
        "/local/foreign_storage/deposit",
        json={
            "target_peer": host_uuid,
            "file_path": str(file_path),
            "password": args.password,
            "ttl_days": args.ttl_days,
            "transport": "stream",
        },
    )
    deposit_id = deposit["deposit_id"]
    _step(f"   deposit_id={deposit_id}")

    # Wait for the host to flip to "stored".
    _step("   waiting for host to accept + ingest...")
    deadline = time.time() + args.accept_timeout_s
    while time.time() < deadline:
        rows = _api(args.depositor, args.depositor_token, "GET",
                    "/local/foreign_storage/deposits")
        mine = [d for d in (rows.get("deposits") or [])
                if d["deposit_id"] == deposit_id]
        if mine and mine[0]["status"] == "stored":
            break
        time.sleep(1.0)
    else:
        print("ERROR: timed out waiting for status=stored")
        return 1

    # 2. Unlock.
    _step("2. depositor -> POST /foreign_storage/unlock/{id}")
    _api(args.depositor, args.depositor_token, "POST",
         f"/local/foreign_storage/unlock/{deposit_id}",
         json={"password": args.password})

    # 3. Manifest.
    _step("3. depositor -> GET /foreign_storage/manifest/{id}")
    manifest = _api(args.depositor, args.depositor_token, "GET",
                    f"/local/foreign_storage/manifest/{deposit_id}")
    if manifest["size"] != total:
        print(f"ERROR: manifest size {manifest['size']} != local {total}")
        return 1
    _step(f"   filename={manifest['filename']} mime={manifest['mime']} "
          f"size={manifest['size']} chunks={manifest['chunk_count']}")

    # 4. Full GET.
    _step("4. depositor -> GET /foreign_storage/preview/{id} (full)")
    full = _raw(args.depositor, args.depositor_token,
                f"/local/foreign_storage/preview/{deposit_id}")
    if full.status_code != 200:
        print(f"ERROR: full GET status {full.status_code}")
        return 1
    actual_sha = hashlib.sha256(full.content).hexdigest()
    if actual_sha != expected_sha:
        print(f"ERROR: full SHA mismatch expected={expected_sha} got={actual_sha}")
        return 1
    _step("   sha256 match.")

    # 5. Range GET in the middle.
    if total >= 4096:
        start = total // 3
        end = start + 1023
        _step(f"5. depositor -> GET /preview/{deposit_id} bytes={start}-{end}")
        partial = _raw(args.depositor, args.depositor_token,
                       f"/local/foreign_storage/preview/{deposit_id}",
                       headers={"Range": f"bytes={start}-{end}"})
        if partial.status_code != 206:
            print(f"ERROR: partial GET status {partial.status_code}")
            return 1
        if partial.content != plaintext[start:end + 1]:
            print("ERROR: partial slice mismatch")
            return 1
        _step("   partial slice match.")
    else:
        _step("5. (skipped — file too small for partial test)")

    # 6. Lock.
    _step("6. depositor -> POST /foreign_storage/lock/{id}")
    _api(args.depositor, args.depositor_token, "POST",
         f"/local/foreign_storage/lock/{deposit_id}")

    # 7. Preview after lock should 401.
    _step("7. depositor -> GET /preview/{id} after lock")
    locked = _raw(args.depositor, args.depositor_token,
                  f"/local/foreign_storage/preview/{deposit_id}")
    if locked.status_code != 401:
        print(f"ERROR: expected 401 after lock, got {locked.status_code}")
        return 1
    _step("   401 as expected.")

    print("[wave7-e2e] ALL CHECKPOINTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
