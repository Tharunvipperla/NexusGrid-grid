"""Sender-hosted large (>5 MB) chat/DM attachments.

Small attachments (<=5 MB) ride inside the message, sealed. Larger ones would
bloat the frame/relay, so instead the sender keeps the file on its own disk and
the message carries only a reference (``attach_kind="foreign"``). When the
recipient opens the conversation it pulls the bytes from the sender, which
seals them per-requester at serve time — symkey for a group, ECIES for a DM —
so the transfer stays end-to-end encrypted.

Blobs are stored plaintext on the *sender's* own device (it's their file) under
``<cache>/chat_attachments/<msg_id>.bin`` and sealed only when served.
"""

from __future__ import annotations

import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from nexus.core.identity import get_node_port
from nexus.core.paths import cache_dir

_log = logging.getLogger("nexus.runtime.chat_attachments")

# Hard ceiling for a single attachment. Inline is capped at 5 MB elsewhere;
# this bounds the sender-hosted path so a single message can't stage gigabytes.
MAX_ATTACH_BYTES = 100 * 1024 * 1024


def _dir():
    d = cache_dir(get_node_port()) / "chat_attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(blob_key: str):
    # blob_key is a uuid msg_id — keep only hex so it's a safe filename.
    safe = "".join(c for c in blob_key if c.isalnum())
    return _dir() / f"{safe}.bin"


def store_blob(blob_key: str, data: bytes) -> None:
    _path(blob_key).write_bytes(data)


def load_blob(blob_key: str) -> bytes | None:
    p = _path(blob_key)
    if not p.exists():
        return None
    return p.read_bytes()


def has_blob(blob_key: str) -> bool:
    return _path(blob_key).exists()


def delete_blob(blob_key: str) -> None:
    try:
        os.remove(_path(blob_key))
    except OSError:
        pass


def seal_with_symkey(symkey: bytes, data: bytes) -> bytes:
    """ChaCha20-Poly1305 seal: 12-byte nonce prefix + ciphertext."""
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(symkey).encrypt(nonce, data, b"")


def open_with_symkey(symkey: bytes, blob: bytes) -> bytes:
    nonce, ct = blob[:12], blob[12:]
    return ChaCha20Poly1305(symkey).decrypt(nonce, ct, b"")


__all__ = [
    "MAX_ATTACH_BYTES",
    "store_blob",
    "load_blob",
    "has_blob",
    "delete_blob",
    "seal_with_symkey",
    "open_with_symkey",
]
