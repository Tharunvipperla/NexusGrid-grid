"""Wave 19 — opaque channel frame seal/open + dedupe cache."""

from __future__ import annotations

import base64
import secrets

import pytest

from nexus.security.group_ecies import NONCE_LEN, SYMKEY_LEN, mint_group_symkey
from nexus.security.group_frame import (
    FrameDedupeCache,
    FrameVerificationError,
    GroupFrame,
    open_frame,
    seal_frame,
)
from nexus.security.group_grant import generate_keypair, sign_grant
from nexus.utils.time import iso_now


# ---- test fixtures ------------------------------------------------------


def _make_grant(
    *,
    group_id: str,
    member_pubkey: str,
    admin_privkey: str,
    expires_at: str | None = None,
) -> bytes:
    from datetime import datetime, timedelta, timezone
    return sign_grant(
        group_id=group_id,
        member_pubkey=member_pubkey,
        roles=("member",),
        admin_privkey=admin_privkey,
        issued_at=iso_now(),
        expires_at=expires_at or (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat(),
        nonce=secrets.token_hex(16),
    )


@pytest.fixture
def setup_group():
    """Returns (group_id, symkey, admin_priv, admin_pub, member_priv, member_pub, grant_blob)."""
    admin_priv, admin_pub = generate_keypair()
    member_priv, member_pub = generate_keypair()
    group_id = "grp_" + secrets.token_hex(8)
    grant = _make_grant(
        group_id=group_id,
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
    )
    symkey = mint_group_symkey()
    return {
        "group_id": group_id,
        "symkey": symkey,
        "admin_priv": admin_priv,
        "admin_pub": admin_pub,
        "member_priv": member_priv,
        "member_pub": member_pub,
        "grant": grant,
    }


# ---- round-trip ---------------------------------------------------------


def test_seal_open_round_trip(setup_group):
    s = setup_group
    payload = b"hello channel " * 4
    frame = seal_frame(
        channel=s["group_id"],
        frame_type="pending.request",
        payload=payload,
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    opened = open_frame(
        frame, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]]
    )
    assert opened.payload == payload
    assert opened.channel == s["group_id"]
    assert opened.frame_type == "pending.request"
    assert opened.sender_pubkey == s["member_pub"]


def test_frame_id_is_unique(setup_group):
    s = setup_group
    ids = set()
    for _ in range(20):
        f = seal_frame(
            channel=s["group_id"],
            frame_type="x",
            payload=b"y",
            symkey=s["symkey"],
            sender_grant_blob=s["grant"],
            sender_privkey_hex=s["member_priv"],
        )
        ids.add(f.frame_id)
    assert len(ids) == 20


