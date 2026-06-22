"""File-system paths, PyInstaller-safe resource lookup, and file permission helpers.

Extracted from Phase-1/node_modified.py (lines 100-123).

Design notes
------------
The node has two distinct "root" concepts:

* ``BASE_DIR`` — writable root for runtime artefacts (secrets, SQLite database,
  caches, avatars). This lives next to the executable or the source file.

* ``get_resource_dir()`` — read-only root for bundled resources (index.html,
  static assets). When frozen by PyInstaller this resolves to
  ``sys._MEIPASS`` — a temporary directory populated at launch. Never write
  into it.

``cache_dir(port)`` is a function rather than a module constant because the
cache is keyed by the node's listen port, which is only known after
``cli.parse_args()`` has run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_data_dir(name: str = "NexusGrid") -> Path:
    """Per-user, OS-appropriate application-data directory.

    * Windows → ``%LOCALAPPDATA%\\NexusGrid``
    * macOS   → ``~/Library/Application Support/NexusGrid``
    * Linux   → ``$XDG_DATA_HOME/NexusGrid`` (or ``~/.local/share/NexusGrid``)
    """
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        root = str(Path.home() / "Library" / "Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / name


def _resolve_base_dir() -> Path:
    """Pick the writable runtime root, in priority order:

    1. ``NEXUS_DATA_DIR`` (env, also set by the ``--data-dir`` CLI flag).
    2. Packaged build → the per-user app-data dir (so a downloaded ``.exe`` run
       from Downloads doesn't litter that folder — its state lives under
       ``%LOCALAPPDATA%\\NexusGrid`` etc.).
    3. Running from source → the repo's ``Phase-2`` dir (unchanged dev behavior).
    """
    override = os.environ.get("NEXUS_DATA_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    elif getattr(sys, "frozen", False):
        base = _app_data_dir()
    else:
        base = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent
        return base  # source tree already exists; don't mkdir the repo root
    base.mkdir(parents=True, exist_ok=True)
    return base


BASE_DIR: Path = _resolve_base_dir()


def get_resource_dir() -> Path:
    """Return the directory containing bundled read-only resources.

    Under PyInstaller: ``sys._MEIPASS`` (extracted bundle).
    Otherwise: :data:`BASE_DIR`.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return BASE_DIR


def cache_dir(port: int) -> Path:
    """Return (creating if needed) the per-port cache directory.

    The node multiplexes cache state by listen port so several nodes can run
    on one host without stomping on each other.
    """
    path = BASE_DIR / f"nexus_cache_{int(port)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def secure_file_permissions(path: str | os.PathLike) -> None:
    """Restrict *path* to owner-only access (0o600) on Unix.

    Windows is a no-op: per-user profile directories already restrict access
    by default, and ``icacls`` can race when two processes share a directory.
    Best effort — never raises.
    """
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
