"""Cache root directories + content-addressable cache keys.

Extracted from node_modified.py:

* ``_venv_cache_root`` / ``_pip_wheel_cache_dir`` / ``_node_cache_root``
  — lines 1059-1088
* ``_venv_cache_key`` / ``_node_cache_key`` — lines 1091-1111
* ``_detect_uv`` — lines 1096-1102

All three caches live directly under :data:`nexus.core.BASE_DIR` (not the
per-port ``cache_dir``) because venvs and wheels are heavyweight and should
be shared across every node instance that happens to be running on the same
host. They are content-addressed (hash-keyed) so two nodes touching the
same entry concurrently is safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from nexus.core import BASE_DIR

_log = logging.getLogger("nexus.caches.paths")


def venv_cache_root() -> Path:
    """Persistent cache of pre-built venvs, keyed by requirements hash."""
    root = Path(BASE_DIR) / "nexus_venv_cache"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        _log.debug("Failed to create venv cache root", exc_info=True)
    return root


def pip_wheel_cache_dir() -> Path:
    """Persistent pip wheel cache shared across every venv on this host.

    Wheels are content-hashed by pip, so sharing carries no tampering risk
    even when the full-venv cache is disabled or a cache miss occurs.
    """
    root = Path(BASE_DIR) / "nexus_pip_cache"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        _log.debug("Failed to create pip cache dir", exc_info=True)
    return root


def node_cache_root() -> Path:
    """Persistent cache of pre-installed ``node_modules`` trees."""
    root = Path(BASE_DIR) / "nexus_node_cache"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception:
        _log.debug("Failed to create node cache root", exc_info=True)
    return root


def venv_cache_key(requirements_text: str) -> str:
    """Stable 32-char hash of ``requirements.txt`` contents.

    Lines are trimmed, comments dropped, and sorted so cosmetic edits don't
    invalidate the cache while a genuine dependency change still does.
    """
    normalized = "\n".join(
        sorted(
            line.strip()
            for line in requirements_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def node_cache_key(lock_or_manifest_text: str) -> str:
    """Stable 32-char hash for a ``package-lock.json`` or ``package.json``."""
    normalized = lock_or_manifest_text.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def detect_uv() -> str | None:
    """Return path to the ``uv`` tool if present on PATH, else ``None``.

    When available, ``uv`` replaces ``python -m venv`` + ``pip install`` with
    a hardlinked, content-addressed install that is ~10× faster and shares
    packages across venvs automatically.
    """
    return shutil.which("uv")


__all__ = [
    "venv_cache_root",
    "pip_wheel_cache_dir",
    "node_cache_root",
    "venv_cache_key",
    "node_cache_key",
    "detect_uv",
]
