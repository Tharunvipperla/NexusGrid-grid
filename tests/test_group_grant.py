"""Wave 15.2 — grant envelope crypto.

Pure-unit tests for :mod:`nexus.security.group_grant`. No DB, no API
— just the Ed25519 sign/verify primitives.
"""

from __future__ import annotations

import json
import secrets

from nexus.security import group_grant


def _admin_keys() -> tuple[str, str]:
    return group_grant.generate_keypair()


def _member_keys() -> tuple[str, str]:
    return group_grant.generate_keypair()


def _sample_grant(
    *,
    admin_priv: str,
    member_pub: str,
    expires_at: str = "2099-01-01T00:00:00+00:00",
    issued_at: str = "2026-05-19T00:00:00+00:00",
    roles: tuple[str, ...] = ("member",),
    nonce: str = "deadbeef",
) -> bytes:
    return group_grant.sign_grant(
        group_id="g1",
        member_pubkey=member_pub,
        roles=roles,
        admin_privkey=admin_priv,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


# ---- keypair helpers -----------------------------------------------------


def test_generate_keypair_returns_64_hex_chars():
    priv, pub = group_grant.generate_keypair()
    assert len(priv) == group_grant.KEY_HEX_LEN == 64
    assert len(pub) == group_grant.KEY_HEX_LEN == 64
    int(priv, 16)
    int(pub, 16)


def test_derive_pubkey_matches_generated_pubkey():
    priv, pub = group_grant.generate_keypair()
    assert group_grant.derive_pubkey(priv) == pub


def test_two_generate_calls_produce_different_keys():
    a = group_grant.generate_keypair()
    b = group_grant.generate_keypair()
    assert a != b


# ---- grant sign + verify -------------------------------------------------


def test_sign_and_verify_grant_roundtrip():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    grant = group_grant.verify_grant(blob, group_admin_pubkeys=[admin_pub])

    assert grant is not None
    assert grant.group_id == "g1"
    assert grant.member_pubkey == member_pub
    assert grant.issued_by_pubkey == admin_pub
    assert grant.roles == ("member",)


def test_verify_grant_returns_none_for_tampered_payload():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    envelope = json.loads(blob.decode("utf-8"))
    envelope["payload"]["roles"] = ["admin"]
    tampered = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert group_grant.verify_grant(tampered, group_admin_pubkeys=[admin_pub]) is None


def test_verify_grant_returns_none_for_tampered_signature():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    envelope = json.loads(blob.decode("utf-8"))
    flipped = list(bytes.fromhex(envelope["signature"]))
    flipped[0] ^= 0x01
    envelope["signature"] = bytes(flipped).hex()
    tampered = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    assert group_grant.verify_grant(tampered, group_admin_pubkeys=[admin_pub]) is None


def test_verify_grant_rejects_unknown_issuer():
    admin_priv, _ = _admin_keys()
    other_pub = group_grant.generate_keypair()[1]
    _, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)

    # admin_pub is missing from the trusted set; verification fails.
    assert group_grant.verify_grant(blob, group_admin_pubkeys=[other_pub]) is None


def test_verify_grant_returns_none_when_admin_set_empty():
    admin_priv, _ = _admin_keys()
    _, member_pub = _member_keys()
    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    assert group_grant.verify_grant(blob, group_admin_pubkeys=[]) is None


def test_verify_grant_returns_none_when_expired():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()

    blob = _sample_grant(
        admin_priv=admin_priv,
        member_pub=member_pub,
        issued_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-02T00:00:00+00:00",
    )

    # Pin "now" past expiry — expect None.
    assert (
        group_grant.verify_grant(
            blob,
            group_admin_pubkeys=[admin_pub],
            now_iso="2026-01-03T00:00:00+00:00",
        )
        is None
    )
    # Pin "now" before expiry — verifies.
    assert (
        group_grant.verify_grant(
            blob,
            group_admin_pubkeys=[admin_pub],
            now_iso="2026-01-01T12:00:00+00:00",
        )
        is not None
    )


def test_verify_grant_rejects_malformed_blob():
    _, admin_pub = _admin_keys()
    assert group_grant.verify_grant(b"not json", group_admin_pubkeys=[admin_pub]) is None
    assert group_grant.verify_grant(b"{}", group_admin_pubkeys=[admin_pub]) is None
    assert (
        group_grant.verify_grant(
            b'{"payload": {}, "signature": "00"}',
            group_admin_pubkeys=[admin_pub],
        )
        is None
    )


def test_verify_grant_accepts_any_admin_in_set():
    admin_a_priv, admin_a_pub = _admin_keys()
    _, admin_b_pub = _admin_keys()
    _, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_a_priv, member_pub=member_pub)

    # Trust both admins — the one who actually signed wins.
    grant = group_grant.verify_grant(
        blob, group_admin_pubkeys=[admin_a_pub, admin_b_pub]
    )
    assert grant is not None and grant.issued_by_pubkey == admin_a_pub


def test_distinct_nonces_produce_distinct_signatures():
    admin_priv, _ = _admin_keys()
    _, member_pub = _member_keys()

    a = _sample_grant(admin_priv=admin_priv, member_pub=member_pub, nonce="aa")
    b = _sample_grant(admin_priv=admin_priv, member_pub=member_pub, nonce="bb")
    assert a != b


