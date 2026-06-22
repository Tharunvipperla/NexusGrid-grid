"""Credential-blob crypto for the cloud-eviction tier.

Two flavours of wrap, both AES-256-GCM with HKDF-SHA256-derived keys.

* **At-rest** (:func:`wrap_credential_blob`): IKM is the depositor's
  ``.nexus_secret`` (via :func:`nexus.security.tokens.get_signing_secret`).
  Random nonce per wrap. Holds the depositor's cloud creds in the
  ``CloudCredential`` SQLite table.

* **Transit** (:func:`wrap_for_transit`): IKM is the per-peer shared
  ``Peer.signing_key`` — the same HMAC secret that already authenticates
  trusted-peer WebSocket frames. Salted by a 16-byte ``eviction_nonce``
  chosen freshly per cloud-eviction request. The host (which never holds
  the depositor's deposit AES key) recovers the credential blob via the
  shared signing key.

The transit-wrap design intentionally does **not** leak the depositor's
deposit AES key to the host: the host's ability to decrypt the credential
blob comes from the existing trusted-peer relationship, not from any new
privilege over the deposit.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nexus.security.deposit_crypto import KEY_BYTES, NONCE_BYTES
from nexus.security.tokens import get_signing_secret

EVICTION_NONCE_BYTES = 16

_AT_REST_SALT = b"nexus.fs.cloud.creds.atrest.v1\x00\x00"  # 32 bytes
_AT_REST_INFO = b"fs.cloud.creds.atrest.v1"
_TRANSIT_INFO = b"fs.cloud.creds.v1"
_TASK_DATA_INFO = b"task.data.creds.v1"
_VIEW_GRANT_INFO = b"fs.view.grant.v1"
_TRANSIT_NONCE = b"\x00" * NONCE_BYTES


def _at_rest_key() -> bytes:
    secret = get_signing_secret().encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=_AT_REST_SALT,
        info=_AT_REST_INFO,
    ).derive(secret)


def wrap_credential_blob(plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` for storage in the depositor's SQLite DB."""
    aes = AESGCM(_at_rest_key())
    nonce = os.urandom(NONCE_BYTES)
    return nonce + aes.encrypt(nonce, plaintext, None)


def unwrap_credential_blob(blob: bytes) -> bytes:
    """Inverse of :func:`wrap_credential_blob`. Raises ``InvalidTag`` on tamper."""
    if len(blob) < NONCE_BYTES:
        raise ValueError("blob too short")
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    aes = AESGCM(_at_rest_key())
    return aes.decrypt(nonce, ct, None)


def derive_eviction_wrap_key(
    peer_signing_key: str, eviction_nonce: bytes
) -> bytes:
    """Per-eviction transit-wrap key bound to a trusted-peer pair."""
    if not peer_signing_key:
        raise ValueError("peer_signing_key required")
    if len(eviction_nonce) != EVICTION_NONCE_BYTES:
        raise ValueError(
            f"eviction_nonce must be {EVICTION_NONCE_BYTES} bytes, "
            f"got {len(eviction_nonce)}"
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=eviction_nonce,
        info=_TRANSIT_INFO,
    ).derive(peer_signing_key.encode("utf-8"))


def wrap_for_transit(
    peer_signing_key: str, eviction_nonce: bytes, plaintext: bytes
) -> bytes:
    """Encrypt ``plaintext`` for one-shot transmission to the host."""
    key = derive_eviction_wrap_key(peer_signing_key, eviction_nonce)
    aes = AESGCM(key)
    return aes.encrypt(_TRANSIT_NONCE, plaintext, None)


def unwrap_from_transit(
    peer_signing_key: str, eviction_nonce: bytes, blob: bytes
) -> bytes:
    """Host-side inverse of :func:`wrap_for_transit`. Raises ``InvalidTag``."""
    key = derive_eviction_wrap_key(peer_signing_key, eviction_nonce)
    aes = AESGCM(key)
    return aes.decrypt(_TRANSIT_NONCE, blob, None)


