"""Filesystem helpers that don't belong in a more specific utils module.

Extracted from node_modified.py:

* ``_dir_size_bytes`` — lines 7663-7674 (used by ``/local/venv_cache_info``).
"""

from __future__ import annotations

import os


def dir_size_bytes(path: str) -> int:
    """Return the summed size of every regular file under *path*.

    Best-effort: permission errors on individual files are skipped; any
    higher-level failure returns ``0`` rather than raising, matching the
    the original implementation cache-admin handler that expects a numeric even on a broken
    cache directory.
    """
    total = 0
    try:
        for dp, _dn, fns in os.walk(path):
            for fn in fns:
                try:
                    total += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
    except Exception:
        pass
    return total


__all__ = ["dir_size_bytes"]
