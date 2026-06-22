"""Task manifest (``task.json`` inside the zip payload) with a small cache.

Extracted from Phase-1/node_modified.py (lines 2528-2552).

The manifest tells the scheduler + runtime which Docker image to use, the
RAM/CPU limits, whether the task needs network access, etc. Parsing a zip
for every scheduling pass is wasteful — the cache is keyed by ``task.id``
and bounded at 500 entries (FIFO eviction).

This is a leaf module inside :mod:`nexus.scheduler` because fitness and
selection both read it. It does not mutate anything.
"""

from __future__ import annotations

import io
import json
import zipfile

_manifest_cache: dict[str, dict] = {}
_MAX_CACHED = 500


def read_task_manifest(
    task_payload: bytes | None = None,
    cache_key: str = "",
) -> dict:
    """Return parsed ``task.json`` from *task_payload*, with caching.

    When *cache_key* is set and already cached, *task_payload* is not
    touched — this makes the helper safe to call against
    ``TaskRecord.payload`` which is a *deferred* SQLAlchemy column.
    """
    if cache_key and cache_key in _manifest_cache:
        return _manifest_cache[cache_key]
    if task_payload is None:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(task_payload)) as zf:
            if "task.json" not in zf.namelist():
                result: dict = {}
            else:
                result = json.loads(zf.read("task.json").decode("utf-8"))
    except Exception:
        result = {}
    if cache_key:
        _manifest_cache[cache_key] = result
        if len(_manifest_cache) > _MAX_CACHED:
            oldest = next(iter(_manifest_cache))
            del _manifest_cache[oldest]
    return result


def clear_manifest_cache() -> None:
    """Tests only: drop every cached manifest."""
    _manifest_cache.clear()


__all__ = ["read_task_manifest", "clear_manifest_cache"]
