"""Signed pair-invite tokens for peer pair links.

Replaces the "grid_key embedded in publicly-shareable link" pattern
that earlier waves used for group joining. A pair-invite link can be
posted on Twitter without granting anyone transport access to the
issuer's relay — possession of the link lets a scraper attempt one
pair request, which the issuer can accept or reject. Rejected /
expired / consumed invites can't be replayed.

Link format::

    nxg://pair#<base64url(JSON)>

JSON payload::

    {
      "k":   "<issuer_pubkey_hex>",            # Ed25519 (group identity)
      "n":   "<issuer_node_id>",               # issuer's node UUID (routes the relay probe)
      "r":   ["wss://relay-a", ...],           # issuer's relay-pool URLs
      "inv": "<base64url(signed_envelope)>",   # signed by issuer_pubkey
      "v":   1
    }

Signed envelope (the ``inv`` blob)::

    {
      "payload": {
        "invite_id":      "<random 256-bit hex>",
        "issuer_pubkey":  "<same as 'k'>",
        "issued_at":      "2026-05-24T...",
        "expires_at":     "2026-05-31T...",
        "max_uses":       1
      },
      "signature": "<hex-encoded Ed25519 signature over canonical JSON>"
    }

Security properties:

* Anyone with the link can attempt a single pair request via the
  issuer's relay. The relay verifies the signed envelope against the
  ``issuer_pubkey`` (which it knows from the issuer's standing
  registration) before forwarding to the issuer.
* ``invite_id`` is single-use (``max_uses=1`` default) — relay tracks
  consumed IDs in a per-issuer cache cleared at ``expires_at``.
* Issuer's UI shows the pair request; only the issuer's accept
  grants the scraper any standing access.
* ``grid_key`` is never in the link — scrapers cannot subscribe to
  the relay, see ``peer_list``, send arbitrary frames, or otherwise
  abuse the transport.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.utils.time import iso_now

PAIR_LINK_SCHEME = "nxg://pair#"
PAIR_INVITE_VERSION = 1

KEY_HEX_LEN = 64       # 32 bytes hex
SIG_HEX_LEN = 128      # 64 bytes hex


@dataclass(frozen=True)
class PairInvite:
    """A successfully-verified pair invite payload."""

    invite_id: str
    issuer_pubkey: str
    issued_at: str
    expires_at: str
    max_uses: int


def _canonical_bytes(payload: dict) -> bytes:
    """Deterministic UTF-8 JSON for signing/verification."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64url_enc(blob: bytes) -> str:
    """RFC 4648 §5 URL-safe base64, padding stripped."""
    return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


def _b64url_dec(text: str) -> bytes:
    """Decode RFC 4648 §5 URL-safe base64, padding-restored."""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_pair_invite(
    *,
    invite_id: str,
    issuer_pubkey: str,
    issued_at: str,
    expires_at: str,
    max_uses: int,
    issuer_privkey: str,
) -> str:
    """Build the ``inv`` blob (base64url-encoded signed envelope).

    Raises ``ValueError`` on malformed inputs. The caller picks
    ``invite_id`` (recommend ``secrets.token_hex(32)``), ``issued_at``,
    ``expires_at``.
    """
    if len(issuer_pubkey) != KEY_HEX_LEN:
        raise ValueError(f"issuer_pubkey must be {KEY_HEX_LEN} hex chars")
    if len(issuer_privkey) != KEY_HEX_LEN:
        raise ValueError(f"issuer_privkey must be {KEY_HEX_LEN} hex chars")
    if max_uses < 1:
        raise ValueError("max_uses must be >= 1")

    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(issuer_privkey))
    derived = priv.public_key().public_bytes_raw().hex()
    if derived != issuer_pubkey:
        raise ValueError("issuer_pubkey does not match issuer_privkey")

    payload = {
        "invite_id": invite_id,
        "issuer_pubkey": issuer_pubkey,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "max_uses": int(max_uses),
    }
    sig = priv.sign(_canonical_bytes(payload)).hex()
    envelope = {"payload": payload, "signature": sig}
    return _b64url_enc(_canonical_bytes(envelope))