def derive_task_data_wrap_key(
    peer_signing_key: str, task_nonce: bytes
) -> bytes:
    """Per-dispatch transit-wrap key for task-data cloud credentials.

    distinct ``info`` literal from :func:`derive_eviction_wrap_key`
    so a captured eviction envelope cannot be replayed as a task-data
    envelope (or vice versa) even between the same trusted-peer pair.
    """
    if not peer_signing_key:
        raise ValueError("peer_signing_key required")
    if len(task_nonce) != EVICTION_NONCE_BYTES:
        raise ValueError(
            f"task_nonce must be {EVICTION_NONCE_BYTES} bytes, "
            f"got {len(task_nonce)}"
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=task_nonce,
        info=_TASK_DATA_INFO,
    ).derive(peer_signing_key.encode("utf-8"))


def wrap_task_data_for_transit(
    peer_signing_key: str, task_nonce: bytes, plaintext: bytes
) -> bytes:
    """Encrypt ``plaintext`` for one-shot dispatch to a worker."""
    key = derive_task_data_wrap_key(peer_signing_key, task_nonce)
    aes = AESGCM(key)
    return aes.encrypt(_TRANSIT_NONCE, plaintext, None)


def unwrap_task_data_from_transit(
    peer_signing_key: str, task_nonce: bytes, blob: bytes
) -> bytes:
    """Worker-side inverse of :func:`wrap_task_data_for_transit`."""
    key = derive_task_data_wrap_key(peer_signing_key, task_nonce)
    aes = AESGCM(key)
    return aes.decrypt(_TRANSIT_NONCE, blob, None)


def derive_view_grant_wrap_key(
    peer_signing_key: str, grant_nonce: bytes
) -> bytes:
    """Per-grant transit-wrap key for the deposit AES key.

    The depositor's deposit AES key is itself the secret being shared.
    Wrapping uses the trusted-peer ``signing_key`` plus a per-grant
    16-byte nonce, with the HKDF ``info`` literal ``fs.view.grant.v1``
    so a captured envelope from another flow (eviction, task data)
    cannot be replayed as a view grant.
    """
    if not peer_signing_key:
        raise ValueError("peer_signing_key required")
    if len(grant_nonce) != EVICTION_NONCE_BYTES:
        raise ValueError(
            f"grant_nonce must be {EVICTION_NONCE_BYTES} bytes, "
            f"got {len(grant_nonce)}"
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=grant_nonce,
        info=_VIEW_GRANT_INFO,
    ).derive(peer_signing_key.encode("utf-8"))


def wrap_view_grant_for_transit(
    peer_signing_key: str, grant_nonce: bytes, deposit_key: bytes
) -> bytes:
    """Encrypt the deposit AES key for delivery to the host."""
    key = derive_view_grant_wrap_key(peer_signing_key, grant_nonce)
    return AESGCM(key).encrypt(_TRANSIT_NONCE, deposit_key, None)


def unwrap_view_grant_from_transit(
    peer_signing_key: str, grant_nonce: bytes, blob: bytes
) -> bytes:
    """Host-side inverse of :func:`wrap_view_grant_for_transit`."""
    key = derive_view_grant_wrap_key(peer_signing_key, grant_nonce)
    return AESGCM(key).decrypt(_TRANSIT_NONCE, blob, None)


__all__ = [
    "EVICTION_NONCE_BYTES",
    "wrap_credential_blob",
    "unwrap_credential_blob",
    "derive_eviction_wrap_key",
    "wrap_for_transit",
    "unwrap_from_transit",
    "derive_task_data_wrap_key",
    "wrap_task_data_for_transit",
    "unwrap_task_data_from_transit",
    "derive_view_grant_wrap_key",
    "wrap_view_grant_for_transit",
    "unwrap_view_grant_from_transit",
]
