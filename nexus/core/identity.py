"""Node identity: UUID, display name, and IP↔UUID resolution.

Extracted from Phase-1/node_modified.py:

* display-name generator — lines 791-805
* node UUID lifecycle — lines 808-819
* UUID/IP mappings — lines 822-925

Why "identity" is its own module
--------------------------------
The node UUID is the single stable reference to "this node" that survives
port changes, IP changes, and relay round-trips. Almost every subpackage
touches it — relay client, peer protocol, audit log, UI topology — so it
has to live in :mod:`nexus.core` where there is no cycle risk.

IP↔UUID resolution is owned here rather than in ``networking`` for the same
reason: ``telemetry`` needs to mask log IPs and ``tasks`` need to record the
master that dispatched them, neither of which should have to import the
network stack.
"""

from __future__ import annotations

import random
import uuid
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Display name generator
# ---------------------------------------------------------------------------

_RANDOM_NAME_ADJ: tuple[str, ...] = (
    "spoofy", "gimbo", "zesty", "floofy", "bloopy", "snappy", "zippy", "fizzy",
    "wobbly", "nifty", "perky", "chirpy", "jolly", "loopy", "sneaky", "rowdy",
    "fluffy", "quirky", "zappy", "dizzy", "mossy", "twinkly", "bumpy", "crispy",
)
_RANDOM_NAME_NOUN: tuple[str, ...] = (
    "otter", "gizmo", "panda", "goblin", "wombat", "koala", "raven", "turtle",
    "falcon", "badger", "ferret", "weasel", "bobcat", "ocelot", "lemur", "moth",
    "crab", "hedgehog", "walrus", "pigeon", "squid", "puffin", "beaver", "yak",
)


def generate_random_display_name() -> str:
    """Return a fresh human-friendly name like ``spoofyotter42``.

    Used as the default display name when a user hasn't picked one.
    """
    return (
        f"{random.choice(_RANDOM_NAME_ADJ)}"
        f"{random.choice(_RANDOM_NAME_NOUN)}"
        f"{random.randint(10, 99)}"
    )


# ---------------------------------------------------------------------------
# Node UUID
# ---------------------------------------------------------------------------

# Mutable module-level cache. Seeded by :func:`get_or_create_node_uuid` on
# first call and then reused for the lifetime of the process. External code
# that only needs the current value may read :data:`NODE_UUID` directly but
# **must not** write to it.
NODE_UUID: str = ""

# Set by :func:`set_node_port` at app startup (from CLI ``--port``). Used by
# :func:`get_node_identity` to build the ``ip:port`` string that every peer
# payload and audit entry records as the dispatching node's address.
_NODE_PORT: int = 0


def set_node_port(port: int) -> None:
    """Record the HTTP port this node is listening on.

    Called once from :func:`nexus.app.create_app` lifespan. Required before
    :func:`get_node_identity` returns a meaningful value.
    """
    global _NODE_PORT
    _NODE_PORT = int(port)


def get_node_identity() -> str:
    """Return ``<local_ip>:<port>`` — the Phase-1 node identity string.

    Ported from Phase-1/node_modified.py:684-685.
    """
    # Local import: ``nexus.utils`` imports ``nexus.core`` indirectly.
    from nexus.utils.net import get_local_ip

    return f"{get_local_ip()}:{_NODE_PORT}"


def get_node_port() -> int:
    """Return the HTTP port previously registered via :func:`set_node_port`."""
    return _NODE_PORT


def get_or_create_node_uuid(settings: dict | None = None) -> str:
    """Return the node's persistent UUID, assigning a fresh one if unset.

    Parameters
    ----------
    settings
        The live ``LOCAL_SETTINGS`` dict. When omitted (the common call
        form used by Phase-1 code) it is resolved lazily via
        :mod:`nexus.core.config` to avoid an import cycle at module load.
    """
    global NODE_UUID
    if NODE_UUID:
        return NODE_UUID
    if settings is None:
        # Lazy import keeps the top-level ``nexus.core`` namespace free of
        # a direct identity→config dependency at load time.
        from nexus.core.config import LOCAL_SETTINGS as _settings

        settings = _settings
    existing = str(settings.get("node_uuid", "") or "")
    if existing:
        NODE_UUID = existing
        return NODE_UUID
    NODE_UUID = f"nexus_{uuid.uuid4().hex[:16]}"
    settings["node_uuid"] = NODE_UUID
    return NODE_UUID


