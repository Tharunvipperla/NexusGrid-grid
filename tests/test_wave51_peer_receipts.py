"""Wave 51 — 1:1 peer-task usage receipts (no shared group).

The worker proves the group pubkey it claims by signing the result
attribution; the consumer credits it only on a valid proof, so a node can't
attribute work to a key it doesn't hold.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nexus.security import group_keys, tokens
from nexus.security.group_grant import generate_keypair
from nexus.security.usage_receipt import sign_worker_proof
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


def _count_receipts():
    async def _go():
        from sqlalchemy import func, select
        async with get_session() as s:
            return (await s.execute(select(func.count(UsageReceipt.receipt_id)))).scalar_one()
    return asyncio.run(_go())


def _make_task():
    # No target_groups -> a 1:1 peer task. `worker` is unreachable in tests, so
    # the receipt stores locally but the push is a best-effort no-op.
    return SimpleNamespace(
        id="task-1", worker="1.2.3.4:9000", env_vars="{}", status="completed",
    )


def test_peer_receipt_credits_proven_worker(isolated_db, monkeypatch):
    from nexus.runtime.usage_receipts import issue_compute_receipt

    # No-op the network push (no real provider in tests).
    monkeypatch.setattr(
        "nexus.runtime.usage_receipts._push_receipt",
        lambda *a, **k: asyncio.sleep(0),
    )

    worker_priv, worker_pub = generate_keypair()
    proof = sign_worker_proof("task-1", worker_pub, 25, worker_priv)
    asyncio.run(issue_compute_receipt(_make_task(), 25, worker_pub, proof))

    assert _count_receipts() == 1
    # group_id == "" peer receipt: NOT in the group stats table, but it does
    # count toward this node's verified global usage + bilateral exchange.
    from nexus.runtime.usage_receipts import global_usage_summary
    g = asyncio.run(global_usage_summary())
    assert g["compute_secs_consumed"] == 25 and g["tasks_consumed"] == 1

    from nexus.api.local import _exchange_with_pubkey
    x = asyncio.run(_exchange_with_pubkey(worker_pub))
    assert x["they_gave_compute_secs"] == 25

    async def _no_group_rows():
        async with get_session() as s:
            from sqlalchemy import func, select
            return (await s.execute(select(func.count(GroupComputeStat.group_id)))).scalar_one()
    assert asyncio.run(_no_group_rows()) == 0


def test_peer_receipt_rejects_unproven_pubkey(isolated_db, monkeypatch):
    from nexus.runtime.usage_receipts import issue_compute_receipt

    monkeypatch.setattr(
        "nexus.runtime.usage_receipts._push_receipt",
        lambda *a, **k: asyncio.sleep(0),
    )

    _, victim_pub = generate_keypair()       # a pubkey the caller does NOT own
    attacker_priv, _ = generate_keypair()
    forged = sign_worker_proof("task-1", victim_pub, 25, attacker_priv)  # wrong signer

    asyncio.run(issue_compute_receipt(_make_task(), 25, victim_pub, forged))
    assert _count_receipts() == 0  # unproven -> nothing credited

    # A missing proof is rejected too.
    asyncio.run(issue_compute_receipt(_make_task(), 25, victim_pub, ""))
    assert _count_receipts() == 0
