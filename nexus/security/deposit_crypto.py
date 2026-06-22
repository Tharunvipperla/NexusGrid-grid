"""Per-deposit encryption primitives.

The depositor holds the only password. Every chunk + the manifest are
encrypted with a key derived from password + per-deposit salt via
Argon2id. AES-256-GCM with a deterministic IV (chunk_idx as 96 bits)
gives integrity + confidentiality; the IV is safe because the key is
unique per deposit.

Manifest IV is fixed (``b"\\x00" * 12 OR'd with manifest tag``) since
there is exactly one manifest per key. Wrong password fails GCM tag
verification before any plaintext is returned.
"""

from __future__ import annotations

import json
from typing import Any

from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
GCM_OVERHEAD = NONCE_BYTES + TAG_BYTES

_ARGON2_TIME_COST = 2
_ARGON2_MEMORY_KIB = 19 * 1024  # 19 MiB
_ARGON2_PARALLELISM = 1

_MANIFEST_NONCE = b"\xff" * NONCE_BYTES


def derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte key from *password* + *salt* via Argon2id."""
    if len(salt) != SALT_BYTES:
        raise ValueError(f"salt must be {SALT_BYTES} bytes, got {len(salt)}")
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_KIB,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def _chunk_nonce(chunk_idx: int) -> bytes:
    if chunk_idx < 0:
        raise ValueError("chunk_idx must be non-negative")
    return chunk_idx.to_bytes(NONCE_BYTES, "big")


def encrypt_chunk(key: bytes, chunk: bytes, chunk_idx: int) -> bytes:
    """Return ``nonce || ciphertext || tag`` for *chunk*."""
    aes = AESGCM(key)
    nonce = _chunk_nonce(chunk_idx)
    ct = aes.encrypt(nonce, chunk, associated_data=None)
    return nonce + ct


def decrypt_chunk(key: bytes, blob: bytes, chunk_idx: int) -> bytes:
    """Verify + decrypt one chunk. Raises ``InvalidTag`` on tamper / wrong key."""
    if len(blob) < GCM_OVERHEAD:
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    expected = _chunk_nonce(chunk_idx)
    if nonce != expected:
        raise ValueError(
            f"nonce mismatch at chunk {chunk_idx}: stored={nonce.hex()} "
            f"expected={expected.hex()}"
        )
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, associated_data=None)


def seal_manifest(key: bytes, manifest: dict[str, Any]) -> bytes:
    """Encrypt the manifest dict so the host can't enumerate filenames."""
    aes = AESGCM(key)
    payload = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    return _MANIFEST_NONCE + aes.encrypt(_MANIFEST_NONCE, payload, None)


def unseal_manifest(key: bytes, blob: bytes) -> dict[str, Any]:
    """Inverse of :func:`seal_manifest`."""
    if len(blob) < GCM_OVERHEAD or blob[:NONCE_BYTES] != _MANIFEST_NONCE:
        raise ValueError("manifest blob malformed")
    aes = AESGCM(key)
    payload = aes.decrypt(_MANIFEST_NONCE, blob[NONCE_BYTES:], None)
    return json.loads(payload.decode("utf-8"))


__all__ = [
    "KEY_BYTES",
    "SALT_BYTES",
    "NONCE_BYTES",
    "TAG_BYTES",
    "GCM_OVERHEAD",
    "derive_key",
    "encrypt_chunk",
    "decrypt_chunk",
    "seal_manifest",
    "unseal_manifest",
]
