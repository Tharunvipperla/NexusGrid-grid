"""Counterparty-signed usage receipts.

A receipt is the unforgeable record of one resource exchange: the **consumer**
signs "I used N units from <provider>", so the provider can prove its
contribution and the consumer cannot repudiate its consumption. Because the
signer is always the receipt's ``consumer_pubkey``, a verifier needs no extra
trust — the signature *is* the proof, and no node can inflate its own standing
(it can't forge the counterparty's Ed25519 signature).

Reuses the group-identity Ed25519 primitives from :mod:`nexus.security.group_grant`.
"""

from __future__ import annotations

import base64
import hashlib

from nexus.security.group_grant import (
    _canonical_payload_bytes,
    _load_privkey,
    _load_pubkey,
)

# Domain separator — independent from grant/frame/challenge signing domains so a
# signature in one protocol can never be replayed as a receipt.
_RECEIPT_DOMAIN = b"nexus-usage-receipt-v1\x00"

# A worker proves the group pubkey it claims is really its own by
# signing the result attribution with the matching private key. Distinct domain.
_WORKER_PROOF_DOMAIN = b"nexus-worker-proof-v1\x00"

# The exact, ordered fields that are signed. Anything outside this set (e.g. a
# frame envelope) is NOT covered by the signature.
RECEIPT_FIELDS = (
    "receipt_id",
    "group_id",
    "provider_pubkey",
    "consumer_pubkey",
    "kind",
    "ref_id",
    "amount",
    "ts",
)


def canonical_body(receipt: dict) -> dict:
    """Project a receipt dict down to exactly the signed fields."""
    return {k: receipt.get(k) for k in RECEIPT_FIELDS}


def _material(receipt: dict) -> bytes:
    return _RECEIPT_DOMAIN + _canonical_payload_bytes(canonical_body(receipt))


def sign_receipt(receipt: dict, consumer_privkey_hex: str) -> str:
    """Sign *receipt* with the consumer's Ed25519 group key → base64 signature."""
    sig = _load_privkey(consumer_privkey_hex).sign(_material(receipt))
    return base64.b64encode(sig).decode("ascii")


def verify_receipt(receipt: dict, sig_b64: str) -> bool:
    """True iff *sig_b64* is a valid signature **by the receipt's own
    ``consumer_pubkey``** over the canonical body. Forged amounts, swapped
    parties, or a wrong signer all fail."""
    pub = str(receipt.get("consumer_pubkey") or "")
    if not pub:
        return False
    try:
        _load_pubkey(pub).verify(base64.b64decode(sig_b64), _material(receipt))
        return True
    except Exception:
        return False


_STATEMENT_DOMAIN = b"nexus-signed-statement-v1\x00"


def sign_statement(kind: str, payload: dict, privkey_hex: str) -> str:
    """Sign an authenticated control statement (e.g. a service-access
    request or grant update). ``kind`` is folded into the signed material so a
    signature for one statement type can't be replayed as another."""
    body = {"_kind": str(kind or ""), **{k: payload[k] for k in sorted(payload)}}
    sig = _load_privkey(privkey_hex).sign(
        _STATEMENT_DOMAIN + _canonical_payload_bytes(body)
    )
    return base64.b64encode(sig).decode("ascii")


def verify_statement(kind: str, payload: dict, sig_b64: str, signer_pubkey: str) -> bool:
    """True iff *sig_b64* is a valid signature by *signer_pubkey* over the
    (kind, payload) statement — so the named signer really sent it."""
    if not signer_pubkey or not sig_b64:
        return False
    body = {"_kind": str(kind or ""), **{k: payload[k] for k in sorted(payload)}}
    try:
        _load_pubkey(signer_pubkey).verify(
            base64.b64decode(sig_b64),
            _STATEMENT_DOMAIN + _canonical_payload_bytes(body),
        )
        return True
    except Exception:
        return False


# Security F-007: direct messages carry a sender signature so a forged DM can't
# impersonate a contact. The statement binds the message identity + a hash of the
# (decrypted) text, verified against the pubkey bound to ``from_uuid``.
STMT_DM = "dm"


def dm_statement_payload(msg_id: str, from_uuid: str, sent_at: str, text: str) -> dict:
    """Canonical signed fields for a direct message. ``text`` is the plaintext —
    both sender (pre-seal) and receiver (post-unseal) hash the same content."""
    return {
        "msg_id": str(msg_id or ""),
        "from_uuid": str(from_uuid or ""),
        "sent_at": str(sent_at or ""),
        "body_sha256": hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
    }


def _worker_proof_material(task_id: str, worker_pubkey: str, elapsed_secs: int) -> bytes:
    body = {
        "task_id": str(task_id or ""),
        "worker_pubkey": str(worker_pubkey or ""),
        "elapsed_secs": int(elapsed_secs or 0),
    }
    return _WORKER_PROOF_DOMAIN + _canonical_payload_bytes(body)


def sign_worker_proof(
    task_id: str, worker_pubkey: str, elapsed_secs: int, privkey_hex: str
) -> str:
    """Worker-side: prove ``worker_pubkey`` is ours by signing the result
    attribution with the matching Ed25519 group key → base64 signature."""
    sig = _load_privkey(privkey_hex).sign(
        _worker_proof_material(task_id, worker_pubkey, elapsed_secs)
    )
    return base64.b64encode(sig).decode("ascii")


def verify_worker_proof(
    task_id: str, worker_pubkey: str, elapsed_secs: int, sig_b64: str
) -> bool:
    """True iff *sig_b64* is a valid signature by ``worker_pubkey`` over this
    exact attribution — i.e. the claimant holds that pubkey's private key, so a
    node can't credit work to a key it doesn't own."""
    if not worker_pubkey or not sig_b64:
        return False
    try:
        _load_pubkey(worker_pubkey).verify(
            base64.b64decode(sig_b64),
            _worker_proof_material(task_id, worker_pubkey, elapsed_secs),
        )
        return True
    except Exception:
        return False


__all__ = [
    "RECEIPT_FIELDS",
    "canonical_body",
    "sign_receipt",
    "verify_receipt",
    "sign_worker_proof",
    "verify_worker_proof",
    "sign_statement",
    "verify_statement",
]
