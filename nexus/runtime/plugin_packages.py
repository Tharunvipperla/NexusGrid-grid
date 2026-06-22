"""D1 — plugin/recipe packages: share & install drop-in modules.

A *package* is one portable JSON file bundling one or more plugin modules
(relay / pump / runner / db-provider) with a small manifest. You export the
plugins you've built into a package, share that file however you like (chat,
repo, a peer), and the receiver imports it in one click — installing each
module into the right plugin folder.

Decentralized by design: there is no central registry or server. A package is
just a file. Installation reuses the A8 editor's safe CRUD
(:mod:`nexus.runtime.plugin_files`): every module's Python is syntax-checked and
written to disk, but **never imported or executed** — running stays each
subsystem's own explicit, sandboxed action.

A node also keeps a local *library* of saved packages under
``BASE_DIR/nexus_packages/*.json`` so a package you build can be kept,
re-downloaded, and re-installed later.
"""

from __future__ import annotations

import json
import logging
import re

from nexus.runtime import plugin_files
from nexus.utils.time import iso_now

_log = logging.getLogger("nexus.runtime.plugin_packages")

PACKAGE_FORMAT = "nexusgrid-plugin-package"
PACKAGE_VERSION = 1

_PKG_NAME_RE = re.compile(r"[A-Za-z0-9 ._-]{1,60}")
_MAX_MODULES = 50


def _packages_dir():
    from nexus.core.paths import BASE_DIR

    d = BASE_DIR / "nexus_packages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_pkg_filename(name: str) -> str:
    """Sanitize a package name into a ``*.json`` filename (no traversal).

    Idempotent on an already-sanitized ``*.json`` name so a saved filename
    round-trips exactly through ``read_package`` / ``delete_package``."""
    name = name or ""
    if name.endswith(".json"):
        name = name[:-5]
    stem = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name).strip("_.")
    return (stem[:60] or "package") + ".json"


# --- build / validate --------------------------------------------------------


def build_package(items: list[dict], name: str = "", description: str = "") -> dict:
    """Assemble a package from ``items`` (``[{kind, name}, ...]``). Reads each
    module's current source; raises ``ValueError`` if any is unknown/missing."""
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one module to export")
    if len(items) > _MAX_MODULES:
        raise ValueError(f"too many modules (max {_MAX_MODULES})")
    modules: list[dict] = []
    for it in items:
        kind = str((it or {}).get("kind") or "")
        mod_name = str((it or {}).get("name") or "")
        if kind not in plugin_files.KINDS:
            raise ValueError(f"unknown plugin kind '{kind}'")
        rec = plugin_files.read_module(kind, mod_name)  # validates name
        if not rec:
            raise ValueError(f"no such module {kind}/{mod_name}")
        modules.append({"kind": kind, "name": rec["name"], "source": rec["source"]})

    from nexus.core import get_node_identity

    return {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "name": str(name or "").strip()[:60],
        "description": str(description or "").strip()[:300],
        "created_at": iso_now(),
        "node": get_node_identity(),
        "modules": modules,
    }


def validate_package(data: dict) -> dict:
    """Validate + normalize a package dict. Returns the normalized package or
    raises ``ValueError`` with a human-readable reason."""
    if not isinstance(data, dict):
        raise ValueError("package must be a JSON object")
    if data.get("format") != PACKAGE_FORMAT:
        raise ValueError("not a NexusGrid plugin package")
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid package version")
    if version > PACKAGE_VERSION:
        raise ValueError(
            f"package format v{version} is newer than this node supports "
            f"(v{PACKAGE_VERSION}); update NexusGrid first"
        )
    raw = data.get("modules")
    if not isinstance(raw, list) or not raw:
        raise ValueError("package has no modules")
    if len(raw) > _MAX_MODULES:
        raise ValueError(f"package has too many modules (max {_MAX_MODULES})")

    modules: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            raise ValueError("malformed module entry")
        kind = str(m.get("kind") or "")
        name = str(m.get("name") or "")
        source = m.get("source")
        if kind not in plugin_files.KINDS:
            raise ValueError(f"unknown plugin kind '{kind}'")
        try:
            name = plugin_files._safe_name(name)
        except ValueError as exc:
            raise ValueError(f"{kind}: {exc}")
        v = plugin_files.validate_source(source if isinstance(source, str) else "")
        if not v["ok"]:
            raise ValueError(f"{kind}/{name}: {v.get('error')}")
        modules.append({"kind": kind, "name": name, "source": source})

    return {
        "format": PACKAGE_FORMAT,
        "version": version,
        "name": str(data.get("name") or "").strip()[:60],
        "description": str(data.get("description") or "").strip()[:300],
        "created_at": str(data.get("created_at") or ""),
        "node": str(data.get("node") or ""),
        "modules": modules,
    }


