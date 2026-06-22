"""Member liveness presence beacons.

Every node periodically broadcasts a ``presence.beacon`` frame into each
group it belongs to. Receivers bump the sender's ``GroupMember.last_seen_at``
(see :func:`nexus.runtime.group_inbox.apply_presence_beacon`), so the
Members pane can render an online dot for recently-seen members and
"offline N days" (capped at 30) for the rest.

Beacons are ephemeral: they are excluded from the Wave-37 frame log so a
catch-up peer never replays a stale "I was online days ago" ping. A paused
group is skipped automatically by ``publish_frame``.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from nexus.security.group_keys import get_local_group_pubkey
from nexus.storage import get_session
from nexus.storage.models import GroupMember

_log = logging.getLogger("nexus.runtime.group_presence")

# Beacon cadence + the window the UI treats as "online". The window is a
# little over two intervals so a single dropped beacon doesn't flap the dot.
PRESENCE_BEACON_INTERVAL_S = 60
PRESENCE_ONLINE_WINDOW_S = 150


async def beacon_my_groups() -> int:
    """Publish one presence beacon into every group this node belongs to.

    Returns the number of groups beaconed.
    """
    from nexus.runtime.group_inbox import publish_presence_beacon

    me = get_local_group_pubkey()
    async with get_session() as session:
        group_ids = (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.pubkey == me)
            )
        ).scalars().all()
        for gid in group_ids:
            try:
                await publish_presence_beacon(session, gid)
            except Exception:
                _log.debug("presence beacon failed for %s", gid[:8], exc_info=True)
        await session.commit()
    return len(group_ids)


async def presence_beacon_loop(
    poll_seconds: float = float(PRESENCE_BEACON_INTERVAL_S),
) -> None:
    """Forever: beacon presence into every group every ``poll_seconds``."""
    while True:
        try:
            await beacon_my_groups()
        except Exception:
            _log.debug("presence_beacon_loop tick failed", exc_info=True)
        await asyncio.sleep(poll_seconds)


__all__ = [
    "PRESENCE_BEACON_INTERVAL_S",
    "PRESENCE_ONLINE_WINDOW_S",
    "beacon_my_groups",
    "presence_beacon_loop",
]
