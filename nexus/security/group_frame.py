"""Opaque AEAD channel frames for group traffic.

A *frame* is what one member publishes to a group channel. Relays
forward frames without ever decrypting them; only members holding the
group symkey can read the payload.

The envelope (the part visible to the relay) carries:

* ``frame_id`` — UUID4, used by subscribers for replay dedupe.
* ``channel`` — the ``group_id``; binds the frame to its group.
* ``frame_type`` — e.g. ``"pending.request"``, ``"roster.update"``.
* ``sender_grant_b64`` — the sender's grant blob; proves group membership.
* ``nonce_b64`` — 12-byte ChaCha20-Poly1305 nonce.
* ``ciphertext_b64`` — AEAD-encrypted payload.
* ``signature_b64`` — sender's ed25519 signature over the envelope's
  invariant fields (see :data:`_FRAME_SIG_DOMAIN`). Prevents one member
  from impersonating another by replaying a stolen grant blob.

Verification is a four-step check:

1. Parse envelope shape; reject malformed.
2. Verify the grant blob via :func:`group_grant.verify_grant` — proves
   sender is in the group + identifies their pubkey.
3. Verify the per-frame signature against the verified grant's
   ``member_pubkey`` over the canonical signature material — proves
   *this specific* frame came from the holder of that private key.
4. AEAD-decrypt the ciphertext using the group symkey, with the
   channel/type/frame_id bound as associated data so a frame can't be
   replayed from one channel or one type to another.

The dedupe cache (:class:`FrameDedupeCache`) tracks recently-seen
``frame_id`` values so a subscriber across multiple relay bindings
processes each frame once.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from nexus.security import group_grant
from nexus.security.group_ecies import NONCE_LEN, SYMKEY_LEN
from nexus.security.group_grant import (
    Grant,
    _load_privkey,
    _load_pubkey,
)


# Domain separator for per-frame signatures. Independent from the grant
# signature domain to prevent cross-protocol replay.
_FRAME_SIG_DOMAIN = b"nexus.group.frame.sig.v1|"

# AEAD associated-data domain. Folded with channel/type/frame_id so an
# attacker can't transplant a ciphertext across channels.
_FRAME_AAD_DOMAIN = b"nexus.group.frame.aad.v1|"


# ---- envelope -----------------------------------------------------------


@dataclass(frozen=True)
class GroupFrame:
    """The wire shape of a frame. Always base64 / utf-8 safe."""

    frame_id: str
    channel: str
    frame_type: str
    sender_grant_b64: str
    nonce_b64: str
    ciphertext_b64: str
    signature_b64: str

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "channel": self.channel,
            "frame_type": self.frame_type,
            "sender_grant_b64": self.sender_grant_b64,
            "nonce_b64": self.nonce_b64,
            "ciphertext_b64": self.ciphertext_b64,
            "signature_b64": self.signature_b64,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GroupFrame":
        try:
            return cls(
                frame_id=str(d["frame_id"]),
                channel=str(d["channel"]),
                frame_type=str(d["frame_type"]),
                sender_grant_b64=str(d["sender_grant_b64"]),
                nonce_b64=str(d["nonce_b64"]),
                ciphertext_b64=str(d["ciphertext_b64"]),
                signature_b64=str(d["signature_b64"]),
            )
        except KeyError as exc:
            raise ValueError(f"frame missing field: {exc}") from exc


# ---- AAD + signature material ------------------------------------------


def _aad(channel: str, frame_type: str, frame_id: str) -> bytes:
    return (
        _FRAME_AAD_DOMAIN
        + channel.encode("utf-8")
        + b"|"
        + frame_type.encode("utf-8")
        + b"|"
        + frame_id.encode("utf-8")
    )


def _signature_material(
    *, frame_id: str, channel: str, frame_type: str, nonce: bytes, ciphertext: bytes
) -> bytes:
    return (
        _FRAME_SIG_DOMAIN
        + frame_id.encode("utf-8")
        + b"|"
        + channel.encode("utf-8")
        + b"|"
        + frame_type.encode("utf-8")
        + b"|"
        + nonce
        + b"|"
        + ciphertext
    )


# ---- seal / open --------------------------------------------------------


def seal_frame(
    *,
    channel: str,
    frame_type: str,
    payload: bytes,
    symkey: bytes,
    sender_grant_blob: bytes,
    sender_privkey_hex: str,
    frame_id: Optional[str] = None,
) -> GroupFrame:
    """Encrypt + sign a payload for publication to a group channel.

    Caller supplies the symkey (typically opened from
    ``Group.group_symkey_enc``) and the sender's grant blob + ed25519
    private key. ``frame_id`` is generated if not provided.
    """
    if not channel:
        raise ValueError("channel must be non-empty")
    if not frame_type:
        raise ValueError("frame_type must be non-empty")
    if len(symkey) != SYMKEY_LEN:
        raise ValueError(f"symkey must be {SYMKEY_LEN} bytes")
    if not sender_grant_blob:
        raise ValueError("sender_grant_blob must be non-empty")

    fid = frame_id or uuid.uuid4().hex
    nonce = secrets.token_bytes(NONCE_LEN)
    aad = _aad(channel, frame_type, fid)
    ciphertext = ChaCha20Poly1305(symkey).encrypt(nonce, payload, aad)

    sig_material = _signature_material(
        frame_id=fid,
        channel=channel,
        frame_type=frame_type,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    signature = _load_privkey(sender_privkey_hex).sign(sig_material)

    return GroupFrame(
        frame_id=fid,
        channel=channel,
        frame_type=frame_type,
        sender_grant_b64=base64.b64encode(sender_grant_blob).decode("ascii"),
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
        signature_b64=base64.b64encode(signature).decode("ascii"),
    )


@dataclass(frozen=True)
class OpenedFrame:
    """Successful result of :func:`open_frame`."""

    frame_id: str
    channel: str
    frame_type: str
    sender_pubkey: str
    sender_grant: Grant
    payload: bytes


class FrameVerificationError(Exception):
    """Raised when a frame fails any verification step.

    The message names the failed step so callers can log + move on
    (a single malformed frame should never crash the subscriber loop).
    """


def open_frame(
    frame: GroupFrame,
    *,
    symkey: bytes,
    group_admin_pubkeys: Iterable[str],
    now_iso: Optional[str] = None,
) -> OpenedFrame:
    """Verify + decrypt a frame.

    Raises :class:`FrameVerificationError` on any failure. Callers
    should catch and either drop the frame (best practice — never trust
    unverified bytes) or log + alert.
    """
    if len(symkey) != SYMKEY_LEN:
        raise FrameVerificationError("symkey wrong length")

    # Decode base64 fields up front.
    try:
        grant_blob = base64.b64decode(frame.sender_grant_b64.encode("ascii"))
        nonce = base64.b64decode(frame.nonce_b64.encode("ascii"))
        ciphertext = base64.b64decode(frame.ciphertext_b64.encode("ascii"))
        signature = base64.b64decode(frame.signature_b64.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise FrameVerificationError(f"base64 decode failed: {exc}") from exc

    if len(nonce) != NONCE_LEN:
        raise FrameVerificationError("nonce wrong length")

    # Verify grant — membership + admin signature + expiry.
    grant = group_grant.verify_grant(
        grant_blob,
        group_admin_pubkeys=group_admin_pubkeys,
        now_iso=now_iso,
    )
    if grant is None:
        raise FrameVerificationError("grant failed verification")
    if grant.group_id != frame.channel:
        # A grant for one group can't be used to publish on another.
        raise FrameVerificationError("grant.group_id != frame.channel")

    # Verify per-frame signature against the grant's member_pubkey.
    sig_material = _signature_material(
        frame_id=frame.frame_id,
        channel=frame.channel,
        frame_type=frame.frame_type,
        nonce=nonce,
        ciphertext=ciphertext,
    )
    try:
        _load_pubkey(grant.member_pubkey).verify(signature, sig_material)
    except (InvalidSignature, ValueError) as exc:
        raise FrameVerificationError("frame signature invalid") from exc

    # AEAD-decrypt with AAD bound to channel + type + frame_id.
    aad = _aad(frame.channel, frame.frame_type, frame.frame_id)
    try:
        plaintext = ChaCha20Poly1305(symkey).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # InvalidTag from cryptography
        raise FrameVerificationError("aead decryption failed") from exc

    return OpenedFrame(
        frame_id=frame.frame_id,
        channel=frame.channel,
        frame_type=frame.frame_type,
        sender_pubkey=grant.member_pubkey,
        sender_grant=grant,
        payload=plaintext,
    )


# ---- dedupe cache -------------------------------------------------------


@dataclass
class FrameDedupeCache:
    """Bounded LRU set of seen frame_ids.

    Subscribers consult :meth:`seen` on every inbound frame and skip
    those that match. Because each member subscribes to every relay
    binding for a channel, the same frame may arrive 2+ times — this
    cache makes processing idempotent.
    """

    capacity: int = 1024
    _seen: "OrderedDict[str, None]" = field(default_factory=OrderedDict)

    def seen(self, frame_id: str) -> bool:
        """Return True if ``frame_id`` was previously recorded.

        Always records the id (LRU bump on a hit; insert on a miss),
        evicting the oldest entry if the cache is at capacity.
        """
        if not frame_id:
            return False
        if frame_id in self._seen:
            # LRU bump.
            self._seen.move_to_end(frame_id)
            return True
        self._seen[frame_id] = None
        if len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()


__all__ = [
    "GroupFrame",
    "OpenedFrame",
    "FrameVerificationError",
    "FrameDedupeCache",
    "seal_frame",
    "open_frame",
]
