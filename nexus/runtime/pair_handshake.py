"""Issuer-side pending pair-invite request tracking.

When a stranger redeems an ``nxg://pair#...`` link, the relay verifies
the signed invite and forwards a ``pair_invite_probe`` frame to the
issuer's main WS. This module holds the in-process pending list and
the API the user's accept/reject buttons hit.

State is in-memory only — pending requests don't survive a restart
(the relay times out the probe WS after ~60 s, so a restart that
takes longer than that just drops them; the requester sees a
"no response" reject and can re-redeem if their invite_id is still
the only one consumed for that token).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger("nexus.runtime.pair_handshake")


@dataclass
class IncomingPairRequest:
    """A pair request forwarded by the relay, awaiting user decision."""

    transient_id: str
    invite_id: str
    bob_pubkey: str
    bob_relay_urls: list[str]
    bob_display_name: str
    received_at: float = field(default_factory=time.time)


_pending: dict[str, IncomingPairRequest] = {}
_lock = asyncio.Lock()


async def add(req: IncomingPairRequest) -> None:
    async with _lock:
        _pending[req.transient_id] = req


async def get(transient_id: str) -> Optional[IncomingPairRequest]:
    async with _lock:
        return _pending.get(transient_id)


async def pop(transient_id: str) -> Optional[IncomingPairRequest]:
    async with _lock:
        return _pending.pop(transient_id, None)


async def list_pending() -> list[IncomingPairRequest]:
    """Newest first."""
    async with _lock:
        items = list(_pending.values())
    items.sort(key=lambda r: r.received_at, reverse=True)
    return items


async def prune_stale(max_age_sec: float = 120.0) -> None:
    """Drop requests older than *max_age_sec*. The relay-side probe WS
    has a ~60 s reply timeout; we keep entries a bit longer so a slow
    user click still resolves, but never indefinitely."""
    cutoff = time.time() - max_age_sec
    async with _lock:
        for tid in [t for t, r in _pending.items() if r.received_at < cutoff]:
            _pending.pop(tid, None)


__all__ = [
    "IncomingPairRequest",
    "add",
    "get",
    "pop",
    "list_pending",
    "prune_stale",
]
