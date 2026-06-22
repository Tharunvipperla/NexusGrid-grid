"""Group grant envelope crypto.

A grant is a signed envelope an admin issues to a member, proving the
member holds the listed roles in the group at issuance time. It is
**asymmetrically signed** so any verifier (a service host, a peer
relay, another member) can validate it given only the group's
``admin_pubkeys`` — no shared secret needed.

Two operations:

1. **Grant signing.** An admin's Ed25519 private key signs a canonical
   JSON payload covering ``(group_id, member_pubkey, roles,
   issued_by_pubkey, issued_at, expires_at, nonce)``. The output is a
   self-contained blob (envelope JSON with embedded signature, UTF-8
   bytes) suitable for transport, storage in ``group_grants``, or
   handover to the member.

2. **Challenge-response.** When a member connects to a service, the
   service issues a fresh nonce. The member signs
   ``b"nexus.group.challenge.v1|" + nonce + sha256(grant_blob)`` with
   their *own* Ed25519 private key (the one bound to
   ``member_pubkey`` in the grant). The verifier checks both:
   (a) the grant is signed by a current group admin AND not expired,
   (b) the challenge signature verifies against the grant's
       ``member_pubkey``.
   A stolen grant blob alone is useless without the matching private
   key.

Key format on the wire and in the DB is **hex-encoded** raw 32-byte
Ed25519 keys, matching the existing token / signature conventions in
:mod:`nexus.security.crypto` and :mod:`nexus.security.cred_crypto`.

This module is **stateless** — it does no DB or filesystem access and
makes no assumption about where keys live. Key management (founder
keypair on disk, admin set on the group, etc.) is the caller's job;
the handshake protocol in step 15.5 wires the pieces together.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.utils.time import iso_now


KEY_HEX_LEN = 64  # 32 bytes Ed25519 key, hex-encoded
SIGNATURE_HEX_LEN = 128  # 64 bytes Ed25519 signature, hex-encoded

# Domain separator for the challenge-response signature so a signature
# minted for a challenge cannot be replayed as a grant signature (or any
# other future Ed25519-signed message in the codebase).
_CHALLENGE_DOMAIN = b"nexus.group.challenge.v1|"


@dataclass(frozen=True)
class Grant:
    """A successfully-verified grant. Returned by :func:`verify_grant`."""

    group_id: str
    member_pubkey: str
    roles: tuple[str, ...]
    issued_by_pubkey: str
    issued_at: str
    expires_at: str
    nonce: str


# ---- keypair helpers -----------------------------------------------------


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair.

    Returns ``(privkey_hex, pubkey_hex)`` — both 64-character lowercase
    hex strings (32 bytes raw each).
    """
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes_raw()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv_bytes.hex(), pub_bytes.hex()


def derive_pubkey(privkey_hex: str) -> str:
    """Return the hex pubkey matching ``privkey_hex`` (32-byte Ed25519)."""
    priv = _load_privkey(privkey_hex)
    return priv.public_key().public_bytes_raw().hex()


def _load_privkey(privkey_hex: str) -> Ed25519PrivateKey:
    if len(privkey_hex) != KEY_HEX_LEN:
        raise ValueError(f"privkey must be {KEY_HEX_LEN} hex chars")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(privkey_hex))


def _load_pubkey(pubkey_hex: str) -> Ed25519PublicKey:
    if len(pubkey_hex) != KEY_HEX_LEN:
        raise ValueError(f"pubkey must be {KEY_HEX_LEN} hex chars")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))


# ---- grant signing / verification ----------------------------------------


