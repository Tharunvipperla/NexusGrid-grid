"""Group-scoped compute: build the eligible-worker pool.

The scheduler keys ``STATE.active_workers`` by the peer's reachable address
(``peer.ip``), while group membership is keyed by node UUID
(``GroupMember.node_id``). This module bridges the two: given a set of
group_ids, it returns ``{group_id: set(eligible worker keys)}`` for members
holding ``task:run``, including both the resolved ip and the raw node UUID so
the membership check matches however ``active_workers`` was keyed (LAN peer ip
vs relay-path node_id fallback).
"""

from __future__ import annotations

from sqlalchemy import select

from nexus.core.identity import resolve_uuid_to_ip
from nexus.security.group_permissions import PERM_TASK_RUN, effective_permissions
from nexus.storage import get_session
from nexus.storage.models import GroupComputeStat, GroupMember


async def build_group_worker_pool(
    group_ids: set[str],
) -> dict[str, set[str]]:
    """Return ``{group_id: set(worker keys)}`` for ``task:run`` members."""
    pool: dict[str, set[str]] = {}
    if not group_ids:
        return pool
    async with get_session() as session:
        for gid in group_ids:
            members = (
                await session.execute(
                    select(GroupMember).where(GroupMember.group_id == gid)
                )
            ).scalars().all()
            keys: set[str] = set()
            for m in members:
                perms = await effective_permissions(session, gid, m.pubkey)
                if PERM_TASK_RUN not in perms:
                    continue
                node_id = (m.node_id or "").strip()
                if not node_id:
                    continue
                keys.add(node_id)
                ip = resolve_uuid_to_ip(node_id)
                if ip:
                    keys.add(ip)
            pool[gid] = keys
    return pool


async def group_member_uuids_with_task_run(
    group_ids: set[str],
) -> set[str]:
    """Union of node UUIDs of members holding ``task:run`` across *group_ids*.

    Used to scope foreign-storage deposits — capacities are keyed by
    ``peer_uuid`` (= ``GroupMember.node_id``), so no ip translation needed.
    """
    allowed: set[str] = set()
    if not group_ids:
        return allowed
    async with get_session() as session:
        for gid in group_ids:
            members = (
                await session.execute(
                    select(GroupMember).where(GroupMember.group_id == gid)
                )
            ).scalars().all()
            for m in members:
                node_id = (m.node_id or "").strip()
                if not node_id:
                    continue
                perms = await effective_permissions(session, gid, m.pubkey)
                if PERM_TASK_RUN in perms:
                    allowed.add(node_id)
    return allowed


async def record_compute_stat(
    group_id: str, *, contributed: int = 0, consumed: int = 0,
    compute_secs_contributed: int = 0, compute_secs_consumed: int = 0,
    storage_bytes_hosted: int = 0, storage_bytes_used: int = 0,
) -> None:
    """+ 48: record THIS node's pool usage for *group_id*.

    Two sinks: the live ``GroupComputeStat`` totals (task counts only — that's
    what the ``compute.stats`` beacon shares group-wide) and the 
    time-series sampler (all factors, for the Diagnostics history + export).
    """
    if not group_id:
        return
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.utils.time import iso_now

    if contributed or consumed:
        me = get_local_group_pubkey()
        async with get_session() as session:
            row = await session.get(GroupComputeStat, (group_id, me))
            if row is None:
                row = GroupComputeStat(
                    group_id=group_id, member_pubkey=me,
                    tasks_contributed=0, tasks_consumed=0,
                )
                session.add(row)
            row.tasks_contributed = int(row.tasks_contributed or 0) + int(contributed)
            row.tasks_consumed = int(row.tasks_consumed or 0) + int(consumed)
            row.updated_at = iso_now()
            await session.commit()

    from nexus.runtime.group_compute_telemetry import record
    await record(
        group_id,
        tasks_contributed=contributed, tasks_consumed=consumed,
        compute_secs_contributed=compute_secs_contributed,
        compute_secs_consumed=compute_secs_consumed,
        storage_bytes_hosted=storage_bytes_hosted,
        storage_bytes_used=storage_bytes_used,
    )


__all__ = [
    "build_group_worker_pool",
    "group_member_uuids_with_task_run",
    "record_compute_stat",
]
