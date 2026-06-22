"""Relay self-healing.

Cloudflare quick-tunnel URLs are ephemeral: every time the tunnel
restarts it gets a fresh ``*.trycloudflare.com`` address. Without help a
group bound to the old URL would silently lose its relay.

This module closes that gap. It remembers the tunnel URL this node last
advertised (``relay_self_heal_url``); when the tunnel comes up with a
new one, it swaps the URL in every group binding that used the old one
and broadcasts a ``relay.update`` so the whole group converges — no
manual re-bind. The tunnel also auto-starts on boot when it was last
enabled.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import local_relay, relay_tunnel
from nexus.runtime.group_inbox import publish_relay_update
from nexus.security.group_keys import get_local_group_pubkey
from nexus.storage import get_session, save_local_settings_to_db
from nexus.storage.models import GroupRelayBinding
from nexus.utils.time import iso_now

_log = logging.getLogger("nexus.runtime.relay_selfheal")

# Small in-memory observability so the Relays subtab can show
# "Self-heal: N moves, last at <ts>" without a separate audit table.
_reconcile_count: int = 0
_last_reconcile_at: str = ""


def status() -> dict:
    """Snapshot of self-heal activity since this process started."""
    return {
        "tracked_url": str(LOCAL_SETTINGS.get("relay_self_heal_url", "") or ""),
        "reconcile_count": _reconcile_count,
        "last_reconcile_at": _last_reconcile_at,
    }


async def reconcile_relay_url(old_url: str, new_url: str) -> int:
    """Swap ``old_url`` -> ``new_url`` in every group binding that has it.

    Broadcasts a ``relay.update`` per change so members converge.
    Returns the number of groups updated.
    """
    old_url = (old_url or "").strip()
    new_url = (new_url or "").strip()
    if not old_url or not new_url or old_url == new_url:
        return 0
    me = get_local_group_pubkey()

    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupRelayBinding.group_id).where(
                    (GroupRelayBinding.relay_url == old_url)
                    & (GroupRelayBinding.status == "active")
                )
            )
        ).fetchall()
    group_ids = sorted({r[0] for r in rows})

    for gid in group_ids:
        try:
            async with get_session() as session:
                new_row = await session.get(
                    GroupRelayBinding, (gid, new_url)
                )
                if new_row is None:
                    session.add(
                        GroupRelayBinding(
                            group_id=gid,
                            relay_url=new_url,
                            operator_pubkey=me,
                            registered_at=iso_now(),
                            last_seen_at="",
                            status="active",
                        )
                    )
                else:
                    new_row.status = "active"
                old_row = await session.get(
                    GroupRelayBinding, (gid, old_url)
                )
                if old_row is not None:
                    old_row.status = "retired"
                await publish_relay_update(session, gid, new_url, "add", me)
                await publish_relay_update(
                    session, gid, old_url, "remove", me
                )
                await session.commit()
        except Exception:
            _log.warning("relay self-heal for %s failed", gid, exc_info=True)

    if group_ids:
        global _reconcile_count, _last_reconcile_at
        _reconcile_count += len(group_ids)
        _last_reconcile_at = iso_now()
        _log.info(
            "relay self-heal: %d group(s) moved to %s",
            len(group_ids), new_url,
        )
    return len(group_ids)


async def start_tunnel_and_reconcile() -> dict:
    """Start the local relay + tunnel, then self-heal bound groups.

    Backs the ``/local/relay/tunnel/start`` endpoint and boot autostart.
    The blocking work (cloudflared download + tunnel) runs off the loop.
    """
    if not local_relay.is_running():
        grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
        port = int(
            LOCAL_SETTINGS.get("local_relay_port")
            or local_relay.DEFAULT_RELAY_PORT
        )
        local_relay.start(port, grid_key)
        LOCAL_SETTINGS["local_relay_enabled"] = True
        LOCAL_SETTINGS["local_relay_port"] = port

    relay_port = int(local_relay.status()["port"])
    old_url = str(LOCAL_SETTINGS.get("relay_self_heal_url", "") or "")
    st = await asyncio.to_thread(relay_tunnel.start, relay_port)
    new_url = str(st.get("relay_url", "") or "")
    if new_url and new_url != old_url:
        await reconcile_relay_url(old_url, new_url)
    LOCAL_SETTINGS["relay_self_heal_url"] = new_url
    LOCAL_SETTINGS["relay_tunnel_enabled"] = True
    await save_local_settings_to_db()
    return st


async def maybe_autostart() -> None:
    """Boot hook — relaunch the tunnel if it was enabled last run."""
    if not LOCAL_SETTINGS.get("relay_tunnel_enabled"):
        return
    try:
        await start_tunnel_and_reconcile()
    except Exception:
        _log.warning("relay tunnel autostart failed", exc_info=True)


__all__ = [
    "reconcile_relay_url",
    "start_tunnel_and_reconcile",
    "maybe_autostart",
    "status",
]
