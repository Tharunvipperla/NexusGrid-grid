"""Depositor-side per-chunk plaintext cache + in-flight fetches.

The HTTP preview endpoint pulls one or more encrypted chunks from the host
on demand, decrypts them client-side, and streams the byte slice the
browser asked for. This module provides:

* A small in-memory LRU plaintext cache (default 64 MB, configurable via
  ``LOCAL_SETTINGS["preview_chunk_cache_mb"]``). Sequential video reads
  hit cache; out-of-order seeks fall through to the host.
* An in-flight Future map so two preview requests for the same chunk
  share a single host round-trip.

The host-reply path (``_handle_retrieve_chunk``) calls
:func:`resolve_chunk` whenever it decrypts a chunk; if a preview Future
is waiting on that ``(deposit_id, chunk_idx)`` it gets the plaintext.

State is held in module-level dicts keyed by ``deposit_id`` so locking
or TTL-evicting a deposit can call :func:`drop_deposit` to wipe both the
cached plaintext and any pending Futures.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from nexus.security.deposit_crypto import decrypt_chunk

_DEFAULT_CACHE_MB = 64

_pending: dict[tuple[str, int], asyncio.Future[bytes]] = {}
_cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_cache_bytes = 0


def _cache_cap_bytes() -> int:
    try:
        from nexus.api.local import LOCAL_SETTINGS

        mb = int(LOCAL_SETTINGS.get("preview_chunk_cache_mb") or _DEFAULT_CACHE_MB)
    except Exception:
        mb = _DEFAULT_CACHE_MB
    return max(1, mb) * 1024 * 1024


def _trim_cache() -> None:
    global _cache_bytes
    cap = _cache_cap_bytes()
    while _cache_bytes > cap and _cache:
        _, evicted = _cache.popitem(last=False)
        _cache_bytes -= len(evicted)


def _store_in_cache(deposit_id: str, chunk_idx: int, plaintext: bytes) -> None:
    global _cache_bytes
    key = (deposit_id, chunk_idx)
    if key in _cache:
        _cache_bytes -= len(_cache.pop(key))
    _cache[key] = plaintext
    _cache_bytes += len(plaintext)
    _trim_cache()


def get_cached(deposit_id: str, chunk_idx: int) -> bytes | None:
    """Return cached plaintext for one chunk, refreshing LRU order."""
    key = (deposit_id, chunk_idx)
    blob = _cache.get(key)
    if blob is None:
        return None
    _cache.move_to_end(key)
    return blob


async def fetch_plaintext(
    deposit_id: str,
    key_bytes: bytes,
    host_uuid: str,
    chunk_idx: int,
    *,
    request_open: Any | None = None,
    timeout_s: float = 30.0,
) -> bytes:
    """Return decrypted plaintext for one chunk, going to the host if needed.

    ``request_open`` is an awaitable callback that issues the
    ``storage_retrieve_open`` frame for ``chunk_idx``. Injected so the
    networking layer is not imported here (keeps the module testable).
    """
    cached = get_cached(deposit_id, chunk_idx)
    if cached is not None:
        return cached

    pending_key = (deposit_id, chunk_idx)
    fut = _pending.get(pending_key)
    if fut is None:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _pending[pending_key] = fut
        if request_open is not None:
            try:
                await request_open(host_uuid, chunk_idx)
            except Exception:
                _pending.pop(pending_key, None)
                if not fut.done():
                    fut.set_exception(RuntimeError("retrieve_open send failed"))
                raise

    try:
        blob = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout_s)
    finally:
        # Caller after the first one shares the same Future; only the last
        # awaiter sees it removed. Future.done() is true here on success.
        if pending_key in _pending and _pending[pending_key].done():
            _pending.pop(pending_key, None)

    plaintext = decrypt_chunk(key_bytes, blob, chunk_idx)
    _store_in_cache(deposit_id, chunk_idx, plaintext)
    return plaintext


def resolve_chunk(deposit_id: str, chunk_idx: int, ciphertext: bytes) -> bool:
    """Called by ``_handle_retrieve_chunk`` when a host reply arrives.

    Returns True if a preview Future was waiting (and got the bytes),
    False otherwise. The retrieve-to-disk path can ignore the return.
    """
    pending_key = (deposit_id, chunk_idx)
    fut = _pending.get(pending_key)
    if fut is None or fut.done():
        return False
    fut.set_result(ciphertext)
    return True


def drop_deposit(deposit_id: str) -> None:
    """Wipe cached plaintext + cancel pending fetches for one deposit."""
    global _cache_bytes
    dead_keys = [k for k in _cache if k[0] == deposit_id]
    for k in dead_keys:
        _cache_bytes -= len(_cache.pop(k))

    pending_keys = [k for k in _pending if k[0] == deposit_id]
    for k in pending_keys:
        fut = _pending.pop(k)
        if not fut.done():
            fut.cancel()


def reset_for_testing() -> None:
    global _cache_bytes
    _cache.clear()
    _cache_bytes = 0
    for fut in list(_pending.values()):
        if not fut.done():
            fut.cancel()
    _pending.clear()


def cache_stats() -> dict[str, int]:
    return {
        "entries": len(_cache),
        "bytes": _cache_bytes,
        "pending": len(_pending),
    }
