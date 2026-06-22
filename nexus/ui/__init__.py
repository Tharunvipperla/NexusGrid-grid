"""HTML serving, avatar endpoint, UI broadcaster.

See ``README.md`` for the contract. Call :func:`mount_ui(app)` from the
app factory to register every UI route and install the event-bus bridge
that feeds the WebSocket broadcaster.
"""

from fastapi import FastAPI

from nexus.ui import avatar, serve
from nexus.ui.broadcaster import (
    broadcast_ui_update,
    install_event_bridge,
    register_ws,
    unregister_ws,
)


def mount_ui(app: FastAPI) -> None:
    """Mount UI routes on *app* and subscribe the broadcaster to the event bus."""
    app.include_router(serve.router)
    app.include_router(avatar.router)
    install_event_bridge()


__all__ = [
    "mount_ui",
    "broadcast_ui_update",
    "register_ws",
    "unregister_ws",
]
