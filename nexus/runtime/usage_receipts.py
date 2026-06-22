"""Usage-receipt application, issuance, and distribution.

The pool-usage ledger is rebuilt from counterparty-signed receipts
(:mod:`nexus.security.usage_receipt`). ``store_and_apply`` is the single,
idempotent entry point every delivery path funnels through (local issue, peer
push, group frame, catch-up replay): it verifies the consumer signature, stores
the receipt once, and bumps the derived ``GroupComputeStat`` + this node's time
buckets exactly once.
"""

from __future__ import annotations

import logging
import uuid

from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.security.usage_receipt import sign_receipt, verify_receipt
from nexus.storage import get_session
from nexus.storage.models import GroupComputeStat, GroupMember, UsageReceipt
from nexus.utils.time import iso_now

_log = logging.getLogger("nexus.runtime.usage_receipts")


def make_receipt(
    *, group_id: str, provider_pubkey: str, kind: str, ref_id: str, amount: int,
) -> tuple[dict, str]:
    """Build + sign a receipt as the local node (the consumer). Returns
    ``(receipt_dict, sig_b64)``."""
    receipt = {
        "receipt_id": uuid.uuid4().hex,
        "group_id": group_id or "",
        "provider_pubkey": provider_pubkey,
        "consumer_pubkey": get_local_group_pubkey(),
        "kind": kind,
        "ref_id": ref_id,
        "amount": int(amount),
        "ts": iso_now(),
    }
    return receipt, sign_receipt(receipt, get_local_group_privkey())


async def store_and_apply(receipt: dict, sig: str) -> bool:
    """Verify + persist a receipt and fold it into the derived ledger, exactly
    once. Returns True if it was newly applied, False if invalid or a duplicate.
    """
    if not verify_receipt(receipt, sig):
        _log.debug("rejected receipt with bad consumer signature")
        return False
    rid = str(receipt.get("receipt_id") or "")
    provider = str(receipt.get("provider_pubkey") or "")
    consumer = str(receipt.get("consumer_pubkey") or "")
    gid = str(receipt.get("group_id") or "")
    kind = str(receipt.get("kind") or "")
    amount = int(receipt.get("amount") or 0)
    if not rid or not provider or not consumer:
        return False

    async with get_session() as session:
        if await session.get(UsageReceipt, rid) is not None:
            return False  # dedupe — already counted on this node
        session.add(UsageReceipt(
            receipt_id=rid, group_id=gid, provider_pubkey=provider,
            consumer_pubkey=consumer, kind=kind, ref_id=str(receipt.get("ref_id") or ""),
            amount=amount, ts=str(receipt.get("ts") or iso_now()), sig=sig,
        ))
        # Derived per-member totals for the shared "Pool usage" table (groups
        # only; compute tasks are the live counts the table shows).
        if gid and kind == "compute":
            await _bump_stat(session, gid, provider, contributed=1)
            await _bump_stat(session, gid, consumer, consumed=1)
        await session.commit()

    # Time-series buckets are this node's own history — feed them only when we
    # are a party to the exchange.
    me = get_local_group_pubkey()
    if me in (provider, consumer):
        from nexus.runtime.group_compute_telemetry import record
        bucket_gid = gid or "peer"
        if kind == "compute":
            if provider == me:
                await record(bucket_gid, tasks_contributed=1,
                             compute_secs_contributed=amount)
            if consumer == me:
                await record(bucket_gid, tasks_consumed=1,
                             compute_secs_consumed=amount)
        elif kind == "storage":
            if provider == me:
                await record(bucket_gid, storage_bytes_hosted=amount)
            if consumer == me:
                await record(bucket_gid, storage_bytes_used=amount)
    return True


async def _bump_stat(session, group_id, member, *, contributed=0, consumed=0):
    row = await session.get(GroupComputeStat, (group_id, member))
    if row is None:
        row = GroupComputeStat(
            group_id=group_id, member_pubkey=member,
            tasks_contributed=0, tasks_consumed=0,
        )
        session.add(row)
    row.tasks_contributed = int(row.tasks_contributed or 0) + int(contributed)
    row.tasks_consumed = int(row.tasks_consumed or 0) + int(consumed)
    row.updated_at = iso_now()


