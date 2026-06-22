"""Single-process cache for the ``/local/network`` response.

Extracted from node_modified.py (lines 8200-8201 + the check at
the head of ``local_get_network_graph`` on line 8206).

The UI polls ``/local/network`` frequently; assembling the full network
dict can be expensive. the original implementation keeps a one-second-TTL cache plus a
monotonic revision counter so the client can short-circuit with
``?since=<rev>`` when nothing changed.

The cache is small, mutable, and only touched from inside the
``/local/network`` handler, so we keep it in a module-level dict rather
than growing another field on :data:`nexus.core.STATE`.
"""

from __future__ import annotations

from typing import Any

NETWORK_CACHE_TTL = 1.0

_cache: dict[str, Any] = {"data": None, "ts": 0.0, "revision": 0}


def get_cache() -> dict[str, Any]:
    """Return the live cache dict. Callers read/write in place."""
    return _cache


def reset_for_testing() -> None:
    """Tests only: reset cache to its pristine shape."""
    _cache["data"] = None
    _cache["ts"] = 0.0
    _cache["revision"] = 0


__all__ = ["NETWORK_CACHE_TTL", "get_cache", "reset_for_testing"]
