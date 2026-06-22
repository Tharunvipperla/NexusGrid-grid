"""Wave 18 — X25519 derivation + ECIES seal/open primitives."""

from __future__ import annotations

import pytest

from nexus.security.group_ecies import (
    NONCE_LEN,
    SYMKEY_LEN,
    X25519_KEY_LEN,
    derive_x25519_privkey,
    derive_x25519_pubkey_hex,
    ecies_open,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_grant import generate_keypair


# ---- X25519 derivation determinism --------------------------------------


def test_x25519_pubkey_is_deterministic():
    ed_priv_hex, _ = generate_keypair()
    a = derive_x25519_pubkey_hex(ed_priv_hex)
    b = derive_x25519_pubkey_hex(ed_priv_hex)
    assert a == b
    assert len(a) == 64  # 32 bytes hex


def test_x25519_different_seeds_give_different_keys():
    pub1 = derive_x25519_pubkey_hex(generate_keypair()[0])
    pub2 = derive_x25519_pubkey_hex(generate_keypair()[0])
    assert pub1 != pub2


def test_derive_rejects_wrong_length_hex():
    with pytest.raises(ValueError):
        derive_x25519_privkey("deadbeef")


# ---- ECIES round-trip ---------------------------------------------------


def test_ecies_round_trip_recovers_plaintext():
    ed_priv_hex, _ = generate_keypair()
    pub_hex = derive_x25519_pubkey_hex(ed_priv_hex)
    plaintext = b"hello group symkey " * 4
    envelope = ecies_seal(plaintext, pub_hex)
    assert ecies_open(envelope, ed_priv_hex) == plaintext


def test_ecies_envelope_layout():
    ed_priv_hex, _ = generate_keypair()
    pub_hex = derive_x25519_pubkey_hex(ed_priv_hex)
    payload = mint_group_symkey()
    envelope = ecies_seal(payload, pub_hex)
    # ephemeral_pub (32) + nonce (12) + ct+tag (32 + 16 = 48 for a 32-byte payload).
    assert len(envelope) == X25519_KEY_LEN + NONCE_LEN + SYMKEY_LEN + 16


def test_ecies_different_envelopes_each_time():
    """Fresh ephemeral key per call should yield different envelopes."""
    ed_priv_hex, _ = generate_keypair()
    pub_hex = derive_x25519_pubkey_hex(ed_priv_hex)
    payload = b"same payload"
    a = ecies_seal(payload, pub_hex)
    b = ecies_seal(payload, pub_hex)
    assert a != b
    assert ecies_open(a, ed_priv_hex) == payload
    assert ecies_open(b, ed_priv_hex) == payload


def test_ecies_open_with_wrong_key_fails():
    pub_hex = derive_x25519_pubkey_hex(generate_keypair()[0])
    other_priv_hex, _ = generate_keypair()
    envelope = ecies_seal(b"secret", pub_hex)
    with pytest.raises(Exception):
        ecies_open(envelope, other_priv_hex)


def test_ecies_open_with_truncated_envelope_fails():
    ed_priv_hex, _ = generate_keypair()
    pub_hex = derive_x25519_pubkey_hex(ed_priv_hex)
    envelope = ecies_seal(b"x" * 32, pub_hex)
    with pytest.raises(ValueError):
        ecies_open(envelope[:30], ed_priv_hex)


def test_ecies_seal_rejects_empty_plaintext():
    pub_hex = derive_x25519_pubkey_hex(generate_keypair()[0])
    with pytest.raises(ValueError):
        ecies_seal(b"", pub_hex)


def test_mint_returns_32_bytes():
    k = mint_group_symkey()
    assert isinstance(k, bytes)
    assert len(k) == SYMKEY_LEN