# ---------------------------------------------------------------------------
# IP ↔ UUID resolution
# ---------------------------------------------------------------------------

# Bidirectional mapping. Neither is authoritative alone — peers can change
# their LAN IP while keeping the same UUID, and a freshly discovered peer
# may have an IP but no UUID yet.
_UUID_TO_IP: dict[str, str] = {}
_IP_TO_UUID: dict[str, str] = {}

# Optional persistence hook. Set by :mod:`nexus.storage` during
# initialization so every successful mapping registration also updates the
# ``peers.resolved_ip`` column. Kept as a callable rather than a direct
# storage import so ``core`` doesn't depend on ``storage``.
_persist_resolved_ip_hook: Optional[Callable[[str, str], None]] = None


def set_persist_hook(hook: Optional[Callable[[str, str], None]]) -> None:
    """Install a callback invoked whenever a UUID→IP mapping is registered.

    The hook receives ``(peer_uuid, real_ip_port)``. It should not raise —
    registration succeeds regardless.
    """
    global _persist_resolved_ip_hook
    _persist_resolved_ip_hook = hook


def register_peer_uuid(peer_uuid: str, real_ip_port: str) -> None:
    """Record a UUID↔IP mapping for a discovered peer.

    The mapping is ignored if either value is empty or if ``real_ip_port``
    starts with a masked-IP placeholder (bullet character). That placeholder
    comes from :data:`nexus.utils.text.MASKED_IP_PLACEHOLDER` and must never
    be promoted back into a usable address.
    """
    if not (peer_uuid and real_ip_port):
        return
    if real_ip_port.startswith("\u2022"):
        return
    _UUID_TO_IP[peer_uuid] = real_ip_port
    _IP_TO_UUID[real_ip_port] = peer_uuid
    if _persist_resolved_ip_hook and str(peer_uuid).startswith("nexus_"):
        try:
            _persist_resolved_ip_hook(peer_uuid, real_ip_port)
        except Exception:
            pass


def resolve_ip_to_uuid(ip_port: str) -> str:
    """Return the UUID associated with ``ip_port``, or ``ip_port`` itself."""
    return _IP_TO_UUID.get(ip_port, ip_port)


def resolve_uuid_to_ip(peer_uuid: str) -> str:
    """Return the IP associated with ``peer_uuid``, or ``peer_uuid`` itself."""
    return _UUID_TO_IP.get(peer_uuid, peer_uuid)


def snapshot_mappings() -> dict[str, dict[str, str]]:
    """Return a plain copy of the current mappings. Useful for diagnostics."""
    return {
        "uuid_to_ip": dict(_UUID_TO_IP),
        "ip_to_uuid": dict(_IP_TO_UUID),
    }


def clear_mappings() -> None:
    """Reset both mappings. Intended for tests."""
    _UUID_TO_IP.clear()
    _IP_TO_UUID.clear()


def fmt_peer(identifier: str) -> str:
    """Return a human-friendly label for any peer identifier.

    Ported from Phase-1/node_modified.py (lines 846-867). Prefers
    ``display_name#<4hex>`` from live heartbeats or LAN beacons, falls
    back to ``ip:port``, and only shows the raw ``nexus_<uuid>`` when
    nothing else is known.
    """
    # Local import to avoid the cycle: ``state`` imports ``utils`` which
    # imports ``identity``. Pulling STATE lazily keeps identity loadable
    # before state is ready.
    from nexus.core.state import STATE
    from nexus.networking.discovery import lookup_discovered_peer

    aw = STATE.active_workers.get(identifier, {}) if identifier else {}
    dn = (aw.get("stats", {}) or {}).get("user_display_name", "")
    if dn:
        short = identifier[-4:] if len(identifier) >= 4 else identifier
        return f"{dn}#{short}"
    _duuid, _dentry = lookup_discovered_peer(identifier)
    if _dentry and len(_dentry) > 1 and _dentry[1]:
        short = identifier[-4:] if len(identifier) >= 4 else identifier
        return f"{_dentry[1]}#{short}"
    resolved = resolve_uuid_to_ip(identifier)
    if resolved != identifier:
        return resolved
    return identifier
