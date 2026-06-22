"""Query helpers used from outside the storage package.

Anything that would look like ``db.execute(select(Peer).filter(...))`` in a
caller belongs here instead, so subpackages never reach into
:mod:`nexus.storage.models` directly. Keeps query shapes consistent and makes
future schema changes a one-file search.

Extracted from node_modified.py:

* ``load_local_settings_from_db`` / ``save_local_settings_to_db``
  — lines 1837-1872
* peer lookups scattered throughout the file (e.g. 833-843, 6034-6038)
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from nexus.core.config import (
    DEFAULT_LOCAL_SETTINGS,
    LOCAL_SETTINGS,
    normalize_local_settings,
)
from nexus.core.identity import register_peer_uuid
from nexus.storage.database import get_session
from nexus.storage.models import LocalConfigRecord, Peer


# ---------------------------------------------------------------------------
# Local settings
# ---------------------------------------------------------------------------

_GLOBAL_CONFIG_ID = "global"


async def load_local_settings_from_db() -> None:
    """Load persisted settings into the live :data:`LOCAL_SETTINGS` dict.

    If the row does not exist, writes out :data:`DEFAULT_LOCAL_SETTINGS` after
    normalisation so the next startup is fast and the ``/local/settings``
    endpoint has something to return.
    """
    async with get_session() as db:
        record = (
            await db.execute(
                select(LocalConfigRecord).filter(
                    LocalConfigRecord.id == _GLOBAL_CONFIG_ID
                )
            )
        ).scalar_one_or_none()

        if not record:
            LOCAL_SETTINGS.update(normalize_local_settings(DEFAULT_LOCAL_SETTINGS))
            db.add(
                LocalConfigRecord(
                    id=_GLOBAL_CONFIG_ID,
                    config_json=json.dumps(LOCAL_SETTINGS),
                )
            )
            await db.commit()
            return

        try:
            parsed = json.loads(record.config_json or "{}")
        except Exception:
            parsed = {}
        LOCAL_SETTINGS.update(normalize_local_settings(parsed))


async def save_local_settings_to_db() -> None:
    """Persist the live :data:`LOCAL_SETTINGS` dict, re-normalising first."""
    LOCAL_SETTINGS.update(normalize_local_settings(LOCAL_SETTINGS))
    async with get_session() as db:
        record = (
            await db.execute(
                select(LocalConfigRecord).filter(
                    LocalConfigRecord.id == _GLOBAL_CONFIG_ID
                )
            )
        ).scalar_one_or_none()
        if not record:
            db.add(
                LocalConfigRecord(
                    id=_GLOBAL_CONFIG_ID,
                    config_json=json.dumps(LOCAL_SETTINGS),
                )
            )
        else:
            record.config_json = json.dumps(LOCAL_SETTINGS)
        await db.commit()


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------


async def get_peer_by_ip(ip: str) -> Optional[Peer]:
    """Return the :class:`Peer` row for *ip* (IP:port or UUID), or ``None``."""
    async with get_session() as db:
        return (
            await db.execute(select(Peer).filter(Peer.ip == ip))
        ).scalar_one_or_none()


async def list_peers() -> list[Peer]:
    """Return every peer row."""
    async with get_session() as db:
        return list((await db.execute(select(Peer))).scalars().all())


async def persist_resolved_ip(peer_uuid: str, real_ip_port: str) -> None:
    """Update ``peers.resolved_ip`` for the row keyed by *peer_uuid*.

    No-op if the peer is not in the database (e.g. freshly discovered and not
    yet accepted). Never raises — called from an identity hook that must not
    break registration on DB hiccups.
    """
    try:
        async with get_session() as db:
            peer = (
                await db.execute(select(Peer).filter(Peer.ip == peer_uuid))
            ).scalar_one_or_none()
            if peer and peer.resolved_ip != real_ip_port:
                peer.resolved_ip = real_ip_port
                await db.commit()
    except Exception:
        pass


async def seed_identity_mappings() -> None:
    """Re-populate ``core.identity`` with UUID↔IP pairs stored in the DB.

    Called on startup so peers keyed by UUID are resolvable before any beacon
    arrives. Mirrors the original implementation lines 6034-6038.
    """
    for peer in await list_peers():
        if str(peer.ip).startswith("nexus_") and peer.resolved_ip:
            register_peer_uuid(peer.ip, peer.resolved_ip)
