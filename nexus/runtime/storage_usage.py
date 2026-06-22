"""Storage-usage breakdown for the Diagnostics screen.

Walks the node's on-disk footprint under ``BASE_DIR`` and groups it into
categories the user understands, each flagged *deletable* or not. Live data
(the database, identity keys) and peers' hosted deposits are never bulk-deleted
from here; caches, saved artifacts, backup leftovers and stale per-port DBs are.

``scan`` and ``clear`` take optional ``base``/``port`` so they're testable
against a temp dir without a running node.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from nexus.core import get_node_port
from nexus.core.paths import BASE_DIR

# Build-cache directories (rebuilt on demand) and the restore-staging artifacts.
_CACHE_DIRS = ("nexus_venv_cache", "nexus_node_cache", "nexus_pip_cache")
_PENDING_NAME = "restore_pending.zip"

# Only these may be wiped via clear(); everything else is read-only here.
DELETABLE = {"artifacts", "caches", "backups", "stale_db"}


def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            total = 0
            for root, _dirs, files in os.walk(p):
                for f in files:
                    try:
                        total += (Path(root) / f).stat().st_size
                    except OSError:
                        pass
            return total
    except OSError:
        pass
    return 0


def _glob_size(base: Path, pattern: str) -> int:
    return sum(_size(p) for p in base.glob(pattern))


def _db_prefix(port: int) -> str:
    return f"nexus_mod_{port}.db"


def _stale_db_files(base: Path, port: int) -> list[Path]:
    keep = _db_prefix(port)
    return [p for p in base.glob("nexus_mod_*.db*") if not p.name.startswith(keep)]


def _backup_files(base: Path) -> list[Path]:
    out = list(base.glob("*.pre_restore")) + list(base.glob("*.invalid"))
    pending = base / _PENDING_NAME
    if pending.exists():
        out.append(pending)
    return out


def scan(base: Path | None = None, port: int | None = None) -> dict:
    """Return ``{categories:[{key,label,bytes,deletable,note}], total_bytes}``."""
    base = Path(base) if base else BASE_DIR
    port = get_node_port() if port is None else int(port)
    db_prefix = _db_prefix(port)

    cats = [
        {"key": "database", "label": "Database (tasks, peers, audit, settings)",
         "bytes": sum(_size(base / f"{db_prefix}{ext}") for ext in ("", "-wal", "-shm")),
         "deletable": False, "note": "live node data — use Backup to move it"},
        {"key": "identity", "label": "Identity & secrets",
         "bytes": _glob_size(base, ".nexus_*"),
         "deletable": False, "note": "keys — deleting these breaks this node"},
        {"key": "artifacts", "label": "Result artifacts",
         "bytes": _size(base / "completed_tasks"),
         "deletable": True, "note": "saved task outputs & logs"},
        {"key": "hosted", "label": "Hosted storage (for peers)",
         "bytes": _size(base / f"nexus_cache_{port}"),
         "deletable": False, "note": "peers' deposits — manage per-deposit in Foreign Storage"},
        {"key": "caches", "label": "Build caches (venv / node / pip)",
         "bytes": sum(_size(base / d) for d in _CACHE_DIRS),
         "deletable": True, "note": "rebuilt automatically when needed"},
        {"key": "backups", "label": "Backup & restore leftovers",
         "bytes": sum(_size(p) for p in _backup_files(base)),
         "deletable": True, "note": "old restore-staging files"},
        {"key": "stale_db", "label": "Old databases (other ports)",
         "bytes": sum(_size(p) for p in _stale_db_files(base, port)),
         "deletable": True, "note": "left over from running on a different port"},
    ]
    return {"categories": cats, "total_bytes": sum(c["bytes"] for c in cats)}


def _category_targets(key: str, base: Path, port: int) -> list[Path]:
    """The top-level files/dirs a deletable category owns."""
    if key == "artifacts":
        return [base / "completed_tasks"]
    if key == "caches":
        return [base / d for d in _CACHE_DIRS]
    if key == "backups":
        return _backup_files(base)
    if key == "stale_db":
        return _stale_db_files(base, port)
    return []


def _rm(path: Path) -> int:
    """Delete a file or dir, returning the bytes freed."""
    if not path.exists():
        return 0
    freed = _size(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            return 0
    return freed


def clear(key: str, base: Path | None = None, port: int | None = None) -> dict:
    """Delete a deletable category's files. Returns ``{key, removed_bytes}``."""
    if key not in DELETABLE:
        raise ValueError(f"'{key}' is not a deletable storage category")
    base = Path(base) if base else BASE_DIR
    port = get_node_port() if port is None else int(port)
    removed = sum(_rm(t) for t in _category_targets(key, base, port))
    return {"key": key, "removed_bytes": removed}


def list_files(key: str, base: Path | None = None, port: int | None = None) -> list[dict]:
    """The individual files in a deletable category, as ``{path, bytes}`` with
    *path* relative to ``BASE_DIR`` (newest/biggest first)."""
    if key not in DELETABLE:
        raise ValueError(f"'{key}' is not a deletable storage category")
    base = Path(base) if base else BASE_DIR
    port = get_node_port() if port is None else int(port)
    out: list[dict] = []
    for t in _category_targets(key, base, port):
        if t.is_file():
            out.append({"path": t.name, "bytes": _size(t)})
        elif t.is_dir():
            for root, _dirs, files in os.walk(t):
                for f in files:
                    p = Path(root) / f
                    try:
                        rel = p.relative_to(base).as_posix()
                    except ValueError:
                        continue
                    out.append({"path": rel, "bytes": _size(p)})
    out.sort(key=lambda x: -x["bytes"])
    return out


def delete_file(key: str, rel_path: str, base: Path | None = None, port: int | None = None) -> dict:
    """Delete one file from a deletable category. The path is validated to live
    *inside* that category (no traversal, no touching other categories)."""
    if key not in DELETABLE:
        raise ValueError(f"'{key}' is not a deletable storage category")
    base = Path(base) if base else BASE_DIR
    port = get_node_port() if port is None else int(port)
    base_r = base.resolve()
    target = (base / rel_path).resolve()
    if base_r != target and base_r not in target.parents:
        raise ValueError("path escapes the node directory")
    allowed = False
    for a in _category_targets(key, base, port):
        ar = a.resolve()
        if target == ar or ar in target.parents:
            allowed = True
            break
    if not allowed:
        raise ValueError("path is not in this category")
    return {"removed_bytes": _rm(target)}


__all__ = ["scan", "clear", "list_files", "delete_file", "DELETABLE"]