def verify_pair_invite(
    inv_b64: str,
    *,
    expected_issuer_pubkey: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> Optional[PairInvite]:
    """Parse + validate a signed pair-invite blob.

    Returns the :class:`PairInvite` on success, ``None`` on any failure
    (malformed, bad signature, expired, issuer-pubkey mismatch).
    ``expected_issuer_pubkey`` (when provided) must match the embedded
    ``issuer_pubkey`` — used by the relay server to refuse invites
    that don't name *this* issuer.
    """
    try:
        envelope = json.loads(_b64url_dec(inv_b64).decode("utf-8"))
        payload = envelope["payload"]
        sig_hex = envelope["signature"]
        invite_id = str(payload["invite_id"])
        issuer_pubkey = str(payload["issuer_pubkey"])
        issued_at = str(payload["issued_at"])
        expires_at = str(payload["expires_at"])
        max_uses = int(payload["max_uses"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    if len(issuer_pubkey) != KEY_HEX_LEN:
        return None
    if len(sig_hex) != SIG_HEX_LEN:
        return None
    if max_uses < 1:
        return None
    if expected_issuer_pubkey and expected_issuer_pubkey != issuer_pubkey:
        return None

    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(issuer_pubkey))
        pub.verify(bytes.fromhex(sig_hex), _canonical_bytes(payload))
    except (InvalidSignature, ValueError):
        return None

    now = now_iso or iso_now()
    if expires_at <= now:
        return None

    return PairInvite(
        invite_id=invite_id,
        issuer_pubkey=issuer_pubkey,
        issued_at=issued_at,
        expires_at=expires_at,
        max_uses=max_uses,
    )


def encode_pair_link(
    *,
    issuer_pubkey: str,
    issuer_node_id: str,
    relay_urls: list[str],
    signed_invite_b64: str,
) -> str:
    """Pack a complete ``nxg://pair#...`` link.

    ``issuer_node_id`` is the issuer's node UUID — the relay routes
    ``pair_invite_probe`` frames by node_id, so the link must carry it
    explicitly (the ``inv`` payload only carries the pubkey, which the
    relay doesn't index by).
    """
    if len(issuer_pubkey) != KEY_HEX_LEN:
        raise ValueError(f"issuer_pubkey must be {KEY_HEX_LEN} hex chars")
    if not (issuer_node_id or "").strip():
        raise ValueError("issuer_node_id required")
    blob = {
        "k": issuer_pubkey,
        "n": str(issuer_node_id),
        "r": [str(u) for u in (relay_urls or [])],
        "inv": signed_invite_b64,
        "v": PAIR_INVITE_VERSION,
    }
    return PAIR_LINK_SCHEME + _b64url_enc(_canonical_bytes(blob))


def decode_pair_link(link: str) -> Optional[dict]:
    """Parse ``nxg://pair#...``. Returns dict with keys ``k``, ``n``,
    ``r``, ``inv``, ``v`` on success; ``None`` on malformed input.

    Does NOT validate the signed invite — caller calls
    :func:`verify_pair_invite` on the ``inv`` field separately.
    """
    s = (link or "").strip()
    if not s.startswith(PAIR_LINK_SCHEME):
        return None
    try:
        blob = json.loads(_b64url_dec(s[len(PAIR_LINK_SCHEME):]).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    if str(blob.get("k", "")) == "" or not isinstance(blob.get("r"), list):
        return None
    if not str(blob.get("n", "")).strip():
        return None
    if not blob.get("inv"):
        return None
    return blob


__all__ = [
    "PAIR_LINK_SCHEME",
    "PAIR_INVITE_VERSION",
    "PairInvite",
    "sign_pair_invite",
    "verify_pair_invite",
    "encode_pair_link",
    "decode_pair_link",
]
