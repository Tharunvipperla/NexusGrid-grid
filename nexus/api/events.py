"""Server-Sent Events stream for the UI.

The browser opens one ``EventSource('/local/events/stream')`` on boot
and re-fetches the affected views whenever it receives an event.
Replaces the previous "click to refresh" pattern.

Auth follows the rest of ``/local/*`` — the dependency raises 401 on
a bad token. ``EventSource`` cannot set custom headers, so the UI
must use ``?local_token=`` (already supported by
:func:`verify_local_auth`).

Heartbeats: every 15 s we emit a comment line (``: ping``) so
intermediaries don't time the connection out and the browser keeps
``readyState === OPEN`` even when no real events are flowing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from nexus.runtime import event_bus
from nexus.security import verify_local_auth

_log = logging.getLogger("nexus.api.events")

router = APIRouter(
    prefix="/local/events",
    tags=["Events"],
    dependencies=[Depends(verify_local_auth)],
)

HEARTBEAT_INTERVAL_S = 15.0


@router.get("/stream", summary="Server-Sent Events stream of UI state changes")
async def events_stream() -> StreamingResponse:
    async def gen():
        # SSE: each event is `data: <json>\n\n`; comment lines start
        # with ':' and are ignored by EventSource but keep the socket
        # warm through proxies.
        yield ": connected\n\n"
        q = await event_bus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        q.get(), timeout=HEARTBEAT_INTERVAL_S,
                    )
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # No-event tick — keep the connection warm. Cancelling
                    # q.get() is safe; the subscriber stays registered.
                    yield ": ping\n\n"
        finally:
            await event_bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


__all__ = ["router"]
