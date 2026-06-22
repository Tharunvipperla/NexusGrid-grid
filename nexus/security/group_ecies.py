"""X25519 derivation + ECIES envelope for the group symkey.

The group symkey is a 32-byte secret shared by every member. It is
minted **lazily** by the founder on the first grant issuance and
delivered to each new member inside an ECIES envelope sealed to their
X25519 public key. Once cached locally, the symkey is later used
(+) to AEAD-encrypt channel frames so the relay sees only
ciphertext.

Each node's X25519 keypair is **deterministically derived** from its
existing ed25519 group key seed via HKDF-SHA256. No new key file is
needed — the same ``.nexus_group_key`` anchors both signing and
encryption identities. The derivation is domain-separated so it
cannot collide with future uses of the same seed.

The ECIES construction is the standard X25519 + HKDF + AEAD envelope:

    ephemeral_priv = X25519.generate()
    shared          = X25519(ephemeral_priv, recipient_pub)
    aead_key        = HKDF-SHA256(shared, info=DOMAIN, L=32)
    nonce           = 12 random bytes
    ct              = ChaCha20Poly1305(aead_key, nonce, plaintext, aad=None)
    envelope        = ephemeral_pub || nonce || ct

The envelope is unauthenticated WRT the sender on its own, but it
rides inside a founder-signed grant payload — the grant signature is
the authenticity check, the envelope just protects confidentiality.
"""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nexus.security.group_grant import KEY_HEX_LEN, _load_privkey  # type: ignore


# Domain separators — never reuse across primitives.
_X25519_DERIVE_INFO = b"nexus.x25519.v1"
_ECIES_HKDF_INFO = b"nexus.group_symkey.ecies.v1"

# Bytes-length constants.
SYMKEY_LEN = 32
NONCE_LEN = 12
X25519_KEY_LEN = 32


# ---- X25519 derivation --------------------------------------------------


def derive_x25519_privkey(ed25519_privkey_hex: str) -> X25519PrivateKey:
    """Return the X25519 private key derived from this node's ed25519 seed.

    ``ed25519_privkey_hex`` is the 64-char hex string returned by
    :func:`nexus.security.group_keys.get_local_group_privkey`. The
    raw 32-byte seed is hashed with HKDF-SHA256 and the resulting
    32 bytes feed :class:`X25519PrivateKey.from_private_bytes`, which
    applies the X25519 scalar clamping internally.
    """
    if len(ed25519_privkey_hex) != KEY_HEX_LEN:
        raise ValueError(f"ed25519 privkey must be {KEY_HEX_LEN} hex chars")
    # _load_privkey validates the hex + returns an Ed25519PrivateKey. We
    # don't actually need the Ed25519 object — just bytes.fromhex would do —
    # but routing through the validator keeps callers honest.
    _load_privkey(ed25519_privkey_hex)
    seed = bytes.fromhex(ed25519_privkey_hex)
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=X25519_KEY_LEN,
        salt=None,
        info=_X25519_DERIVE_INFO,
    ).derive(seed)
    return X25519PrivateKey.from_private_bytes(derived)


def derive_x25519_pubkey_hex(ed25519_privkey_hex: str) -> str:
    """Convenience: derive the X25519 pubkey (hex) for advertising."""
    priv = derive_x25519_privkey(ed25519_privkey_hex)
    return priv.public_key().public_bytes_raw().hex()


def _load_pubkey_hex(pubkey_hex: str) -> X25519PublicKey:
    if len(pubkey_hex) != KEY_HEX_LEN:
        raise ValueError(f"x25519 pubkey must be {KEY_HEX_LEN} hex chars")
    return X25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))


# ---- symkey mint --------------------------------------------------------


def mint_group_symkey() -> bytes:
    """Return a fresh 32-byte symmetric key for AEAD use."""
    return secrets.token_bytes(SYMKEY_LEN)


# ---- ECIES seal / open --------------------------------------------------


def _derive_aead_key(shared_secret: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=SYMKEY_LEN,
        salt=None,
        info=_ECIES_HKDF_INFO,
    ).derive(shared_secret)


def ecies_seal(plaintext: bytes, recipient_pubkey_hex: str) -> bytes:
    """Seal ``plaintext`` to ``recipient_pubkey_hex``.

    Envelope layout: ``ephemeral_pub (32) || nonce (12) || ct+tag``.
    The recipient calls :func:`ecies_open` to decrypt.
    """
    if not plaintext:
        raise ValueError("plaintext must be non-empty")
    recipient_pub = _load_pubkey_hex(recipient_pubkey_hex)
    ephemeral_priv = X25519PrivateKey.generate()
    ephemeral_pub_bytes = ephemeral_priv.public_key().public_bytes_raw()
    shared = ephemeral_priv.exchange(recipient_pub)
    aead_key = _derive_aead_key(shared)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = ChaCha20Poly1305(aead_key).encrypt(nonce, plaintext, associated_data=None)
    return ephemeral_pub_bytes + nonce + ct


def ecies_open(envelope: bytes, recipient_privkey_ed25519_hex: str) -> bytes:
    """Open an envelope sealed to this node's X25519 pubkey.

    ``recipient_privkey_ed25519_hex`` is the ed25519 hex (the X25519
    private key is derived inside, same as during seal).
    """
    if len(envelope) < X25519_KEY_LEN + NONCE_LEN + 16:
        raise ValueError("envelope too short")
    ephemeral_pub_bytes = envelope[:X25519_KEY_LEN]
    nonce = envelope[X25519_KEY_LEN : X25519_KEY_LEN + NONCE_LEN]
    ct = envelope[X25519_KEY_LEN + NONCE_LEN :]
    recipient_priv = derive_x25519_privkey(recipient_privkey_ed25519_hex)
    ephemeral_pub = X25519PublicKey.from_public_bytes(ephemeral_pub_bytes)
    shared = recipient_priv.exchange(ephemeral_pub)
    aead_key = _derive_aead_key(shared)
    return ChaCha20Poly1305(aead_key).decrypt(nonce, ct, associated_data=None)


__all__ = [
    "SYMKEY_LEN",
    "NONCE_LEN",
    "X25519_KEY_LEN",
    "derive_x25519_privkey",
    "derive_x25519_pubkey_hex",
    "mint_group_symkey",
    "ecies_seal",
    "ecies_open",
]