async def global_usage_summary() -> dict:
    """This node's receipt-derived global pool usage, all groups +
    peers, all time. Counterparty-signed, so the node can't inflate it."""
    from sqlalchemy import select
    from nexus.storage.models import UsageReceipt

    me = get_local_group_pubkey()
    out = {"compute_secs_contributed": 0, "compute_secs_consumed": 0,
           "storage_bytes_hosted": 0, "storage_bytes_used": 0,
           "tasks_contributed": 0, "tasks_consumed": 0,
           "service_secs_served": 0, "service_secs_used": 0,
           "service_bytes_served": 0, "service_bytes_used": 0,
           "service_users": 0,
           "peers_helped": 0, "peers_used": 0}
    helped: set[str] = set()
    used: set[str] = set()
    service_users: set[str] = set()
    async with get_session() as session:
        rows = (await session.execute(
            select(UsageReceipt).where(
                (UsageReceipt.provider_pubkey == me)
                | (UsageReceipt.consumer_pubkey == me)
            )
        )).scalars().all()
    for r in rows:
        amt = int(r.amount or 0)
        i_provided = r.provider_pubkey == me
        if i_provided:
            helped.add(r.consumer_pubkey)
        else:
            used.add(r.provider_pubkey)
        if r.kind == "compute":
            if i_provided:
                out["compute_secs_contributed"] += amt
                out["tasks_contributed"] += 1
            else:
                out["compute_secs_consumed"] += amt
                out["tasks_consumed"] += 1
        elif r.kind == "storage":
            out["storage_bytes_hosted" if i_provided else "storage_bytes_used"] += amt
        elif r.kind == "service":
            out["service_secs_served" if i_provided else "service_secs_used"] += amt
            if i_provided:
                service_users.add(r.consumer_pubkey)
        elif r.kind == "service_bytes":
            out["service_bytes_served" if i_provided else "service_bytes_used"] += amt
    out["peers_helped"] = len(helped)
    out["peers_used"] = len(used)
    out["service_users"] = len(service_users)
    return out


async def _provider_pubkey_for(group_id: str, worker_uuid: str, worker_ip: str) -> str:
    """Resolve the worker's group Ed25519 pubkey from the roster."""
    from sqlalchemy import select
    async with get_session() as session:
        q = select(GroupMember.pubkey).where(GroupMember.group_id == group_id)
        if worker_uuid:
            row = (await session.execute(
                q.where(GroupMember.node_id == worker_uuid).limit(1)
            )).scalar_one_or_none()
            if row:
                return row
        if worker_ip:
            row = (await session.execute(
                q.where(GroupMember.peer_address == worker_ip).limit(1)
            )).scalar_one_or_none()
            if row:
                return row
    return ""


async def issue_compute_receipt(
    task, elapsed_secs: int, worker_pubkey: str = "", worker_proof: str = "",
) -> None:
    """Consumer-side: on a finished task, sign a receipt crediting the worker
    (provider) and distribute it. Group tasks resolve the provider from the
    trusted roster and broadcast; 1:1 peer tasks credit the worker's
    proven pubkey and push straight to it. Best-effort; never raises."""
    secs = max(0, int(elapsed_secs or 0))
    me = get_local_group_pubkey()
    try:
        from nexus.tasks.metadata import extract_task_metadata
        meta = extract_task_metadata(task)
        groups = meta.get("target_groups") or []
        worker = str(getattr(task, "worker", "") or "")
        ref_id = str(getattr(task, "id", ""))

        if groups:
            gid = ""
            provider = ""
            for g in groups:
                provider = await _provider_pubkey_for(g, worker, worker)
                if provider:
                    gid = g
                    break
            if not provider or provider == me:
                return  # ran it myself, or can't resolve the worker
            receipt, sig = make_receipt(
                group_id=gid, provider_pubkey=provider, kind="compute",
                ref_id=ref_id, amount=secs,
            )
            await store_and_apply(receipt, sig)
            # Broadcast to the group so every member's view converges.
            from nexus.runtime.group_inbox import publish_usage_receipt
            async with get_session() as session:
                await publish_usage_receipt(session, gid, receipt, sig)
            await _push_receipt(await _provider_addr(gid, provider), receipt, sig)
            return

        # 1:1 peer task — no roster to resolve from, so the worker
        # tells us its group pubkey and *proves* ownership by signing the
        # attribution. Reject an unproven/forged pubkey so a node can't credit
        # work to a key it doesn't hold.
        from nexus.security.usage_receipt import verify_worker_proof
        provider = (worker_pubkey or "").strip()
        if not provider or provider == me:
            return
        if not verify_worker_proof(ref_id, provider, elapsed_secs, worker_proof):
            _log.debug("worker proof failed for peer task %s", ref_id)
            return
        receipt, sig = make_receipt(
            group_id="", provider_pubkey=provider, kind="compute",
            ref_id=ref_id, amount=secs,
        )
        await store_and_apply(receipt, sig)
        await _push_receipt(worker, receipt, sig)
    except Exception:
        _log.debug("issue_compute_receipt failed", exc_info=True)


async def _provider_addr(group_id, provider_pubkey) -> str:
    from sqlalchemy import select
    async with get_session() as session:
        return (await session.execute(
            select(GroupMember.peer_address).where(
                (GroupMember.group_id == group_id)
                & (GroupMember.pubkey == provider_pubkey)
                & (GroupMember.peer_address != "")
            ).limit(1)
        )).scalar_one_or_none() or ""


async def _push_receipt(addr, receipt, sig) -> None:
    if not addr:
        return
    from nexus.networking.peer_http import peer_http_post
    try:
        await peer_http_post(addr, "/peer/usage_receipt", {"receipt": receipt, "sig": sig})
    except Exception:
        _log.debug("usage receipt push failed", exc_info=True)


__all__ = [
    "make_receipt",
    "store_and_apply",
    "issue_compute_receipt",
    "global_usage_summary",
]
