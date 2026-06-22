"""FastAPI routers: /local, /peer, WebSocket, diagnostics.

See ``README.md`` for the contract. Call :func:`register_routers(app)`
from the app factory to mount every router.
"""

from fastapi import FastAPI

from nexus.api import (
    diagnostics,
    events,
    group_peer,
    groups,
    local,
    pair_invites,
    peer,
    relay_admin,
    websocket,
)


def register_routers(app: FastAPI) -> None:
    """Mount every router on *app*.

    The order matters only for overlapping paths; each router here uses
    its own prefix so the order is effectively free.
    """
    app.include_router(diagnostics.router)
    app.include_router(events.router)
    app.include_router(groups.router)
    app.include_router(groups.invitations_router)
    app.include_router(group_peer.router)
    app.include_router(local.router)
    app.include_router(pair_invites.router)
    app.include_router(peer.router)
    app.include_router(relay_admin.router)
    app.include_router(websocket.router)


__all__ = ["register_routers"]
