"""HMAC signing primitives for peer-to-peer integrity.

Extracted from node_modified.py (lines 1571-1613).

Every peer payload (task bundle, result, bye frame) carries a hex HMAC-SHA256
signature over ``purpose|task_id|extra|sha256(payload)``. Both sides must
agree on the ``purpose`` string (e.g. ``"task_bundle"``, ``"task_result"``)
and the signing key.

Keys
----

* The default key is the node's persistent ``SIGNING_SECRET`` from
  :mod:`nexus.security.tokens`.
* For per-peer signing (negotiated during the join handshake) callers pass
  an explicit ``key=...`` argument. See
  ``node_modified.py:sign_bytes`` callers for examples.
"""

from __future__ import annotations

import hashlib
import hmac

from nexus.security.tokens import get_signing_secret


def _key_bytes(key: str) -> bytes:
    return (key or get_signing_secret()).encode("utf-8")


def sign_bytes(
    purpose: str,
    task_id: str,
    payload: bytes,
    extra: str = "",
    key: str = "",
) -> str:
    """Return a hex HMAC-SHA256 signature over the canonical material.

    ``material = purpose | task_id | extra | sha256(payload)``.
    """
    material = (
        purpose.encode("utf-8")
        + b"|"
        + task_id.encode("utf-8")
        + b"|"
        + extra.encode("utf-8")
        + b"|"
        + hashlib.sha256(payload).hexdigest().encode("utf-8")
    )
    return hmac.new(_key_bytes(key), material, hashlib.sha256).hexdigest()


def verify_signature(
    expected: str,
    purpose: str,
    task_id: str,
    payload: bytes,
    extra: str = "",
    key: str = "",
) -> bool:
    """Constant-time check of a signature produced by :func:`sign_bytes`.

    An empty ``expected`` is treated as invalid (rejected) so callers do not
    need to distinguish "missing header" from "wrong signature".
    """
    if not expected:
        return False
    return hmac.compare_digest(
        expected, sign_bytes(purpose, task_id, payload, extra, key=key)
    )


def sign_bye(node_id: str, ts: float, key: str = "") -> str:
    """Sign a bye frame sent during graceful shutdown.

    Material is ``bye|{node_id}|{ts}``; no payload.
    """
    material = f"bye|{node_id}|{ts}".encode("utf-8")
    return hmac.new(_key_bytes(key), material, hashlib.sha256).hexdigest()


def verify_bye(node_id: str, ts: float, sig: str, key: str = "") -> bool:
    """Verify a bye frame signature. Empty signature ⇒ ``False``."""
    if not sig:
        return False
    return hmac.compare_digest(sig, sign_bye(node_id, ts, key=key))
