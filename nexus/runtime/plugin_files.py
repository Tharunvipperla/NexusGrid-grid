"""In-app editor backend for drop-in plugin modules (A8).

One small manager over the four host-trusted plugin folders so users can edit
relay / pump / runner / db-provider code *from the UI* instead of digging into
files on disk. Each is a ``BASE_DIR/<dir>/*.py`` module loaded by its own
subsystem (local_relay, service_tunnel, replica_runner, db_provider).

This module only does **safe file CRUD + a Python-syntax check** — it never
imports or executes the code. Running stays each subsystem's explicit,
sandboxed action (relays: W61/65; runners: W60). Source is normalized to LF and
written as raw bytes so fingerprints stay byte-stable across platforms (matches
``local_relay.import_module_source``).
"""

from __future__ import annotations

import logging
import os
import re

_log = logging.getLogger("nexus.runtime.plugin_files")

# kind -> {dir, label, fingerprint, doc}
KINDS: dict[str, dict] = {
    "relays": {
        "dir": "nexus_relays", "label": "Relay modules", "fingerprint": True,
        "doc": "Expose an ASGI `app` and a settable `GRID_KEY`. Run from the Relays tab.",
    },
    "pumps": {
        "dir": "nexus_pumps", "label": "Service pumps", "fingerprint": False,
        "doc": "Call `register_pump(name, factory)`; factory() returns transform(direction, chunk).",
    },
    "runners": {
        "dir": "nexus_runners", "label": "Sandbox runners", "fingerprint": False,
        "doc": "Call `register_runner(name, build, ...)` to add a sandbox backend.",
    },
    "dbproviders": {
        "dir": "nexus_dbproviders", "label": "DB providers", "fingerprint": False,
        "doc": "Expose create(admin_dsn,db,user,pw) + drop(admin_dsn,db,user) (+ optional KIND).",
    },
}

_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_MAX_SOURCE = 1024 * 1024  # 1 MB

# Reference implementations shipped inside the app (read-only). Listed so the
# operator can *see* what the node does by default, even though these aren't
# editable files. Only the relay default is a complete, copyable module; the
# rest are reference functions/classes embedded in core modules.
_BUILTINS: dict[str, list[str]] = {
    "relays": ["default"],
    "pumps": ["default"],
    "runners": ["docker", "podman", "raw"],
    "dbproviders": ["postgres"],
}


def _dir(kind: str):
    from nexus.core.paths import BASE_DIR
    if kind not in KINDS:
        raise ValueError(f"unknown plugin kind '{kind}'")
    return BASE_DIR / KINDS[kind]["dir"]


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name) or name == "default":
        raise ValueError("invalid module name (letters, digits, - and _ only; not 'default')")
    return name


def _path(kind: str, name: str):
    return _dir(kind) / f"{_safe_name(name)}.py"


def validate_source(source: str) -> dict:
    """Python-syntax check (compile only — never executes). Returns
    ``{ok}`` or ``{ok: False, error, line}``."""
    if not isinstance(source, str) or not source.strip():
        return {"ok": False, "error": "empty source"}
    if len(source.encode("utf-8")) > _MAX_SOURCE:
        return {"ok": False, "error": "source too large (1 MB max)"}
    try:
        compile(source, "<plugin>", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"{exc.msg}", "line": exc.lineno or 0}
    return {"ok": True}


def _fingerprint(kind: str, path) -> str:
    if not KINDS[kind]["fingerprint"]:
        return ""
    try:
        from nexus.runtime.relay_codeprint import fingerprint_for_path
        return fingerprint_for_path(str(path))
    except Exception:
        return ""


def list_modules(kind: str) -> list[dict]:
    """Editable plugin files in *kind*'s folder (name, size, fingerprint)."""
    d = _dir(kind)
    out: list[dict] = []
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            out.append({"name": f.stem, "size": size,
                        "fingerprint": _fingerprint(kind, f)})
    return out


def read_module(kind: str, name: str) -> dict:
    path = _path(kind, name)
    if not path.is_file():
        return {}
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {"kind": kind, "name": _safe_name(name), "source": source,
            "fingerprint": _fingerprint(kind, path)}


def write_module(kind: str, name: str, source: str) -> dict:
    """Create/overwrite a plugin file. Validates name, size, and Python syntax;
    normalizes to LF + writes raw bytes. Does NOT load/run it."""
    name = _safe_name(name)
    v = validate_source(source)
    if not v["ok"]:
        raise ValueError(v.get("error") or "invalid source")
    d = _dir(kind)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.py"
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalized.encode("utf-8"))
    _log.info("Saved %s plugin '%s'", kind, name)
    return {"kind": kind, "name": name, "fingerprint": _fingerprint(kind, path)}


def delete_module(kind: str, name: str) -> dict:
    name = _safe_name(name)
    # Relays carry a running/builtin guard in local_relay — reuse it.
    if kind == "relays":
        from nexus.runtime import local_relay
        return local_relay.delete_module(name)
    path = _path(kind, name)
    if not path.is_file():
        return {"ok": False, "error": "not_found"}
    try:
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def builtins(kind: str) -> list[dict]:
    """Names of the shipped reference implementations for *kind* (read-only)."""
    if kind not in KINDS:
        raise ValueError(f"unknown plugin kind '{kind}'")
    return [{"name": n, "builtin": True} for n in _BUILTINS.get(kind, [])]


def builtin_source(kind: str, name: str) -> dict:
    """Read-only source of a shipped reference implementation, so the operator
    can see what the app does by default. Relays return the full bundled relay
    file; the others return the relevant reference function/class via inspect."""
    if kind not in KINDS or name not in _BUILTINS.get(kind, []):
        return {}
    src = ""
    try:
        if kind == "relays":
            from nexus.runtime import local_relay
            src = local_relay.get_module_source("default").get("source", "")
        elif kind == "pumps":
            import inspect
            from nexus.runtime import service_tunnel
            src = inspect.getsource(service_tunnel._default_transform)
        elif kind == "runners":
            import inspect
            from nexus.runtime import replica_runner
            fn = replica_runner._raw_argv if name == "raw" else replica_runner._container_argv
            src = inspect.getsource(fn)
        elif kind == "dbproviders":
            import inspect
            from nexus.runtime import db_provider
            src = inspect.getsource(db_provider._PostgresAdapter)
    except Exception:
        src = ""
    if not src:
        return {}
    return {"kind": kind, "name": name, "source": src, "builtin": True, "readonly": True}


def overview() -> list[dict]:
    """All kinds with their module counts + metadata, for the editor's index."""
    out = []
    for kind, meta in KINDS.items():
        out.append({"kind": kind, "label": meta["label"], "doc": meta["doc"],
                    "fingerprint": meta["fingerprint"],
                    "builtins": builtins(kind),
                    "modules": list_modules(kind)})
    return out


__all__ = ["KINDS", "validate_source", "list_modules", "read_module",
           "write_module", "delete_module", "overview", "builtins",
           "builtin_source"]