def test_roles_change_invalidates_old_signature():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()

    a = _sample_grant(admin_priv=admin_priv, member_pub=member_pub, roles=("member",))
    b = _sample_grant(
        admin_priv=admin_priv,
        member_pub=member_pub,
        roles=("member", "admin"),
        nonce="deadbeef",
    )

    # Sigs differ because the payload differs.
    assert json.loads(a)["signature"] != json.loads(b)["signature"]
    # Both still verify cleanly with the right admin set.
    assert group_grant.verify_grant(a, group_admin_pubkeys=[admin_pub]) is not None
    assert group_grant.verify_grant(b, group_admin_pubkeys=[admin_pub]) is not None


# ---- challenge-response --------------------------------------------------


def test_challenge_roundtrip():
    admin_priv, admin_pub = _admin_keys()
    member_priv, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    nonce = secrets.token_bytes(16)
    sig = group_grant.sign_challenge(
        grant_blob=blob, nonce=nonce, member_privkey=member_priv
    )

    assert group_grant.verify_challenge(
        grant_blob=blob,
        nonce=nonce,
        signature=sig,
        group_admin_pubkeys=[admin_pub],
    )


def test_verify_challenge_rejects_wrong_private_key():
    admin_priv, admin_pub = _admin_keys()
    _, member_pub = _member_keys()
    attacker_priv, _ = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    nonce = secrets.token_bytes(16)
    bad_sig = group_grant.sign_challenge(
        grant_blob=blob, nonce=nonce, member_privkey=attacker_priv
    )

    assert not group_grant.verify_challenge(
        grant_blob=blob,
        nonce=nonce,
        signature=bad_sig,
        group_admin_pubkeys=[admin_pub],
    )


def test_verify_challenge_rejects_different_nonce():
    admin_priv, admin_pub = _admin_keys()
    member_priv, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    sig = group_grant.sign_challenge(
        grant_blob=blob, nonce=b"\x01" * 16, member_privkey=member_priv
    )

    # Verifier uses a different nonce than the signer — replay defeated.
    assert not group_grant.verify_challenge(
        grant_blob=blob,
        nonce=b"\x02" * 16,
        signature=sig,
        group_admin_pubkeys=[admin_pub],
    )


def test_verify_challenge_rejects_different_grant_blob():
    """Stolen challenge sig cannot be reused against a different grant
    (e.g. a longer-lived re-signed copy of the same grant)."""
    admin_priv, admin_pub = _admin_keys()
    member_priv, member_pub = _member_keys()

    blob_a = _sample_grant(
        admin_priv=admin_priv, member_pub=member_pub, nonce="aa"
    )
    blob_b = _sample_grant(
        admin_priv=admin_priv, member_pub=member_pub, nonce="bb"
    )

    nonce = secrets.token_bytes(16)
    sig_for_a = group_grant.sign_challenge(
        grant_blob=blob_a, nonce=nonce, member_privkey=member_priv
    )

    assert group_grant.verify_challenge(
        grant_blob=blob_a,
        nonce=nonce,
        signature=sig_for_a,
        group_admin_pubkeys=[admin_pub],
    )
    assert not group_grant.verify_challenge(
        grant_blob=blob_b,
        nonce=nonce,
        signature=sig_for_a,
        group_admin_pubkeys=[admin_pub],
    )


def test_verify_challenge_returns_false_when_grant_expired():
    admin_priv, admin_pub = _admin_keys()
    member_priv, member_pub = _member_keys()

    blob = _sample_grant(
        admin_priv=admin_priv,
        member_pub=member_pub,
        issued_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-02T00:00:00+00:00",
    )
    nonce = secrets.token_bytes(16)
    sig = group_grant.sign_challenge(
        grant_blob=blob, nonce=nonce, member_privkey=member_priv
    )

    assert not group_grant.verify_challenge(
        grant_blob=blob,
        nonce=nonce,
        signature=sig,
        group_admin_pubkeys=[admin_pub],
        now_iso="2026-01-03T00:00:00+00:00",
    )


def test_verify_challenge_returns_false_when_grant_signature_tampered():
    admin_priv, admin_pub = _admin_keys()
    member_priv, member_pub = _member_keys()

    blob = _sample_grant(admin_priv=admin_priv, member_pub=member_pub)
    envelope = json.loads(blob.decode("utf-8"))
    flipped = list(bytes.fromhex(envelope["signature"]))
    flipped[0] ^= 0x01
    envelope["signature"] = bytes(flipped).hex()
    tampered = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")

    nonce = secrets.token_bytes(16)
    sig = group_grant.sign_challenge(
        grant_blob=tampered, nonce=nonce, member_privkey=member_priv
    )
    assert not group_grant.verify_challenge(
        grant_blob=tampered,
        nonce=nonce,
        signature=sig,
        group_admin_pubkeys=[admin_pub],
    )


def test_sign_grant_rejects_bad_member_pubkey_length():
    admin_priv, _ = _admin_keys()
    import pytest

    with pytest.raises(ValueError):
        group_grant.sign_grant(
            group_id="g1",
            member_pubkey="too-short",
            roles=("member",),
            admin_privkey=admin_priv,
            issued_at="2026-05-19T00:00:00+00:00",
            expires_at="2099-01-01T00:00:00+00:00",
            nonce="aa",
        )
