"""Per-node Ed25519 group-identity keypair.

A node uses ONE keypair across every group it is a member of: founder
of group A, admin of group B, member of group C all share the same
``member_pubkey``. The private key never leaves the node; the public
key is what other nodes record on the wire when they admit us.

Persistence mirrors :mod:`nexus.security.tokens`:

* File: ``.nexus_group_key`` next to ``.nexus_secret`` (mode 0o600).
* Format: 64-char lowercase hex of the raw 32-byte Ed25519 private
  key. The public key is derived on read, never stored.
* Created on first call; cached after that.

The handshake protocol in step 15.5 will use this keypair to sign
challenge nonces; admin nodes use it to sign grants for members they
admit. Tests reset the cache via :func:`_reset_for_testing`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from nexus.core.paths import BASE_DIR, secure_file_permissions
from nexus.security.group_grant import derive_pubkey, generate_keypair


GROUP_KEY_FILE = ".nexus_group_key"

_cached_privkey: Optional[str] = None
_cached_pubkey: Optional[str] = None


def _resolve_path() -> Path:
    return Path(BASE_DIR) / GROUP_KEY_FILE


def _read_or_create() -> str:
    """Return the hex Ed25519 private key, generating one if needed."""
    path = _resolve_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            secure_file_permissions(path)
            return existing
    except FileNotFoundError:
        pass

    priv_hex, _ = generate_keypair()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, priv_hex.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Another process won the race; re-read its value so every
        # process in this node converges on the same key.
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        path.write_text(priv_hex, encoding="utf-8")
    secure_file_permissions(path)
    return priv_hex


def get_local_group_privkey() -> str:
    """Return the node's Ed25519 group-identity private key (hex)."""
    global _cached_privkey, _cached_pubkey
    if _cached_privkey is None:
        _cached_privkey = _read_or_create()
        _cached_pubkey = derive_pubkey(_cached_privkey)
    return _cached_privkey


def get_local_group_pubkey() -> str:
    """Return the node's Ed25519 group-identity public key (hex)."""
    global _cached_pubkey
    if _cached_pubkey is None:
        get_local_group_privkey()
    assert _cached_pubkey is not None
    return _cached_pubkey


def _reset_for_testing() -> None:
    """Forget the cached keypair. Pytest fixtures call this between tests."""
    global _cached_privkey, _cached_pubkey
    _cached_privkey = None
    _cached_pubkey = None


__all__ = [
    "GROUP_KEY_FILE",
    "get_local_group_privkey",
    "get_local_group_pubkey",
    "_reset_for_testing",
]
