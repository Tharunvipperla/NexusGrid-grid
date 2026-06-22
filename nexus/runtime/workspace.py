"""Per-task workspace directory + P2P cache resolution.

Extracted from Phase-1/node_modified.py:

* ``resolve_p2p_cache`` — lines 2765-2811

The workspace itself is a plain temporary directory created by the caller
(worker-client, when a task arrives). This module owns the helpers around
it — including P2P cache resolution, which walks the trusted-peer list to
download a dataset over LAN before falling back to the master.

If the task manifest specifies ``cloud_uri``, ``resolve_p2p_cache`` returns
a local path to the cached bundle. When no peer has it yet, a mock placeholder
is written so the task-setup code can continue (Phase-1 behaviour).
"""

from __future__ import annotations

import hashlib
import logging
import os

import httpx
from sqlalchemy import select

from nexus.core import cache_dir
from nexus.core.identity import (
    get_node_identity,
    get_node_port,
    resolve_ip_to_uuid,
    resolve_uuid_to_ip,
)
from nexus.storage import Peer, get_session

_log = logging.getLogger("nexus.runtime.workspace")


async def resolve_p2p_cache(
    cloud_uri: str, master_ip: str, port: int | None = None
) -> str:
    """Return a local path to the cached bundle for *cloud_uri*.

    Resolution order:
    1. Already-downloaded file under :func:`nexus.core.cache_dir`.
    2. Trusted peer's ``/peer/cache_query?uri_hash=...`` endpoint.
    3. Fallback placeholder (Phase-1 mock).
    """
    if not cloud_uri:
        return ""
    # Resolve the listening port at call time. Hardcoding 8000 caused the
    # writer to land downloads in `cache_dir(8000)` while `/peer/cache_query`
    # reads from `cache_dir(get_node_port())` — mismatched directories on any
    # node not listening on 8000.
    if port is None:
        port = get_node_port()
    uri_hash = hashlib.sha256(cloud_uri.encode()).hexdigest()
    local_path = os.path.join(str(cache_dir(port)), f"{uri_hash}.zip")
    if os.path.exists(local_path):
        return local_path

    try:
        resolved_master_ip = resolve_uuid_to_ip(master_ip)
        peer_candidates = {
            str(master_ip or "").strip(),
            str(resolved_master_ip or "").strip(),
            str(resolve_ip_to_uuid(master_ip) or "").strip(),
        }
        peer_candidates.discard("")
        cache_headers: dict[str, str] = {}
        async with get_session() as db:
            peer = (
                await db.execute(
                    select(Peer).filter(
                        Peer.ip.in_(list(peer_candidates)),
                        Peer.status == "trusted",
                    )
                )
            ).scalar_one_or_none()
        if peer and peer.their_auth_token:
            # Call ``get_node_identity()`` at request time rather than using a
            # stale ``NODE_UUID`` binding: ``from … import NODE_UUID`` copies
            # the empty value at module load, so later reassignments inside
            # ``get_or_create_node_uuid`` never propagate here. Phase-1 sent
            # ``<ip>:<port>`` on this header — preserve that.
            cache_headers = {
                "X-Cluster-Key": str(peer.their_auth_token),
                "X-Node-Address": get_node_identity(),
            }
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"http://{resolved_master_ip}/peer/cache_query?uri_hash={uri_hash}",
                timeout=2.0,
                headers=cache_headers or None,
            )
            if res.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(res.content)
                return local_path
    except Exception:
        _log.debug("P2P cache resolution failed", exc_info=True)

    with open(local_path, "wb") as f:
        f.write(b"MOCK_CLOUD_DATA")
    return local_path


__all__ = ["resolve_p2p_cache"]
