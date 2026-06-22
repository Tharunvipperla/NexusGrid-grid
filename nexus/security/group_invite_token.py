"""Signed group-join-invite tokens.

Companion to ``pair_invite.py``. A founder generates a signed envelope
that authorises a stranger to send a group join request without the
founder needing to share their relay's ``grid_key`` in the link. Per-
link ``max_uses`` and per-group ``max_members`` caps are enforced at
the join handler.

Distinct from the ``group_invite.py`` module which handles
the legacy bearer-token invite flow — this module is the v2 signed
variant added in.

Signed envelope (the ``inv`` blob in the v=2 ``nxg://join#...`` link)::

    {
      "payload": {
        "invite_id":     "<random 256-bit hex>",
        "group_id":      "<group_id>",
        "founder_pubkey":"<Ed25519 hex>",
        "issued_at":     "ISO-8601",
        "expires_at":    "ISO-8601",
        "max_uses":      <int>
      },
      "signature": "<hex-encoded Ed25519 signature over canonical JSON>"
    }

Security properties (mirrors W36.F's pair-invite model):

* Link is safe to post publicly — no ``grid_key`` / transport credential.
* Signature pins ``founder_pubkey`` + ``group_id`` together; an invite
  can't be repurposed against a different founder or a different group.
* Per-link ``max_uses`` enforced via the local ``GroupJoinInvite.used_count``.
* Per-group ``Group.max_members`` enforced at join time independent of
  which invite was used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.utils.time import iso_now

KEY_HEX_LEN = 64
SIG_HEX_LEN = 128


@dataclass(frozen=True)
class GroupJoinInviteToken:
    invite_id: str
    group_id: str
    founder_pubkey: str
    issued_at: str
    expires_at: str
    max_uses: int


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_group_join_invite(
    *,
    invite_id: str,
    group_id: str,
    founder_pubkey: str,
    issued_at: str,
    expires_at: str,
    max_uses: int,
    founder_privkey: str,
) -> str:
    """Build the ``inv`` blob (hex-encoded canonical JSON envelope)."""
    if len(founder_pubkey) != KEY_HEX_LEN:
        raise ValueError(f"founder_pubkey must be {KEY_HEX_LEN} hex chars")
    if len(founder_privkey) != KEY_HEX_LEN:
        raise ValueError(f"founder_privkey must be {KEY_HEX_LEN} hex chars")
    if max_uses < 1:
        raise ValueError("max_uses must be >= 1")

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(founder_privkey))
    derived = priv.public_key().public_bytes_raw().hex()
    if derived != founder_pubkey:
        raise ValueError("founder_pubkey does not match founder_privkey")

    payload = {
        "invite_id": invite_id,
        "group_id": group_id,
        "founder_pubkey": founder_pubkey,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "max_uses": int(max_uses),
    }
    sig = priv.sign(_canonical(payload)).hex()
    envelope = {"payload": payload, "signature": sig}
    return _canonical(envelope).hex()


def verify_group_join_invite(
    inv_hex: str,
    *,
    expected_group_id: Optional[str] = None,
    expected_founder_pubkey: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Optional[GroupJoinInviteToken]:
    """Verify a signed group invite. Returns the parsed payload or None.

    ``expected_group_id`` / ``expected_founder_pubkey`` defend against
    a forged invite that claims a different group/founder — the join
    handler always pins them to the local group's row.
    """
    try:
        envelope = json.loads(bytes.fromhex(inv_hex).decode("utf-8"))
        payload = envelope["payload"]
        sig_hex = envelope["signature"]
        invite_id = str(payload["invite_id"])
        group_id = str(payload["group_id"])
        founder_pubkey = str(payload["founder_pubkey"])
        issued_at = str(payload["issued_at"])
        expires_at = str(payload["expires_at"])
        max_uses = int(payload["max_uses"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    if len(founder_pubkey) != KEY_HEX_LEN:
        return None
    if len(sig_hex) != SIG_HEX_LEN:
        return None
    if max_uses < 1:
        return None
    if expected_group_id and expected_group_id != group_id:
        return None
    if expected_founder_pubkey and expected_founder_pubkey != founder_pubkey:
        return None

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(founder_pubkey))
        pub.verify(bytes.fromhex(sig_hex), _canonical(payload))
    except (InvalidSignature, ValueError):
        return None

    now = now_iso or iso_now()
    if expires_at <= now:
        return None

    return GroupJoinInviteToken(
        invite_id=invite_id,
        group_id=group_id,
        founder_pubkey=founder_pubkey,
        issued_at=issued_at,
        expires_at=expires_at,
        max_uses=max_uses,
    )


__all__ = [
    "GroupJoinInviteToken",
    "sign_group_join_invite",
    "verify_group_join_invite",
]