def _canonical_payload_bytes(payload: dict) -> bytes:
    """Deterministic UTF-8 JSON encoding suitable for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_grant(
    *,
    group_id: str,
    member_pubkey: str,
    roles: Iterable[str],
    admin_privkey: str,
    issued_at: str,
    expires_at: str,
    nonce: str,
) -> bytes:
    """Build a signed grant blob.

    The caller is responsible for choosing ``issued_at`` and
    ``expires_at`` (ISO-8601 UTC strings) and a unique ``nonce`` (a
    short random hex string; 16 bytes recommended). The returned blob
    is the UTF-8-encoded canonical JSON envelope ``{"payload": {...},
    "signature": "<hex>"}``.
    """
    if len(member_pubkey) != KEY_HEX_LEN:
        raise ValueError("member_pubkey must be 64 hex chars")
    priv = _load_privkey(admin_privkey)
    issuer_pubkey = priv.public_key().public_bytes_raw().hex()

    payload = {
        "group_id": group_id,
        "member_pubkey": member_pubkey,
        "roles": list(roles),
        "issued_by_pubkey": issuer_pubkey,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
    }
    sig = priv.sign(_canonical_payload_bytes(payload)).hex()
    envelope = {"payload": payload, "signature": sig}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_grant(
    blob: bytes,
    *,
    group_admin_pubkeys: Iterable[str],
    now_iso: Optional[str] = None,
) -> Optional[Grant]:
    """Validate a grant blob. Returns the parsed :class:`Grant` or ``None``.

    Reasons for ``None``: malformed JSON, missing fields, signature
    fails verification, ``issued_by_pubkey`` not in
    ``group_admin_pubkeys``, or ``expires_at`` is in the past relative
    to ``now_iso`` (defaults to :func:`nexus.utils.time.iso_now`).
    """
    admin_set = frozenset(group_admin_pubkeys)
    if not admin_set:
        return None

    try:
        envelope = json.loads(blob.decode("utf-8"))
        payload = envelope["payload"]
        signature_hex = envelope["signature"]
        group_id = payload["group_id"]
        member_pubkey = payload["member_pubkey"]
        roles = payload["roles"]
        issued_by_pubkey = payload["issued_by_pubkey"]
        issued_at = payload["issued_at"]
        expires_at = payload["expires_at"]
        nonce = payload["nonce"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None

    if issued_by_pubkey not in admin_set:
        return None
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        return None
    if len(signature_hex) != SIGNATURE_HEX_LEN:
        return None

    try:
        pub = _load_pubkey(issued_by_pubkey)
        pub.verify(bytes.fromhex(signature_hex), _canonical_payload_bytes(payload))
    except (InvalidSignature, ValueError):
        return None

    now = now_iso or iso_now()
    if expires_at <= now:
        return None

    return Grant(
        group_id=group_id,
        member_pubkey=member_pubkey,
        roles=tuple(roles),
        issued_by_pubkey=issued_by_pubkey,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


# ---- challenge-response --------------------------------------------------


def _challenge_material(grant_blob: bytes, nonce: bytes) -> bytes:
    return _CHALLENGE_DOMAIN + nonce + hashlib.sha256(grant_blob).digest()


def sign_challenge(
    *,
    grant_blob: bytes,
    nonce: bytes,
    member_privkey: str,
) -> bytes:
    """Sign a challenge nonce + the grant blob hash with the member's key.

    The signature proves possession of the private key matching the
    ``member_pubkey`` in ``grant_blob``. Replay-bound: changing
    ``nonce`` or substituting a different grant invalidates the
    signature.
    """
    priv = _load_privkey(member_privkey)
    return priv.sign(_challenge_material(grant_blob, nonce))


def verify_challenge(
    *,
    grant_blob: bytes,
    nonce: bytes,
    signature: bytes,
    group_admin_pubkeys: Iterable[str],
    now_iso: Optional[str] = None,
) -> bool:
    """Verify both halves of a challenge response.

    Returns ``True`` only if:

    1. ``grant_blob`` validates via :func:`verify_grant` (admin
       signature, expiry, issuer-in-admin-set), AND
    2. ``signature`` verifies against the grant's ``member_pubkey``
       over ``domain_sep + nonce + sha256(grant_blob)``.
    """
    grant = verify_grant(
        grant_blob,
        group_admin_pubkeys=group_admin_pubkeys,
        now_iso=now_iso,
    )
    if grant is None:
        return False
    try:
        pub = _load_pubkey(grant.member_pubkey)
        pub.verify(signature, _challenge_material(grant_blob, nonce))
    except (InvalidSignature, ValueError):
        return False
    return True


__all__ = [
    "KEY_HEX_LEN",
    "SIGNATURE_HEX_LEN",
    "Grant",
    "generate_keypair",
    "derive_pubkey",
    "sign_grant",
    "verify_grant",
    "sign_challenge",
    "verify_challenge",
]
