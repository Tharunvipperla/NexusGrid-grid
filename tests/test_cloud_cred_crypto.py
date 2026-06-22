"""Wave 6.1 — credential-blob crypto round-trip + tamper tests."""

from __future__ import annotations

import secrets

import pytest
from cryptography.exceptions import InvalidTag

from nexus.security import tokens
from nexus.security.cred_crypto import (
    EVICTION_NONCE_BYTES,
    derive_eviction_wrap_key,
    unwrap_credential_blob,
    unwrap_from_transit,
    wrap_credential_blob,
    wrap_for_transit,
)


@pytest.fixture(autouse=True)
def _isolate_signing_secret(tmp_path, monkeypatch):
    """Each test gets a fresh, throwaway signing secret directory."""
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    yield
    tokens._reset_for_testing()


# ---------------------------------------------------------------------------
# At-rest wrap
# ---------------------------------------------------------------------------

def test_at_rest_round_trip():
    plaintext = b'{"type":"service_account","private_key":"..."}'
    blob = wrap_credential_blob(plaintext)
    assert blob != plaintext
    assert unwrap_credential_blob(blob) == plaintext


def test_at_rest_random_nonce_per_wrap():
    plaintext = b"hello world"
    a = wrap_credential_blob(plaintext)
    b = wrap_credential_blob(plaintext)
    assert a != b
    assert unwrap_credential_blob(a) == plaintext
    assert unwrap_credential_blob(b) == plaintext


def test_at_rest_tamper_fails():
    blob = bytearray(wrap_credential_blob(b"top secret"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        unwrap_credential_blob(bytes(blob))


def test_at_rest_wrong_secret_fails(tmp_path):
    blob = wrap_credential_blob(b"top secret")
    # Rotate the on-disk signing secret + drop the cache → different key.
    tokens._reset_for_testing()
    (tmp_path / ".nexus_secret").write_text("z" * 64, encoding="utf-8")
    with pytest.raises(InvalidTag):
        unwrap_credential_blob(blob)


def test_at_rest_short_blob_rejected():
    with pytest.raises(ValueError):
        unwrap_credential_blob(b"\x00" * 4)


# ---------------------------------------------------------------------------
# Transit wrap
# ---------------------------------------------------------------------------

def test_transit_round_trip():
    peer_key = secrets.token_hex(32)
    nonce = secrets.token_bytes(EVICTION_NONCE_BYTES)
    plaintext = b'{"client_email":"sa@proj.iam.gserviceaccount.com"}'
    blob = wrap_for_transit(peer_key, nonce, plaintext)
    assert unwrap_from_transit(peer_key, nonce, blob) == plaintext


def test_transit_wrong_peer_key_fails():
    nonce = secrets.token_bytes(EVICTION_NONCE_BYTES)
    blob = wrap_for_transit(secrets.token_hex(32), nonce, b"x")
    with pytest.raises(InvalidTag):
        unwrap_from_transit(secrets.token_hex(32), nonce, blob)


def test_transit_wrong_nonce_fails():
    peer_key = secrets.token_hex(32)
    blob = wrap_for_transit(peer_key, secrets.token_bytes(EVICTION_NONCE_BYTES), b"x")
    with pytest.raises(InvalidTag):
        unwrap_from_transit(
            peer_key, secrets.token_bytes(EVICTION_NONCE_BYTES), blob
        )


def test_transit_replay_on_different_peer_pair_fails():
    """Captured WS frame is useless against a different peer pair."""
    nonce = secrets.token_bytes(EVICTION_NONCE_BYTES)
    a_key = secrets.token_hex(32)
    b_key = secrets.token_hex(32)
    blob = wrap_for_transit(a_key, nonce, b"creds")
    with pytest.raises(InvalidTag):
        unwrap_from_transit(b_key, nonce, blob)


def test_hkdf_deterministic():
    peer_key = secrets.token_hex(32)
    nonce = secrets.token_bytes(EVICTION_NONCE_BYTES)
    assert (
        derive_eviction_wrap_key(peer_key, nonce)
        == derive_eviction_wrap_key(peer_key, nonce)
    )


def test_hkdf_validates_input_lengths():
    with pytest.raises(ValueError):
        derive_eviction_wrap_key("", b"\x00" * EVICTION_NONCE_BYTES)
    with pytest.raises(ValueError):
        derive_eviction_wrap_key(secrets.token_hex(32), b"\x00" * 8)
