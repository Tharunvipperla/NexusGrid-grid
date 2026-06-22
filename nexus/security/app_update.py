"""Signed app-update verification — root + per-release delegated keys.

Chain of trust (so a leaked *release* key has a tiny blast radius):

    ROOT key (offline, baked public key below, used rarely)
      └─ signs a delegation CERT: "release key K is authorised until <not_after>"
           └─ release key K (fresh per release) signs the manifest's facts
                └─ the binary's sha256 is one of those facts

A node trusts only the **root** public key (baked in). Each release uses its own
short-lived key, certified by the root. If a release key leaks it can only be
abused until its cert expires — and you mint a new one (or list its key_id in
``REVOKED_KEY_IDS`` and ship a build) without touching the root. The root is the
one thing you guard absolutely (keep it offline / on hardware; it signs only
delegation certs, rarely).

Cut releases with ``tools/sign_release.py`` (holds the offline keys, not shipped).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Root public key (Ed25519, raw, base64). The matching private key is OFFLINE.
ROOT_PUBKEY_B64 = "OPGgu/WrNrX59Uv3bPofYil7DtwF90UMrMVj7bdfaXQ="

# Emergency revocation: release-key ids listed here are rejected even if their
# cert is otherwise valid (ship a build to push a revocation before expiry).
REVOKED_KEY_IDS: frozenset[str] = frozenset()

# Manifest fields the release key signs (everything a node acts on).
SIGNED_FIELDS = ("version", "url", "sha256", "min_version", "notes_url")
# Delegation-cert fields the root signs.
CERT_FIELDS = ("signing_pubkey", "key_id", "not_after", "created")


def _canon(d: dict, fields: tuple[str, ...]) -> bytes:
    return json.dumps(
        {k: str(d.get(k, "")) for k in fields},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def canonical_bytes(manifest: dict) -> bytes:
    """Bytes the release key signs (the manifest facts)."""
    return _canon(manifest, SIGNED_FIELDS)


def cert_bytes(cert: dict) -> bytes:
    """Bytes the root signs (the delegation cert)."""
    return _canon(cert, CERT_FIELDS)


def _verify(pubkey_b64: str, msg: bytes, sig_b64: str) -> bool:
    if not pubkey_b64 or not sig_b64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64)).verify(
            base64.b64decode(sig_b64), msg
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _expired(not_after: str) -> bool:
    try:
        deadline = datetime.fromisoformat(str(not_after).replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > deadline
    except (ValueError, TypeError):
        return True  # unparseable expiry → treat as expired (fail closed)


def verify_release(manifest: dict) -> tuple[bool, str]:
    """Verify the full chain. Returns ``(ok, reason)`` — reason is '' on success."""
    cert = manifest.get("cert") or {}
    # 1. root certifies the release key
    if not _verify(ROOT_PUBKEY_B64, cert_bytes(cert), str(manifest.get("cert_sig", ""))):
        return False, "delegation cert not signed by the root key"
    # 2. release key not revoked
    if str(cert.get("key_id", "")) in REVOKED_KEY_IDS:
        return False, "release key has been revoked"
    # 3. cert still in date
    if _expired(cert.get("not_after", "")):
        return False, "delegation cert has expired"
    # 4. release key signs the manifest facts
    if not _verify(str(cert.get("signing_pubkey", "")), canonical_bytes(manifest), str(manifest.get("sig", ""))):
        return False, "manifest not signed by the certified release key"
    return True, ""
