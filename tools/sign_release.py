"""Cut a signed release with the root + per-release delegated-key scheme.

Keys are OFFLINE and never in the repo. One-time setup, then per release:

    # 1. once: make the root keypair (the trust anchor)
    python tools/sign_release.py --gen-root
    #    -> bake the printed PUBLIC into nexus/security/app_update.py ROOT_PUBKEY_B64
    #    -> keep the PRIVATE offline (set NEXUS_ROOT_PRIVKEY when signing)

    # 2. per release: a FRESH release key is generated and certified by the root.
    #    Pass a (URL, local-path) pair per platform you built — at least one.
    set NEXUS_ROOT_PRIVKEY=<base64 root private>
    python tools/sign_release.py 1.1.0 \
        --win   "https://host/v1.1.0/NexusGrid.exe"        dist/NexusGrid.exe \
        --mac   "https://host/v1.1.0/NexusGrid-macos"      dist/NexusGrid-macos \
        --linux "https://host/v1.1.0/NexusGrid-linux"      dist/NexusGrid-linux \
        --notes "https://host/releases/1.1.0" --min-version 1.0.0 --out manifest.json

The manifest carries: the release key's signature over the facts (incl. the
per-platform binary map), plus a delegation cert (release pubkey + expiry)
signed by the root. A node verifies root -> cert -> release key -> binary hash,
then downloads the binary for its own OS. A leaked release key dies at the
cert's expiry; the root is used only here, rarely.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus.security.app_update import canonical_bytes, cert_bytes, platforms_bytes


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _raw_priv(sk: Ed25519PrivateKey) -> bytes:
    return sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def _raw_pub(sk: Ed25519PrivateKey) -> bytes:
    return sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _load_root(key_file: str | None) -> Ed25519PrivateKey:
    import os

    if key_file:
        with open(key_file) as f:
            b64 = f.read().strip()
    else:
        b64 = os.getenv("NEXUS_ROOT_PRIVKEY", "").strip() or None
    if not b64:
        sys.exit("No root key. Set NEXUS_ROOT_PRIVKEY or pass --root-key-file. Keep it OFFLINE.")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def _gen_root() -> int:
    sk = Ed25519PrivateKey.generate()
    print("ROOT PRIVATE (keep OFFLINE, e.g. NEXUS_ROOT_PRIVKEY):")
    print("  " + _b64(_raw_priv(sk)))
    print("ROOT PUBLIC (bake into app_update.py ROOT_PUBKEY_B64):")
    print("  " + _b64(_raw_pub(sk)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?")
    ap.add_argument("--win", nargs=2, metavar=("URL", "PATH"), help="Windows exe download URL + local path")
    ap.add_argument("--mac", nargs=2, metavar=("URL", "PATH"), help="macOS binary download URL + local path")
    ap.add_argument("--linux", nargs=2, metavar=("URL", "PATH"), help="Linux binary download URL + local path")
    ap.add_argument("--notes", default="")
    ap.add_argument("--min-version", default="0.0.0")
    ap.add_argument("--cert-days", type=int, default=90, help="how long the release key stays valid")
    ap.add_argument("--out", default="manifest.json")
    ap.add_argument("--root-key-file", default=None)
    ap.add_argument("--gen-root", action="store_true", help="generate the root keypair and exit")
    args = ap.parse_args()

    if args.gen_root:
        return _gen_root()
    if not args.version:
        ap.error("version is required (unless --gen-root)")

    platforms: dict[str, dict[str, str]] = {}
    for key, pair in (("windows", args.win), ("macos", args.mac), ("linux", args.linux)):
        if pair:
            url, path = pair
            platforms[key] = {"url": url, "sha256": _sha256_file(path)}
    if not platforms:
        ap.error("provide at least one of --win/--mac/--linux (URL PATH)")

    root = _load_root(args.root_key_file)             # exits cleanly if missing

    # fresh release key, certified by the root
    rel = Ed25519PrivateKey.generate()
    rel_pub_b64 = _b64(_raw_pub(rel))
    now = datetime.now(timezone.utc)
    cert = {
        "signing_pubkey": rel_pub_b64,
        "key_id": hashlib.sha256(_raw_pub(rel)).hexdigest()[:16],
        "not_after": (now + timedelta(days=args.cert_days)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "created": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    cert_sig = _b64(root.sign(cert_bytes(cert)))

    # Top-level url/sha256 = the Windows binary, for backward compatibility with
    # nodes that predate the per-platform map. Empty if no Windows build.
    win = platforms.get("windows", {})
    import json

    manifest = {
        "version": args.version,
        "url": win.get("url", ""),
        "sha256": win.get("sha256", ""),
        "min_version": args.min_version,
        "notes_url": args.notes,
        "platforms": platforms,
        "cert": cert,
        "cert_sig": cert_sig,
    }
    manifest["sig"] = _b64(rel.sign(canonical_bytes(manifest)))
    manifest["platforms_sig"] = _b64(rel.sign(platforms_bytes(platforms)))

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"wrote {args.out} — v{args.version}, platforms {sorted(platforms)}, "
          f"key_id {cert['key_id']}, expires {cert['not_after']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