def test_envelope_does_not_contain_plaintext(setup_group):
    """The whole point of opacity — the wire envelope must not reveal payload."""
    s = setup_group
    secret = b"super-secret-marker-9f3a1d"
    frame = seal_frame(
        channel=s["group_id"],
        frame_type="x",
        payload=secret,
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    blob = repr(frame.to_dict()).encode()
    assert secret not in blob


def test_to_dict_from_dict_round_trip(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    g = GroupFrame.from_dict(f.to_dict())
    assert g == f


# ---- negative paths -----------------------------------------------------


def test_wrong_symkey_fails(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    other = mint_group_symkey()
    with pytest.raises(FrameVerificationError, match="aead decryption"):
        open_frame(f, symkey=other, group_admin_pubkeys=[s["admin_pub"]])


def test_tampered_ciphertext_fails(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p" * 32,
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    ct = bytearray(base64.b64decode(f.ciphertext_b64.encode("ascii")))
    ct[0] ^= 0xFF  # flip a byte
    tampered = GroupFrame(
        frame_id=f.frame_id,
        channel=f.channel,
        frame_type=f.frame_type,
        sender_grant_b64=f.sender_grant_b64,
        nonce_b64=f.nonce_b64,
        ciphertext_b64=base64.b64encode(bytes(ct)).decode("ascii"),
        signature_b64=f.signature_b64,
    )
    # Tampering the ciphertext invalidates the per-frame signature
    # (which covers ciphertext), so the signature check fires first.
    with pytest.raises(FrameVerificationError):
        open_frame(tampered, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_tampered_channel_fails(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    swapped = GroupFrame(
        frame_id=f.frame_id,
        channel="some_other_group",
        frame_type=f.frame_type,
        sender_grant_b64=f.sender_grant_b64,
        nonce_b64=f.nonce_b64,
        ciphertext_b64=f.ciphertext_b64,
        signature_b64=f.signature_b64,
    )
    # The grant's group_id no longer matches the (forged) channel.
    with pytest.raises(FrameVerificationError, match="grant.group_id"):
        open_frame(swapped, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_unknown_admin_rejects_grant(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    _, stranger_pub = generate_keypair()
    with pytest.raises(FrameVerificationError, match="grant failed"):
        open_frame(f, symkey=s["symkey"], group_admin_pubkeys=[stranger_pub])


def test_impersonation_attempt_fails(setup_group):
    """A member who has someone else's grant blob can't sign a frame
    claiming to be that other member — they don't have their privkey."""
    s = setup_group
    impersonator_priv, _ = generate_keypair()
    # Build a frame signed by the impersonator but carrying victim's grant.
    # seal_frame computes the signature with impersonator_priv; verification
    # tries victim's pubkey (from victim's grant) and fails.
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],  # victim's grant
        sender_privkey_hex=impersonator_priv,
    )
    with pytest.raises(FrameVerificationError, match="signature invalid"):
        open_frame(f, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_aad_binds_frame_id(setup_group):
    """Changing frame_id without resigning should fail signature first."""
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    forged = GroupFrame(
        frame_id="ffffffffffffffffffffffffffffffff",
        channel=f.channel,
        frame_type=f.frame_type,
        sender_grant_b64=f.sender_grant_b64,
        nonce_b64=f.nonce_b64,
        ciphertext_b64=f.ciphertext_b64,
        signature_b64=f.signature_b64,
    )
    with pytest.raises(FrameVerificationError):
        open_frame(forged, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_expired_grant_rejected(setup_group):
    """A grant that expired before the frame opens must not validate."""
    from datetime import datetime, timedelta, timezone
    s = setup_group
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    expired = sign_grant(
        group_id=s["group_id"],
        member_pubkey=s["member_pub"],
        roles=("member",),
        admin_privkey=s["admin_priv"],
        issued_at=iso_now(),
        expires_at=past,
        nonce=secrets.token_hex(16),
    )
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=expired,
        sender_privkey_hex=s["member_priv"],
    )
    with pytest.raises(FrameVerificationError, match="grant failed"):
        open_frame(f, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_seal_rejects_short_symkey(setup_group):
    s = setup_group
    with pytest.raises(ValueError):
        seal_frame(
            channel=s["group_id"],
            frame_type="t",
            payload=b"p",
            symkey=b"\x00" * 16,
            sender_grant_blob=s["grant"],
            sender_privkey_hex=s["member_priv"],
        )


def test_open_rejects_short_symkey(setup_group):
    s = setup_group
    f = seal_frame(
        channel=s["group_id"],
        frame_type="t",
        payload=b"p",
        symkey=s["symkey"],
        sender_grant_blob=s["grant"],
        sender_privkey_hex=s["member_priv"],
    )
    with pytest.raises(FrameVerificationError, match="symkey wrong length"):
        open_frame(f, symkey=b"\x00" * 16, group_admin_pubkeys=[s["admin_pub"]])


def test_malformed_base64_rejected(setup_group):
    s = setup_group
    bad = GroupFrame(
        frame_id="abc",
        channel=s["group_id"],
        frame_type="t",
        sender_grant_b64="!!! not base64 !!!",
        nonce_b64="AAAAAAAAAAAAAAAA",
        ciphertext_b64="AAAA",
        signature_b64="AAAA",
    )
    with pytest.raises(FrameVerificationError):
        open_frame(bad, symkey=s["symkey"], group_admin_pubkeys=[s["admin_pub"]])


def test_from_dict_missing_field_raises():
    with pytest.raises(ValueError, match="frame missing field"):
        GroupFrame.from_dict({"frame_id": "x"})


# ---- dedupe cache -------------------------------------------------------


def test_dedupe_first_seen_returns_false():
    c = FrameDedupeCache(capacity=10)
    assert c.seen("a") is False


def test_dedupe_second_seen_returns_true():
    c = FrameDedupeCache(capacity=10)
    c.seen("a")
    assert c.seen("a") is True


def test_dedupe_evicts_oldest_at_capacity():
    c = FrameDedupeCache(capacity=3)
    for fid in ("a", "b", "c"):
        assert c.seen(fid) is False
    # 'd' triggers eviction of 'a' (oldest).
    assert c.seen("d") is False
    # Cache is now {b, c, d}. Don't probe 'a' here — every miss
    # inserts, which would chain-evict the rest.
    assert len(c) == 3


def test_dedupe_evicted_id_is_treated_as_new_on_reprobe():
    c = FrameDedupeCache(capacity=2)
    c.seen("a")
    c.seen("b")
    c.seen("c")  # evicts 'a'
    # 'a' is no longer cached — reprobing returns False (not seen
    # recently) AND re-inserts (chaining evicts 'b').
    assert c.seen("a") is False


def test_dedupe_lru_bump_on_hit():
    """A hit on an old entry should refresh its position so it isn't
    the next eviction target."""
    c = FrameDedupeCache(capacity=3)
    c.seen("a"); c.seen("b"); c.seen("c")
    c.seen("a")  # bumps 'a' to MRU; now order is b, c, a
    c.seen("d")  # evicts 'b'
    assert c.seen("b") is False
    assert c.seen("a") is True  # still there thanks to the bump


def test_dedupe_empty_frame_id_ignored():
    c = FrameDedupeCache(capacity=10)
    assert c.seen("") is False
    assert c.seen("") is False  # never recorded


def test_dedupe_clear():
    c = FrameDedupeCache(capacity=10)
    c.seen("a"); c.seen("b")
    assert len(c) == 2
    c.clear()
    assert len(c) == 0
    assert c.seen("a") is False
