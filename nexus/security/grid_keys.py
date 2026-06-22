"""Derive per-context relay ``grid_key`` values.

Until NexusGrid used a single global ``grid_key`` for every
relay subscription on a node. That meant a relay operator could lump
together every group + pair a node belonged to as one logical bucket,
because every frame this node sent or received rode the same key.

moves to **per-context** ``grid_key`` derivation so the relay
buckets each group + each pair separately. Two consequences:

1. **Isolation.** A rogue relay operator can no longer correlate
   "Alice is in Group X *and* Group Y" — they see one membership
   bucket per ``grid_key`` and have no signal connecting them.
2. **Broadcast scoping.** Discovery beacons (the only fan-out frame
   NexusGrid sends through a relay) now go to a specific bucket
   instead of everyone holding the global key.

The derivations are deterministic SHA-256 hashes so any two nodes
sharing the same context independently produce the same key without
coordinating. Group keys hash the immutable ``group_id``; pair keys
hash the unordered pair ``{pubkey_a, pubkey_b}``.
"""

from __future__ import annotations

import hashlib

GRID_KEY_LEN = 32  # hex chars; 16 bytes of entropy


def derive_group_grid_key(group_id: str) -> str:
    """Return the relay ``grid_key`` for ``group_id``.

    Stable as long as ``group_id`` is — survives symkey rotations,
    relay rebindings, and membership changes.
    """
    gid = (group_id or "").strip()
    if not gid:
        return ""
    digest = hashlib.sha256(b"nexus:group:" + gid.encode("utf-8")).hexdigest()
    return digest[:GRID_KEY_LEN]


def derive_pair_grid_key(pubkey_a: str, pubkey_b: str) -> str:
    """Return the relay ``grid_key`` for the (a, b) pair, order-independent.

    Used for pair-invite traffic + 1:1 peer routing between two trusted
    nodes outside any shared group.
    """
    a = (pubkey_a or "").strip()
    b = (pubkey_b or "").strip()
    if not a or not b:
        return ""
    lo, hi = sorted([a, b])
    digest = hashlib.sha256(
        b"nexus:pair:" + lo.encode("utf-8") + b"|" + hi.encode("utf-8")
    ).hexdigest()
    return digest[:GRID_KEY_LEN]


__all__ = [
    "GRID_KEY_LEN",
    "derive_group_grid_key",
    "derive_pair_grid_key",
]
