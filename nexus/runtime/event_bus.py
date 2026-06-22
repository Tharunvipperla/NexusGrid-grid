"""Local UI event bus.

Tiny in-process fan-out for state-change events the UI cares about.
Other code calls :func:`publish` after mutating shared state (a relay
binding added, a roster delta applied, a join decision flipped);
:func:`subscribe` hands out an async iterator that the SSE endpoint
in :mod:`nexus.api.events` streams to the browser. The browser then
re-fetches just the affected views instead of polling on a timer.

No persistence, no auth — this module is purely a memory-resident
notification channel. Auth happens at the SSE endpoint via the
existing local-token check.

A bounded per-subscriber queue caps blast radius: a slow consumer
can lose events but won't backpressure publishers.
"""

from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger("nexus.runtime.event_bus")

_SUBSCRIBER_QUEUE_SIZE = 64
_subscribers: set[asyncio.Queue[dict]] = set()
_lock = asyncio.Lock()


async def publish(event: dict) -> None:
    """Fan ``event`` out to every current subscriber, dropping if a
    subscriber's queue is full (slow consumer)."""
    async with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            _log.debug("event_bus subscriber queue full; dropped %r", event)


async def subscribe() -> asyncio.Queue[dict]:
    """Register a subscriber and return its queue.

    Caller pulls events via ``await q.get()``; pair every ``subscribe()``
    with :func:`unsubscribe` (use ``try/finally``). The queue approach
    is deliberate — wrapping this in an async generator and then
    calling ``asyncio.wait_for(__anext__())`` on it cancels the
    generator's inner ``await q.get()`` on timeout, which tears the
    subscriber down for good.
    """
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    async with _lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue[dict]) -> None:
    async with _lock:
        _subscribers.discard(q)


__all__ = ["publish", "subscribe", "unsubscribe"]
