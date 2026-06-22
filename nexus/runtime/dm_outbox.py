"""Offline DM outbox.

A 1:1 DM is best-effort at send time (one POST to the peer). If the peer is
offline that single attempt fails and, without this, the message would never
arrive. Group chat already survives this via the Wave-37 frame-log catch-up;
this gives DMs the same guarantee.

Undelivered outbound rows (``direction="out"``, ``delivered=0``) are retried
on a slow cadence. Each attempt re-resolves the peer's current address (so a
peer that came back on a new IP is reached) and re-seals via ``_deliver_dm``,
which flips ``delivered`` to 1 on success.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from nexus.storage import get_session
from nexus.storage.models import DirectMessage

_log = logging.getLogger("nexus.runtime.dm_outbox")

DM_OUTBOX_INTERVAL_S = 45
_BATCH = 50


async def flush_outbox() -> int:
    """Retry delivery of every undelivered outbound DM. Returns the number
    that delivered on this pass."""
    from nexus.api.local import _deliver_dm, _resolve_dm_target

    async with get_session() as db:
        rows = (
            await db.execute(
                select(DirectMessage)
                .where(
                    (DirectMessage.direction == "out")
                    & (DirectMessage.delivered == 0)
                    & (DirectMessage.deleted == 0)
                )
                .order_by(DirectMessage.sent_at)
                .limit(_BATCH)
            )
        ).scalars().all()
        # Snapshot the fields we need; _deliver_dm opens its own session.
        pending = [
            {
                "peer_uuid": r.peer_uuid,
                "msg_id": r.msg_id,
                "body": r.body or "",
                "sent_at": r.sent_at or "",
                "sender_name": r.sender_name or "",
                "reply": {
                    "reply_to": r.reply_to or "",
                    "reply_snippet": r.reply_snippet or "",
                    "reply_sender": r.reply_sender or "",
                },
                "attach": {
                    "attach_kind": r.attach_kind or "",
                    "attach_name": r.attach_name or "",
                    "attach_mime": r.attach_mime or "",
                    "attach_size": int(r.attach_size or 0),
                    "attach_data": r.attach_data or "",
                },
            }
            for r in rows
        ]

    delivered = 0
    for p in pending:
        target = await _resolve_dm_target(p["peer_uuid"])
        ok = await _deliver_dm(
            target, p["msg_id"], p["body"], p["sent_at"],
            p["sender_name"], p["reply"], p["attach"],
        )
        if ok:
            delivered += 1
    return delivered


async def dm_outbox_loop(poll_seconds: float = float(DM_OUTBOX_INTERVAL_S)) -> None:
    """Forever: flush the DM outbox every ``poll_seconds``."""
    while True:
        try:
            await flush_outbox()
        except Exception:
            _log.debug("dm_outbox_loop tick failed", exc_info=True)
        await asyncio.sleep(poll_seconds)


__all__ = ["DM_OUTBOX_INTERVAL_S", "flush_outbox", "dm_outbox_loop"]
