"""Depositor-side session-key cache for foreign-storage deposits.

Derived AES keys live in this module's process-local dict between
`unlock` and `lock` (or until the idle TTL fires). The cache used to
be an ad-hoc ``getattr(STATE, "foreign_storage_pending_keys", {})``
shared between the deposit / retrieve / pump paths; needs a
typed surface so the preview endpoint and TTL GC pass can interact
with it without reaching into ``STATE`` themselves.

Key invariants:

* Stored keys are wrapped in :class:`bytearray` so we can scrub them in
  place when we drop or evict — handing a regular ``bytes`` object to
  the GC and hoping the heap clears is not enough.
* :func:`get` bumps ``last_used_at`` so an active preview keeps the key
  alive past the idle window.
* :func:`gc` returns the list of evicted ids so the caller can audit
  each one.

The cache lives on :class:`STATE` so existing call sites that still
read the legacy ``foreign_storage_pending_keys`` attribute (deposit
pump and retrieve handler) keep working through the transition.
"""

from __future__ import annotations

import time
from typing import Iterable

from nexus.core import STATE


_ATTR = "foreign_storage_pending_keys"
DEFAULT_IDLE_TTL_S = 30 * 60  # 30 minutes


def _bucket() -> dict:
    cache = getattr(STATE, _ATTR, None)
    if cache is None:
        cache = {}
        setattr(STATE, _ATTR, cache)
    return cache


def store(deposit_id: str, key: bytes, **extra) -> None:
    """Persist a derived key for ``deposit_id`` (overwrite if present).

    Any extra kwargs (``file_path``, ``save_to``, ...) are merged into
    the entry alongside the key — used by the deposit pump and the
    retrieve flow.
    """
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError("key must be bytes-like")
    now = time.monotonic()
    bucket = _bucket()
    existing = bucket.get(deposit_id)
    if existing:
        # Scrub the previous key bytes before replacing the entry.
        prev = existing.get("key")
        if isinstance(prev, bytearray):
            for i in range(len(prev)):
                prev[i] = 0
    entry = dict(existing or {})
    entry["key"] = bytearray(key)
    entry["unlocked_at"] = entry.get("unlocked_at", now)
    entry["last_used_at"] = now
    entry.update(extra)
    bucket[deposit_id] = entry


def get(deposit_id: str) -> bytes | None:
    """Return the unlocked key for ``deposit_id`` and bump ``last_used_at``.

    Returns ``None`` if the deposit has never been unlocked or has been
    locked / GCed since.
    """
    entry = _bucket().get(deposit_id)
    if not entry:
        return None
    key = entry.get("key")
    if not key:
        return None
    entry["last_used_at"] = time.monotonic()
    return bytes(key)


def get_entry(deposit_id: str) -> dict | None:
    """Return the full bucket entry — used by deposit pump + retrieve.

    Does NOT bump ``last_used_at``; intended for code paths that need
    the auxiliary ``file_path`` / ``save_to`` fields, where touching
    the timestamp would be misleading.
    """
    return _bucket().get(deposit_id)


def is_unlocked(deposit_id: str) -> bool:
    entry = _bucket().get(deposit_id)
    return bool(entry and entry.get("key"))


def drop(deposit_id: str) -> bool:
    """Remove the entry, scrubbing the key bytes first.

    Returns True if an entry was present.
    """
    entry = _bucket().pop(deposit_id, None)
    if not entry:
        return False
    key = entry.get("key")
    if isinstance(key, bytearray):
        for i in range(len(key)):
            key[i] = 0
    return True


def list_unlocked() -> list[dict]:
    """Snapshot of currently-unlocked deposits (no key material)."""
    out = []
    for deposit_id, entry in _bucket().items():
        if not entry.get("key"):
            continue
        out.append({
            "deposit_id": deposit_id,
            "unlocked_at": entry.get("unlocked_at", 0.0),
            "last_used_at": entry.get("last_used_at", 0.0),
        })
    return out


def gc(now: float | None = None, idle_ttl_s: int = DEFAULT_IDLE_TTL_S) -> list[str]:
    """Drop entries idle past ``idle_ttl_s``. Returns the evicted ids."""
    cutoff = (now if now is not None else time.monotonic()) - idle_ttl_s
    evicted: list[str] = []
    bucket = _bucket()
    for deposit_id in list(bucket.keys()):
        entry = bucket.get(deposit_id) or {}
        if not entry.get("key"):
            continue
        if entry.get("last_used_at", 0.0) < cutoff:
            drop(deposit_id)
            evicted.append(deposit_id)
    return evicted


def reset_for_testing(deposit_ids: Iterable[str] | None = None) -> None:
    """Test helper: scrub specific (or all) entries."""
    bucket = _bucket()
    targets = list(deposit_ids) if deposit_ids is not None else list(bucket.keys())
    for d in targets:
        drop(d)


__all__ = [
    "DEFAULT_IDLE_TTL_S",
    "store",
    "get",
    "get_entry",
    "is_unlocked",
    "drop",
    "list_unlocked",
    "gc",
    "reset_for_testing",
]
