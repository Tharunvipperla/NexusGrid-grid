"""Wave 36.F.1 — pair-invite sign/verify + link round-trip + tamper checks."""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus.security.pair_invite import (
    PAIR_LINK_SCHEME,
    decode_pair_link,
    encode_pair_link,
    sign_pair_invite,
    verify_pair_invite,
)


def _gen_keypair() -> tuple[str, str]:
    """Returns (priv_hex, pub_hex). Ed25519, 32-byte each."""
    priv = Ed25519PrivateKey.generate()
    return (
        priv.private_bytes_raw().hex(),
        priv.public_key().public_bytes_raw().hex(),
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_sign_verify_round_trip_returns_payload():
    priv, pub = _gen_keypair()
    now = datetime.now(timezone.utc)
    blob = sign_pair_invite(
        invite_id="a" * 64,
        issuer_pubkey=pub,
        issued_at=_iso(now),
        expires_at=_iso(now + timedelta(days=7)),
        max_uses=1,
        issuer_privkey=priv,
    )
    inv = verify_pair_invite(blob)
    assert inv is not None
    assert inv.invite_id == "a" * 64
    assert inv.issuer_pubkey == pub
    assert inv.max_uses == 1


def test_verify_rejects_expired_invite():
    priv, pub = _gen_keypair()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    blob = sign_pair_invite(
        invite_id="b" * 64,
        issuer_pubkey=pub,
        issued_at=_iso(past - timedelta(days=8)),
        expires_at=_iso(past),
        max_uses=1,
        issuer_privkey=priv,
    )
    assert verify_pair_invite(blob) is None


def test_verify_rejects_wrong_issuer_pubkey():
    priv_a, pub_a = _gen_keypair()
    _, pub_b = _gen_keypair()
    now = datetime.now(timezone.utc)
    blob = sign_pair_invite(
        invite_id="c" * 64,
        issuer_pubkey=pub_a,
        issued_at=_iso(now),
        expires_at=_iso(now + timedelta(days=1)),
        max_uses=1,
        issuer_privkey=priv_a,
    )
    # Same signed blob, but caller expects pub_b → must reject.
    assert verify_pair_invite(blob, expected_issuer_pubkey=pub_b) is None
    # And the issuer match still works.
    assert verify_pair_invite(blob, expected_issuer_pubkey=pub_a) is not None


def test_sign_rejects_mismatched_keys():
    priv_a, _ = _gen_keypair()
    _, pub_b = _gen_keypair()
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        sign_pair_invite(
            invite_id="d" * 64,
            issuer_pubkey=pub_b,
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(days=1)),
            max_uses=1,
            issuer_privkey=priv_a,
        )


def test_verify_rejects_tampered_signature():
    priv, pub = _gen_keypair()
    now = datetime.now(timezone.utc)
    blob = sign_pair_invite(
        invite_id="e" * 64,
        issuer_pubkey=pub,
        issued_at=_iso(now),
        expires_at=_iso(now + timedelta(days=1)),
        max_uses=1,
        issuer_privkey=priv,
    )
    # Flip a character in the base64url blob — signature mismatch.
    tampered = blob[:-2] + ("A" if blob[-1] != "A" else "B") + blob[-1]
    assert verify_pair_invite(tampered) is None


def test_link_encode_decode_round_trip():
    priv, pub = _gen_keypair()
    now = datetime.now(timezone.utc)
    blob = sign_pair_invite(
        invite_id="f" * 64,
        issuer_pubkey=pub,
        issued_at=_iso(now),
        expires_at=_iso(now + timedelta(days=1)),
        max_uses=1,
        issuer_privkey=priv,
    )
    link = encode_pair_link(
        issuer_pubkey=pub,
        issuer_node_id="alice-node-uuid",
        relay_urls=["wss://relay-a.example", "wss://relay-b.example"],
        signed_invite_b64=blob,
    )
    assert link.startswith(PAIR_LINK_SCHEME)
    decoded = decode_pair_link(link)
    assert decoded is not None
    assert decoded["k"] == pub
    assert decoded["n"] == "alice-node-uuid"
    assert decoded["r"] == [
        "wss://relay-a.example", "wss://relay-b.example",
    ]
    assert decoded["inv"] == blob
    assert decoded["v"] == 1
    # And verification on the decoded inv still works.
    inv = verify_pair_invite(decoded["inv"])
    assert inv is not None and inv.invite_id == "f" * 64


def test_decode_pair_link_rejects_malformed():
    assert decode_pair_link("") is None
    assert decode_pair_link("https://example.com") is None
    assert decode_pair_link("nxg://pair#not-base64!") is None
    assert decode_pair_link("nxg://pair#") is None
