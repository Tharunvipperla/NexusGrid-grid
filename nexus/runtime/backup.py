"""E5 — node backup (export).

Two kinds, both produced by :func:`build_backup`:

* **Normal** (``full=False``) — the small one. ``nexus.db`` (consistent online
  snapshot) + the ``.nexus_*`` identity/secret files + ``manifest.json``. This
  carries everything that lives in the database or in settings: tasks, peers,
  groups, the secrets vault, foreign-storage deposit *records*, relay/scheduler
  config, allowed images, templates, etc. It does NOT carry on-disk file data.

* **Full** (``full=True``) — a complete snapshot of the node. Everything in the
  normal backup PLUS the on-disk data the DB only *references*: the plugin
  module dirs (``nexus_relays``/``nexus_runners``/``nexus_pumps``/
  ``nexus_dbproviders``), saved result artifacts (``completed_tasks``), and the
  bytes of deposits hosted for peers (``nexus_cache_<port>``). Regenerable junk
  (build caches, old-port DBs, restore leftovers) is intentionally left out.

Both share one ``kind`` and one restore path: :func:`apply_pending_restore`
reads whatever the zip contains, so a single upload auto-handles either type —
a normal backup simply has no data files to extract.

The zip contains private keys and the at-rest encryption secret, so it is a
full clone of the node's identity — the UI labels it accordingly and it's only
reachable over the local-token-authenticated API.

Restore is staged on upload and applied on the next node start (before the DB
opens) so a running node is never overwritten from under itself.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path


def _db_path() -> Path:
    from nexus.core import get_node_port
    from nexus.core.paths import BASE_DIR
    return BASE_DIR / f"nexus_mod_{int(get_node_port())}.db"


# Dirs included in a *full* backup beyond the DB + identity files: hand-written
# plugin code, saved artifacts, and the bytes of deposits hosted for peers.
# Regenerable caches and leftovers are deliberately excluded.
def _full_data_dirs(base: Path, port: int) -> list[Path]:
    names = [
        "nexus_relays", "nexus_runners", "nexus_pumps", "nexus_dbproviders",
        "completed_tasks", f"nexus_cache_{int(port)}",
    ]
    return [base / n for n in names]


def _snapshot_db(src: Path, dst: Path) -> None:
    """Consistent online snapshot of a live SQLite DB (handles WAL)."""
    con = sqlite3.connect(str(src))
    try:
        dest = sqlite3.connect(str(dst))
        try:
            with dest:
                con.backup(dest)
        finally:
            dest.close()
    finally:
        con.close()


def build_backup(dest_zip: Path, full: bool = False) -> dict:
    """Write a backup zip to *dest_zip*; return a summary dict.

    *full* adds the on-disk node data (plugins, artifacts, hosted deposit bytes)
    on top of the normal DB + identity bundle — see the module docstring."""
    from datetime import datetime, timezone

    from nexus.core import LOCAL_SETTINGS, get_node_port
    from nexus.core.paths import BASE_DIR
    from nexus.storage.models import SCHEMA_VERSION

    db = _db_path()
    members: list[str] = []
    data_files = 0
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
            if db.is_file():
                snap = Path(td) / "nexus.db"
                _snapshot_db(db, snap)
                z.write(snap, "nexus.db")
                members.append("nexus.db")
            for f in sorted(BASE_DIR.glob(".nexus_*")):
                if f.is_file():
                    z.write(f, f.name)
                    members.append(f.name)
            if full:
                port = int(get_node_port())
                for d in _full_data_dirs(BASE_DIR, port):
                    if not d.is_dir():
                        continue
                    for f in sorted(d.rglob("*")):
                        if f.is_file() and "__pycache__" not in f.parts:
                            z.write(f, f.relative_to(BASE_DIR).as_posix())
                            data_files += 1
            manifest = {
                "kind": "nexus-node-backup",
                "version": 1,
                "full": bool(full),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "node_uuid": LOCAL_SETTINGS.get("node_uuid", ""),
                "schema_version": SCHEMA_VERSION,
                "members": members,
                "data_files": data_files,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))

    return {
        "path": str(dest_zip),
        "bytes": dest_zip.stat().st_size,
        "full": bool(full),
        "members": members + ["manifest.json"],
        "data_files": data_files,
    }


# --- restore (apply a staged backup at startup) ------------------------------
#
# A restore is uploaded via the API and parked as ``restore_pending.zip``. It is
# applied on the next node start — *before* the DB is opened — so a running node
# is never overwritten under itself. The current DB is kept as
# ``…​.db.pre_restore`` so a bad restore is recoverable.

PENDING_NAME = "restore_pending.zip"


def pending_restore_path() -> Path:
    from nexus.core.paths import BASE_DIR
    return BASE_DIR / PENDING_NAME


def validate_backup_zip(path: Path) -> tuple[bool, str]:
    """Cheap sanity check that *path* is one of our backups."""
    try:
        with zipfile.ZipFile(path) as z:
            if "manifest.json" not in z.namelist():
                return False, "no_manifest"
            man = json.loads(z.read("manifest.json"))
            if man.get("kind") != "nexus-node-backup":
                return False, "wrong_kind"
    except Exception as exc:
        return False, f"bad_zip:{type(exc).__name__}"
    return True, ""


def backup_schema_version(path: Path) -> int | None:
    """The DB ``schema_version`` recorded in the backup's manifest, or ``None``."""
    try:
        with zipfile.ZipFile(path) as z:
            return int(json.loads(z.read("manifest.json")).get("schema_version", 0))
    except Exception:
        return None


