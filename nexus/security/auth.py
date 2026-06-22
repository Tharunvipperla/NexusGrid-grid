"""FastAPI auth dependencies for local management and peer endpoints.

Extracted from Phase-1/node_modified.py (lines 1616-1663).

Two layers of authentication:

* ``verify_local_auth`` — bearer token (``X-Local-Token`` or
  ``Authorization: Bearer``) matched against the node's
  :func:`nexus.security.tokens.get_local_api_token`. Additionally the client
  host must be loopback or RFC1918 unless the node was launched with
  ``NEXUS_ALLOW_REMOTE_UI=1``. This gates every ``/local/*`` route.

* ``verify_trusted_peer`` — per-peer ``X-Cluster-Key`` matched against the
  ``peers.my_auth_token`` column where ``status == 'trusted'``. Returns the
  canonical peer IP on success. This gates every ``/peer/*`` route.

``resolve_trusted_peer`` is a lower-level helper used by the WebSocket
handshake where a request object is not available — it returns a tuple
``(peer_ip, is_trusted)`` without raising.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException, Request
from sqlalchemy import select

from nexus.storage.database import get_session
from nexus.storage.models import Peer
from nexus.security.tokens import get_local_api_token
from nexus.utils.net import client_host, env_flag, is_private_or_loopback_host

_log = logging.getLogger("nexus.security.auth")


def _management_client_allowed(host: str) -> bool:
    """Return ``True`` if *host* may hit ``/local/*`` routes.

    Loopback and private IPs are always allowed. Public IPs are allowed only
    when ``NEXUS_ALLOW_REMOTE_UI`` is truthy in the environment.
    """
    if env_flag("NEXUS_ALLOW_REMOTE_UI", False):
        return True
    return is_private_or_loopback_host(host)


async def verify_local_auth(request: Request) -> None:
    """FastAPI dependency that authenticates a management request.

    Raises ``401`` on bad token, ``403`` on disallowed client host. Never
    returns a value — callers add it via ``Depends(verify_local_auth)`` and
    rely on the side effect.
    """
    auth = request.headers.get("Authorization", "")
    token = request.headers.get("X-Local-Token", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    # Fallback: ?local_token=... query param. Needed for browser tags like
    # <video>/<img>/<audio>/<iframe> which cannot set custom headers.
    if not token:
        token = request.query_params.get("local_token", "")
    if not hmac.compare_digest(token, get_local_api_token()):
        raise HTTPException(401, detail="Invalid local API token.")
    if not _management_client_allowed(client_host(request)):
        raise HTTPException(
            403,
            detail=(
                "Management access is restricted to local or private-network "
                "clients."
            ),
        )


async def verify_trusted_peer(request: Request) -> str:
    """FastAPI dependency that authenticates a peer-protocol request.

    Returns the peer's canonical IP on success, raises ``403`` otherwise.
    """
    req_token = request.headers.get("X-Cluster-Key")
    # Reject missing/empty token before the DB query: SQLAlchemy compiles
    # `col == None` to `IS NULL`, which would match peer rows seeded without
    # an explicit my_auth_token (e.g. `--peers` bootstrap) and let an
    # unauthenticated caller impersonate the seeded master.
    if not req_token:
        raise HTTPException(403, detail="Missing cluster key.")
    async with get_session() as db:
        peer = (
            await db.execute(
                select(Peer).filter(
                    Peer.my_auth_token == req_token, Peer.status == "trusted"
                )
            )
        ).scalar_one_or_none()
        if not peer:
            raise HTTPException(403, detail="Unauthorized token.")
    # Batch C: blocked peers can still present a valid token (they were
    # trusted once), but every peer-protocol request from them is denied.
    from nexus.core import LOCAL_SETTINGS
    blocked = set(LOCAL_SETTINGS.get("blocked_peer_uuids") or [])
    if peer.ip in blocked:
        raise HTTPException(403, detail="Peer is blocked.")
    return peer.ip


async def resolve_trusted_peer(
    auth_token: str | None,
    client_host_str: str,
    announced_address: str | None = None,
) -> tuple[str, bool]:
    """Low-level trust resolution used where ``Request`` is unavailable.

    Returns ``(peer_ip, is_trusted)``. Does not raise. Loopback with no
    token is accepted as trusted so an operator can poke the local node from
    curl without a token; every other path requires a valid token.
    """
    if client_host_str in ("127.0.0.1", "localhost", "::1") and not auth_token:
        return client_host_str, True
    if auth_token:
        async with get_session() as db:
            peer = (
                await db.execute(
                    select(Peer).filter(
                        Peer.my_auth_token == auth_token, Peer.status == "trusted"
                    )
                )
            ).scalar_one_or_none()
        if peer:
            return peer.ip, True
    return announced_address or client_host_str, False
