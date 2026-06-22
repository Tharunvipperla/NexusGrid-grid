"""Foreign-storage quota math.

Two callers:

* ``nexus.api.local`` exposes the numbers via the local UI.
* ``nexus.runtime.capacity`` advertises the bit ``foreign_storage`` to peers
  and must do so dynamically based on opt-out + actual disk room.

Lives under ``runtime`` so it can be imported by both without a cycle through
``api``. The helpers are pure, side-effect-free, and cheap enough to call on
every capability publish.
"""

from __future__ import annotations

import shutil

from nexus.core import LOCAL_SETTINGS


def used_gb() -> float:
    """Bytes the host currently holds for foreign deposits, in GB."""
    from nexus.core import cache_dir, get_node_port

    base = cache_dir(get_node_port()) / "foreign_storage"
    if not base.exists():
        return 0.0
    total = 0
    for entry in base.rglob("*.enc"):
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total / (1024 ** 3)


def disk_free_gb() -> float:
    """Free space on the partition that hosts the foreign-storage cache."""
    from nexus.core import cache_dir, get_node_port

    base = cache_dir(get_node_port())
    base.mkdir(parents=True, exist_ok=True)
    try:
        return shutil.disk_usage(str(base)).free / (1024 ** 3)
    except OSError:
        return 0.0


def is_accepting_offers() -> bool:
    """Per-node opt-out toggle for foreign-storage offers.

    A node is accepting only when BOTH:
    * the master "Accept Network Tasks (Node Online)" switch is on, AND
    * the per-feature ``foreign_storage_accept_offers`` toggle is on.

    Coupling the master toggle here matches the user's mental model
    ("offline means offline for everything"). Without this, flipping the
    Node Offline switch silences compute but the node keeps advertising
    as an Accepting foreign-storage host to peers.
    """
    if not bool(LOCAL_SETTINGS.get("node_online", True)):
        return False
    raw = LOCAL_SETTINGS.get("foreign_storage_accept_offers", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(raw)


def auto_opt_out_reason() -> str:
    """Returns a non-empty reason when the node should auto-opt-out.

    The user's rule: if the disk can't honour the pledged size, advertise
    nothing — better to be silent than promise space we can't deliver.
    """
    pledge = float(LOCAL_SETTINGS.get("storage_max_total_gb", 5) or 5)
    free = disk_free_gb()
    if free < pledge:
        return f"disk_free_below_pledge ({free:.2f} GB free, {pledge:.0f} GB pledged)"
    return ""


def is_effectively_accepting() -> bool:
    """Combined gate: manual toggle AND auto-opt-out check."""
    if not is_accepting_offers():
        return False
    return not auto_opt_out_reason()


def effective_free_gb() -> float:
    """Advertised free space.

    The user wants ``min(pledge_remaining, disk_remaining)`` so we never
    promise space we don't actually have. ``disk_remaining`` reserves a
    safety buffer so the OS doesn't grind to a halt at 0 free bytes.
    """
    pledge = float(LOCAL_SETTINGS.get("storage_max_total_gb", 100) or 100)
    pledge_remaining = max(0.0, pledge - used_gb())
    safety = float(LOCAL_SETTINGS.get("foreign_storage_disk_safety_gb", 1.0) or 0.0)
    disk_remaining = max(0.0, disk_free_gb() - safety)
    return min(pledge_remaining, disk_remaining)


__all__ = [
    "used_gb",
    "disk_free_gb",
    "is_accepting_offers",
    "auto_opt_out_reason",
    "is_effectively_accepting",
    "effective_free_gb",
]