def restore_too_new(path: Path) -> int | None:
    """If the backup is from a *newer* schema than this node (an unsupported
    downgrade), return the backup's schema version; otherwise ``None``.

    Forward restores (older/equal backup → newer node) are supported — startup
    runs ``create_all`` for new tables + idempotent ``ADD COLUMN`` migrations,
    and settings merge over current defaults. Backward restores are not: an
    older node can't understand a newer schema, so we refuse rather than corrupt.
    """
    from nexus.storage.models import SCHEMA_VERSION

    bver = backup_schema_version(path)
    if bver is not None and bver > SCHEMA_VERSION:
        return bver
    return None


def apply_pending_restore(port: int) -> dict | None:
    """If a staged restore exists, apply it. Returns a summary, or ``None`` if
    there was nothing to do.

    Extracts ``nexus.db`` and the top-level ``.nexus_*`` identity files, plus —
    for a *full* backup — any data files it carries (plugins/artifacts/hosted
    bytes), each placed under ``BASE_DIR`` with zip-slip path-traversal refused.
    A normal backup simply has no data files, so the same path handles both."""
    from nexus.core.paths import BASE_DIR

    pending = BASE_DIR / PENDING_NAME
    if not pending.is_file():
        return None

    ok, reason = validate_backup_zip(pending)
    if not ok:
        pending.rename(BASE_DIR / (PENDING_NAME + ".invalid"))  # don't loop on it
        return {"applied": False, "reason": reason}

    too_new = restore_too_new(pending)
    if too_new is not None:
        # Backup is from a newer version than this node — refuse the downgrade
        # rather than open a DB this code can't understand.
        pending.rename(BASE_DIR / (PENDING_NAME + ".invalid"))
        return {"applied": False, "reason": "newer_version", "backup_schema": too_new}

    base_resolved = BASE_DIR.resolve()
    db = BASE_DIR / f"nexus_mod_{int(port)}.db"
    members: list[str] = []
    data_files = 0
    with zipfile.ZipFile(pending) as z:
        names = set(z.namelist())
        # Swap the DB only if the backup actually carries one.
        if "nexus.db" in names:
            if db.is_file():
                keep = BASE_DIR / (db.name + ".pre_restore")
                if keep.exists():
                    keep.unlink()
                db.rename(keep)
            for ext in ("-wal", "-shm"):  # stale WAL must not apply to the new DB
                p = BASE_DIR / (db.name + ext)
                if p.exists():
                    p.unlink()
            db.write_bytes(z.read("nexus.db"))
            members.append(db.name)
        for name in names:
            if name in ("manifest.json", "nexus.db") or name.endswith("/"):
                continue
            if name.startswith(".nexus_") and "/" not in name and "\\" not in name:
                (BASE_DIR / name).write_bytes(z.read(name))
                members.append(name)
                continue
            # Full-backup data file. Place it under BASE_DIR, refusing any path
            # that would escape the dir (zip-slip).
            target = (BASE_DIR / name).resolve()
            try:
                target.relative_to(base_resolved)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
            data_files += 1

    pending.unlink()
    return {"applied": True, "members": members, "data_files": data_files}


__all__ = [
    "build_backup", "PENDING_NAME", "pending_restore_path",
    "validate_backup_zip", "apply_pending_restore",
]
