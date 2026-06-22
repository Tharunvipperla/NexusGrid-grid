"""Admin-side grant heartbeat + TTL pruning.

Each grant has a short TTL (default 24 h). On every heartbeat tick
(default 6 h) the admin node re-signs every grant **it issued** that
is currently valid, bumping ``issued_at`` / ``expires_at`` / ``nonce``
and replacing the stored signature. Stop heartbeating = grants lapse
naturally after TTL.

Grants past their ``expires_at`` are deleted on every tick to keep
the table from accumulating dead rows.

The functions are pure DB operations on an :class:`AsyncSession` and
accept an injectable ``now_iso`` so tests can simulate clock
advancement without sleeping. The forever-loop in
:func:`group_heartbeat_loop` is what production wires into the
scheduler set.

Member-side renewal (the joiner pulls a fresh grant before its copy
lapses) is *not* in 15.6 — stops at the admin-side state
machine. The renewal endpoint will land alongside the catalog work in
, when consumers actually start needing long-lived grants.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.security import group_grant
from nexus.security.group_keys import (
    get_local_group_privkey,
    get_local_group_pubkey,
)
from nexus.storage import get_session
from nexus.storage.models import GroupGrant
from nexus.utils.time import iso_now


_log = logging.getLogger("nexus.runtime.group_heartbeat")


GROUP_GRANT_TTL_S = 86_400  # 24 h
GROUP_HEARTBEAT_INTERVAL_S = 21_600  # 6 h


def _expires_iso(ttl_s: int, base_iso: Optional[str] = None) -> str:
    """Return ``base_iso + ttl_s`` (or real now + ttl_s if base_iso is None).

    Plumbing the optional ``base_iso`` lets tests pin both the
    refresh-time *and* the resulting expiry without depending on wall
    clock.
    """
    if base_iso:
        try:
            base = datetime.fromisoformat(base_iso)
        except ValueError:
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    return (base + timedelta(seconds=int(ttl_s))).isoformat()


async def refresh_my_issued_grants(
    session: AsyncSession,
    *,
    now_iso: Optional[str] = None,
    ttl_s: int = GROUP_GRANT_TTL_S,
) -> int:
    """Re-sign every still-valid grant this node issued. Returns the count."""
    me_priv = get_local_group_privkey()
    me_pub = get_local_group_pubkey()
    now = now_iso or iso_now()

    rows = (
        await session.execute(
            select(GroupGrant).where(
                (GroupGrant.issued_by_pubkey == me_pub)
                & (GroupGrant.expires_at > now)
            )
        )
    ).scalars().all()

    new_expires = _expires_iso(ttl_s, base_iso=now_iso)
    refreshed = 0
    for row in rows:
        try:
            roles = json.loads(row.roles_json or "[]")
        except (ValueError, TypeError):
            roles = []
        if not isinstance(roles, list):
            roles = []
        nonce_hex = secrets.token_hex(16)
        new_blob = group_grant.sign_grant(
            group_id=row.group_id,
            member_pubkey=row.member_pubkey,
            roles=tuple(str(r) for r in roles),
            admin_privkey=me_priv,
            issued_at=now,
            expires_at=new_expires,
            nonce=nonce_hex,
        )
        row.issued_at = now
        row.expires_at = new_expires
        row.nonce = nonce_hex
        row.signature = new_blob
        refreshed += 1

    await session.flush()
    return refreshed


async def purge_expired_grants(
    session: AsyncSession,
    *,
    now_iso: Optional[str] = None,
) -> int:
    """Delete grants whose ``expires_at`` is in the past. Returns the count."""
    now = now_iso or iso_now()
    rows = (
        await session.execute(
            select(GroupGrant).where(GroupGrant.expires_at <= now)
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


async def group_heartbeat_loop(
    poll_seconds: float = float(GROUP_HEARTBEAT_INTERVAL_S),
) -> None:
    """Forever: every ``poll_seconds`` refresh + purge."""
    while True:
        try:
            async with get_session() as session:
                refreshed = await refresh_my_issued_grants(session)
                purged = await purge_expired_grants(session)
                if refreshed or purged:
                    await session.commit()
                else:
                    await session.rollback()
                if refreshed or purged:
                    _log.debug(
                        "group_heartbeat: refreshed=%d purged=%d",
                        refreshed,
                        purged,
                    )
        except Exception:
            _log.debug("group_heartbeat tick failed", exc_info=True)
        await asyncio.sleep(poll_seconds)


__all__ = [
    "GROUP_GRANT_TTL_S",
    "GROUP_HEARTBEAT_INTERVAL_S",
    "refresh_my_issued_grants",
    "purge_expired_grants",
    "group_heartbeat_loop",
]
