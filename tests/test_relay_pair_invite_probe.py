"""Relay-server pair-invite verification + redemption helpers.

Exercises the unit-level helpers in ``nexus/relay/server.py``. End-to-end
WS testing would require spinning up the full uvicorn server; not worth the
infrastructure overhead for a security-critical helper that's pure
function-of-input. The WS endpoint itself is just plumbing around these.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus.relay import server as relay
from nexus.security.pair_invite import sign_pair_invite


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    return (
        priv.private_bytes_raw().hex(),
        priv.public_key().public_bytes_raw().hex(),
    )


def _signed_blob(*, valid_for_seconds: int = 3600, max_uses: int = 1):
    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    blob = sign_pair_invite(
        invite_id="x" * 64,
        issuer_pubkey=pub,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=valid_for_seconds)).isoformat(),
        max_uses=max_uses,
        issuer_privkey=priv,
    )
    return blob, pub


def test_verify_pair_invite_returns_payload_on_valid_blob():
    blob, _ = _signed_blob(valid_for_seconds=120)
    payload = relay._verify_pair_invite(blob, now=time.time())
    assert payload is not None
    assert payload["invite_id"] == "x" * 64
    assert payload["max_uses"] == 1


def test_verify_pair_invite_rejects_expired():
    blob, _ = _signed_blob(valid_for_seconds=-60)  # expired 1 min ago
    assert relay._verify_pair_invite(blob, now=time.time()) is None


def test_verify_pair_invite_rejects_tampered_signature():
    blob, _ = _signed_blob(valid_for_seconds=120)
    # Flip a character anywhere in the base64url blob.
    tampered = blob[:-2] + ("A" if blob[-1] != "A" else "B") + blob[-1]
    assert relay._verify_pair_invite(tampered, now=time.time()) is None


def test_verify_pair_invite_rejects_malformed_base64():
    assert relay._verify_pair_invite("not-a-blob!!!", now=time.time()) is None
    assert relay._verify_pair_invite("", now=time.time()) is None


def test_claim_invite_returns_true_then_false():
    # Clear cache between tests since it's module-level state.
    relay._pair_redemptions.clear()
    loop = asyncio.new_event_loop()
    try:
        ok1 = loop.run_until_complete(
            relay._claim_invite("inv-1", "pub-1", time.time())
        )
        ok2 = loop.run_until_complete(
            relay._claim_invite("inv-1", "pub-1", time.time())
        )
        assert ok1 is True
        assert ok2 is False
    finally:
        loop.close()
        relay._pair_redemptions.clear()


def test_claim_invite_evicts_oldest_when_over_cap(monkeypatch):
    relay._pair_redemptions.clear()
    monkeypatch.setattr(relay, "PAIR_REDEMPTION_CACHE_MAX", 3)
    loop = asyncio.new_event_loop()
    try:
        # Fill cache + 1 over to trigger eviction
        for i, ts in enumerate([100.0, 200.0, 300.0, 400.0]):
            loop.run_until_complete(
                relay._claim_invite(f"inv-{i}", "pub", ts)
            )
        # The oldest (ts=100.0, inv-0) should be evicted; others remain.
        assert "inv-0" not in relay._pair_redemptions
        assert "inv-1" in relay._pair_redemptions
        assert "inv-2" in relay._pair_redemptions
        assert "inv-3" in relay._pair_redemptions
    finally:
        loop.close()
        relay._pair_redemptions.clear()
