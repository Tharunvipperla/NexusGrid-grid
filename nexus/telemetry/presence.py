"""Peer presence tracking and stale-peer sweeper.

Extracted from node_modified.py:

* presence dict ops — lines 541-607 (``mark_peer_online``, ``mark_peer_offline``,
  ``is_peer_offline``, ``_log_presence_event``, ``_write_presence_event``).
* stale-peer sweep — lines 5921-6001 (embedded inside ``zombie_sweeper``).

A peer's presence is a small dict stored in
:data:`nexus.core.STATE.peer_presence`::

    {"status": "online"|"offline"|"unknown",
     "last_seen": <epoch>,
     "source":    "ws"|"timeout"|"relay"|"udp"}

The WebSocket handler marks peers ``online`` on register and ``offline`` on
disconnect. The UDP discovery layer marks ``online`` when it sees a beacon.
The zombie sweeper periodically walks the table and downgrades peers whose
``last_seen`` is older than :data:`PEER_PRESENCE_TIMEOUT`.

Every transition also fires a fire-and-forget DB write of a ``PresenceEvent``
row so ``/local/presence_history`` can render the log later.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from nexus.core.constants import PEER_PRESENCE_TIMEOUT
from nexus.core.state import STATE
from nexus.storage.database import get_session
from nexus.storage.models import PresenceEvent
from nexus.utils.time import now_epoch

_log = logging.getLogger("nexus.telemetry.presence")


# ---------------------------------------------------------------------------
# In-memory mutation
# ---------------------------------------------------------------------------

def mark_peer_online(ip: str, source: str = "ws") -> None:
    """Record *ip* as online and persist a transition event if state changed."""
    if not ip:
        return
    prev = STATE.peer_presence.get(ip, {}).get("status")
    STATE.peer_presence[ip] = {
        "status": "online",
        "last_seen": time.time(),
        "source": source,
    }
    if prev != "online":
        log_presence_event(ip, "online", source)


def mark_peer_offline(ip: str, source: str = "ws") -> None:
    """Record *ip* as offline and persist a transition event if state changed."""
    if not ip:
        return
    prev = STATE.peer_presence.get(ip, {}).get("status")
    STATE.peer_presence[ip] = {
        "status": "offline",
        "last_seen": time.time(),
        "source": source,
    }
    if prev != "offline":
        log_presence_event(ip, "offline", source)


# Back-compat short names used throughout the original implementation (``presence.touch``, etc.).
touch = mark_peer_online
mark_offline = mark_peer_offline


def is_peer_offline(ip: str) -> bool:
    """Return ``True`` if we should skip dialing *ip* right now.

    Purely push-based: an offline marker is never demoted by a timer. It
    only clears when one of the three recovery channels delivers evidence
    of life — inbound heartbeat on ``/peer/ws``, a UDP discovery beacon,
    or a relay ``peer_list`` containing this peer. If none fire, the peer
    stays offline forever, which is correct: if it can't reach us via any
    channel, a probe from our side can't reach it either.
    """
    if not ip:
        return False
    entry = STATE.peer_presence.get(ip)
    if not entry:
        return False
    return entry.get("status") == "offline"


def snapshot() -> dict[str, dict]:
    """Return a deep copy of the presence table."""
    return {ip: dict(entry) for ip, entry in STATE.peer_presence.items()}


# ---------------------------------------------------------------------------
# Persistence (fire-and-forget)
# ---------------------------------------------------------------------------

def log_presence_event(peer_ip: str, status: str, source: str) -> None:
    """Schedule a ``PresenceEvent`` insert without blocking the caller.

    Silently skipped when no event loop is running (early startup). This
    differs from the original implementation only in that we check loop presence *before*
    creating the coroutine so Python doesn't warn about an un-awaited
    object — the observable behaviour is identical.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no event loop yet
    loop.create_task(write_presence_event(peer_ip, status, source))


async def write_presence_event(peer_ip: str, status: str, source: str) -> None:
    """Persist one presence transition. Silently swallows DB errors."""
    try:
        async with get_session() as db:
            db.add(
                PresenceEvent(
                    id=str(uuid.uuid4()),
                    peer_ip=peer_ip,
                    status=status,
                    source=source,
                    ts=str(time.time()),
                )
            )
            await db.commit()
    except Exception:
        _log.debug("write_presence_event failed for %s", peer_ip, exc_info=True)


# ---------------------------------------------------------------------------
# Stale-peer sweep
# ---------------------------------------------------------------------------

def sweep_once(timeout: float = PEER_PRESENCE_TIMEOUT) -> list[str]:
    """One pass of the sweeper. Returns the peers just demoted."""
    now = now_epoch()
    demoted: list[str] = []
    for ip, entry in list(STATE.peer_presence.items()):
        if entry.get("status") == "online" and (
            now - float(entry.get("last_seen", 0)) > timeout
        ):
            mark_peer_offline(ip, source="timeout")
            demoted.append(ip)
    return demoted


__all__ = [
    "mark_peer_online",
    "mark_peer_offline",
    "touch",
    "mark_offline",
    "is_peer_offline",
    "snapshot",
    "log_presence_event",
    "write_presence_event",
    "sweep_once",
]
