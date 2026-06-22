"""Wave 49 — counterparty-signed usage receipts.

The pool ledger must be unforgeable: a node can't inflate its own contribution
or hide consumption, because every number is recomputed from receipts the
*consumer* signed. These tests pin (1) the signing primitive and (2) the
``store_and_apply`` path that the receipt is recomputed from.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.security import group_keys, tokens
from nexus.security.group_grant import generate_keypair
from nexus.security.usage_receipt import sign_receipt, verify_receipt
from nexus.storage import database, get_session
from nexus.storage.models import GroupComputeStat, UsageReceipt


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "groups.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def _receipt(consumer_pub, provider_pub, *, amount=10, rid="r1", gid="g1"):
    return {
        "receipt_id": rid,
        "group_id": gid,
        "provider_pubkey": provider_pub,
        "consumer_pubkey": consumer_pub,
        "kind": "compute",
        "ref_id": "task-1",
        "amount": amount,
        "ts": "2026-06-01T00:00:00Z",
    }


# --- signing primitive -------------------------------------------------------


def test_sign_verify_round_trip():
    consumer_priv, consumer_pub = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub)
    sig = sign_receipt(receipt, consumer_priv)
    assert verify_receipt(receipt, sig) is True


def test_verify_fails_on_tampered_amount():
    consumer_priv, consumer_pub = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub, amount=10)
    sig = sign_receipt(receipt, consumer_priv)
    receipt["amount"] = 9999  # inflate the contribution after signing
    assert verify_receipt(receipt, sig) is False


def test_verify_fails_on_wrong_signer():
    # A node signs a receipt but claims someone else is the consumer — the sig
    # is checked against consumer_pubkey, so it can't impersonate.
    attacker_priv, _ = generate_keypair()
    _, consumer_pub = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub)
    sig = sign_receipt(receipt, attacker_priv)
    assert verify_receipt(receipt, sig) is False


# --- store_and_apply ---------------------------------------------------------


def _get_stat(group_id, pubkey):
    async def _go():
        async with get_session() as s:
            return await s.get(GroupComputeStat, (group_id, pubkey))
    return asyncio.run(_go())


def _count_receipts():
    async def _go():
        from sqlalchemy import func, select
        async with get_session() as s:
            return (await s.execute(select(func.count(UsageReceipt.receipt_id)))).scalar_one()
    return asyncio.run(_go())


def test_apply_rejects_forgery_accepts_valid_and_dedupes(isolated_db):
    from nexus.runtime.usage_receipts import store_and_apply

    consumer_priv, consumer_pub = generate_keypair()
    attacker_priv, _ = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub)

    # Forged: signed by someone other than the named consumer → rejected, no row.
    forged = sign_receipt(receipt, attacker_priv)
    assert asyncio.run(store_and_apply(receipt, forged)) is False
    assert _count_receipts() == 0

    # Valid consumer signature → applied once.
    sig = sign_receipt(receipt, consumer_priv)
    assert asyncio.run(store_and_apply(receipt, sig)) is True
    assert _count_receipts() == 1

    # Same receipt_id again → deduped, not double-counted.
    assert asyncio.run(store_and_apply(receipt, sig)) is False
    assert _count_receipts() == 1


def test_verified_receipt_bumps_derived_totals(isolated_db):
    from nexus.runtime.usage_receipts import store_and_apply

    consumer_priv, consumer_pub = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub, amount=42)
    sig = sign_receipt(receipt, consumer_priv)
    assert asyncio.run(store_and_apply(receipt, sig)) is True

    prov = _get_stat("g1", provider_pub)
    cons = _get_stat("g1", consumer_pub)
    assert prov.tasks_contributed == 1 and prov.tasks_consumed == 0
    assert cons.tasks_consumed == 1 and cons.tasks_contributed == 0


def test_forged_receipt_bumps_nothing(isolated_db):
    from nexus.runtime.usage_receipts import store_and_apply

    _, consumer_pub = generate_keypair()
    attacker_priv, _ = generate_keypair()
    _, provider_pub = generate_keypair()
    receipt = _receipt(consumer_pub, provider_pub)
    forged = sign_receipt(receipt, attacker_priv)

    assert asyncio.run(store_and_apply(receipt, forged)) is False
    assert _get_stat("g1", provider_pub) is None
    assert _get_stat("g1", consumer_pub) is None