def install_package(data: dict, overwrite: bool = False) -> dict:
    """Validate then install every module. Existing modules are skipped unless
    ``overwrite``. Never executes any module. Returns per-module results."""
    pkg = validate_package(data)
    results: list[dict] = []
    installed = skipped = errors = 0
    for m in pkg["modules"]:
        kind, name = m["kind"], m["name"]
        exists = bool(plugin_files.read_module(kind, name))
        if exists and not overwrite:
            results.append({"kind": kind, "name": name, "status": "skipped"})
            skipped += 1
            continue
        try:
            res = plugin_files.write_module(kind, name, m["source"])
            results.append({"kind": kind, "name": name, "status": "installed",
                            "fingerprint": res.get("fingerprint", "")})
            installed += 1
        except ValueError as exc:
            results.append({"kind": kind, "name": name, "status": "error",
                            "error": str(exc)})
            errors += 1
    return {"installed": installed, "skipped": skipped, "errors": errors,
            "results": results}


# --- local saved library -----------------------------------------------------


def save_package(data: dict) -> dict:
    """Validate a package and store it in the local library. Returns the
    filename it was saved under."""
    pkg = validate_package(data)
    fname = _safe_pkg_filename(pkg.get("name") or pkg["modules"][0]["name"])
    path = _packages_dir() / fname
    path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
    _log.info("Saved plugin package '%s' (%d modules)", fname, len(pkg["modules"]))
    return {"filename": fname}


def _package_summary(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"filename": path.name, "invalid": True, "size": 0, "modules": []}
    mods = data.get("modules") if isinstance(data, dict) else []
    return {
        "filename": path.name,
        "name": str((data or {}).get("name") or ""),
        "description": str((data or {}).get("description") or ""),
        "created_at": str((data or {}).get("created_at") or ""),
        "node": str((data or {}).get("node") or ""),
        "size": path.stat().st_size,
        "modules": [{"kind": m.get("kind"), "name": m.get("name")}
                    for m in (mods or []) if isinstance(m, dict)],
    }


def list_packages() -> list[dict]:
    """Summaries of every saved package in the local library."""
    d = _packages_dir()
    return [_package_summary(p) for p in sorted(d.glob("*.json"))]


def read_package(filename: str) -> dict:
    """Return a saved package's full contents (for download / install). Raises
    ``ValueError`` on a bad name or missing file."""
    safe = _safe_pkg_filename(filename)
    if safe != filename:
        raise ValueError("invalid package filename")
    path = _packages_dir() / safe
    if not path.is_file():
        raise ValueError("no such package")
    return json.loads(path.read_text(encoding="utf-8"))


def delete_package(filename: str) -> dict:
    safe = _safe_pkg_filename(filename)
    if safe != filename:
        raise ValueError("invalid package filename")
    path = _packages_dir() / safe
    if not path.is_file():
        raise ValueError("no such package")
    path.unlink()
    return {"ok": True}


__all__ = [
    "PACKAGE_FORMAT", "PACKAGE_VERSION",
    "build_package", "validate_package", "install_package",
    "save_package", "list_packages", "read_package", "delete_package",
]
