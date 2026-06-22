"""In-process local relay server.

A founder can run the generic Nexus relay (``nexus/relay/server.py``)
*inside* their own node process instead of deploying it separately. The
relay is served on a second port by a background uvicorn thread; the
node then binds its groups to ``ws://<node-host>:<port>``.

This is a convenience for founders on a LAN or a public-IP host. A relay
on a behind-NAT machine is only reachable on that LAN — see
``docs/guides/relay-deploy.md`` for the public-relay path that works everywhere.

One relay per node. uvicorn skips signal-handler installation when it is
not on the main thread, so a daemon thread is a safe host for the second
server.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import threading
from typing import Optional

_log = logging.getLogger("nexus.runtime.local_relay")

DEFAULT_RELAY_PORT = 9000

# Single running relay per node. (will relax this for relay:host
# volunteers who opt into hosting extra relays; W61 keeps one instance.)
_server = None  # uvicorn.Server
_thread: Optional[threading.Thread] = None
_port: int = 0
# Which relay implementation is loaded — "default" (the bundled
# nexus/relay/server.py) or the name of a host-trusted nexus_relays/<name>.py
# plugin. A group's relay-fingerprint governance gates whether it trusts the
# code a member is actually running.
_module: str = "default"


def _relays_plugin_dir():
    from nexus.core.paths import BASE_DIR
    return BASE_DIR / "nexus_relays"


def _plugin_path(name: str) -> str:
    """Absolute path to a nexus_relays/<name>.py plugin, or "" if absent."""
    p = _relays_plugin_dir() / f"{name}.py"
    return str(p) if p.is_file() else ""


def _bundled_relay_path() -> str:
    from nexus.runtime.relay_codeprint import _resolve_relay_module_path
    return _resolve_relay_module_path()


def available_relay_modules() -> list[dict]:
    """List the relay implementations this node can run: the bundled
    ``default`` plus every host-trusted ``nexus_relays/*.py`` plugin, each with
    its code fingerprint so the operator (and the group's governance) can tell
    them apart."""
    from nexus.runtime.relay_codeprint import (CURRENT_FINGERPRINT,
                                               fingerprint_for_path)
    out = [{"name": "default", "builtin": True, "fingerprint": CURRENT_FINGERPRINT}]
    d = _relays_plugin_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.py")):
            out.append({"name": f.stem, "builtin": False,
                        "fingerprint": fingerprint_for_path(str(f))})
    return out


def _load_relay_module(name: str):
    """Import the chosen relay module and return it. ``default`` is the bundled
    relay; any other name resolves to a nexus_relays/<name>.py plugin (which
    must expose an ASGI ``app`` and a settable ``GRID_KEY``)."""
    if not name or name == "default":
        from nexus.relay import server  # the bundled default relay
        return server
    path = _plugin_path(name)
    if not path:
        raise ValueError(f"unknown relay module '{name}'")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"nexus_relays.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "app"):
        raise ValueError(f"relay module '{name}' has no ASGI 'app'")
    return mod


def _fingerprint_for_module(name: str) -> str:
    from nexus.runtime.relay_codeprint import (CURRENT_FINGERPRINT,
                                               fingerprint_for_path)
    if not name or name == "default":
        return CURRENT_FINGERPRINT
    return fingerprint_for_path(_plugin_path(name))


def _active_fingerprint() -> str:
    return _fingerprint_for_module(_module)


def _port_of_url(url: str) -> int:
    """Best-effort extract the TCP port from a ws(s)://host:port[/...] URL."""
    try:
        tail = str(url).rsplit("://", 1)[-1]
        host_port = tail.split("/", 1)[0]
        return int(host_port.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return 0


def fingerprint_for_url(url: str) -> str:
    """Return the code fingerprint of the relay reachable at *url* IF it is one
    of THIS node's running relays (matched by port — primary or a 
    instance). Empty string when it can't be resolved locally (a remote or
    tunnel-fronted URL); callers treat "" as "can't validate, don't reject"."""
    port = _port_of_url(url)
    if not port:
        return ""
    if is_running() and port == _port:
        return _active_fingerprint()
    inst = _instances.get(port)
    if inst and inst["thread"].is_alive():
        return _fingerprint_for_module(inst["module"])
    # Also a sandboxed (out-of-process) relay running on that port.
    try:
        from nexus.runtime import relay_sandbox
        fp = relay_sandbox.fingerprint_for_port(port)
        if fp:
            return fp
    except Exception:
        pass
    return ""


# --- additional opt-in relay instances ----------------------------
# A relay:host volunteer can run extra relays alongside the primary (e.g. their
# own + a group's). Each must be a DISTINCT module so the relays don't share the
# module's in-memory state (presence table, connected nodes). port -> instance.
_instances: dict = {}  # port -> {"server", "thread", "module"}


def _module_running(name: str) -> bool:
    name = name or "default"
    if is_running() and (_module or "default") == name:
        return True
    return any(i["module"] == name and i["thread"].is_alive()
               for i in _instances.values())


def list_instances() -> list[dict]:
    """Extra relay instances running beyond the primary."""
    out = []
    for port, inst in list(_instances.items()):
        out.append({
            "port": port,
            "module": inst["module"],
            "fingerprint": _fingerprint_for_module(inst["module"]),
            "suggested_url": f"ws://{_local_host()}:{port}",
            "running": inst["thread"].is_alive(),
        })
    return out


def start_instance(port: int, grid_key: str, module: str = "default") -> dict:
    """Start an ADDITIONAL relay on *port* running *module*, alongside the
    primary. Each running relay must be a distinct module (shared module state
    otherwise). Raises ValueError (bad module / duplicate module) or OSError
    (port in use)."""
    port = int(port)
    if is_running() and port == _port:
        raise OSError(f"port {port} is the primary relay's port")
    existing = _instances.get(port)
    if existing and existing["thread"].is_alive():
        return {"port": port, "module": existing["module"], "running": True,
                "suggested_url": f"ws://{_local_host()}:{port}",
                "fingerprint": _fingerprint_for_module(existing["module"])}
    if _module_running(module):
        raise ValueError(
            f"relay module '{module or 'default'}' is already running — run a "
            f"distinct module per relay (they'd otherwise share in-memory state)"
        )

    relay_mod = _load_relay_module(module)  # raises ValueError on unknown plugin

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        raise OSError(f"port {port} is already in use") from exc
    finally:
        probe.close()

    import uvicorn

    relay_mod.GRID_KEY = (grid_key or "").strip() or "nexus-beta-key"
    config = uvicorn.Config(
        relay_mod.app, host="0.0.0.0", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run, name=f"nexus.local_relay.{port}", daemon=True
    )
    thread.start()
    _instances[port] = {"server": server, "thread": thread,
                        "module": module or "default"}
    _log.info("Extra relay instance started on port %s (module=%s)", port, module)
    return {"port": port, "module": module or "default", "running": True,
            "suggested_url": f"ws://{_local_host()}:{port}",
            "fingerprint": _fingerprint_for_module(module)}


def stop_instance(port: int) -> dict:
    port = int(port)
    inst = _instances.pop(port, None)
    if not inst:
        return {"ok": False, "error": "not_found"}
    inst["server"].should_exit = True
    inst["thread"].join(timeout=5.0)
    _log.info("Extra relay instance stopped on port %s", port)
    return {"ok": True}


# --- relay-code distribution (cookbook-style export / import) -------
# A group's relay code is just a module. Export it so it can be shared; import
# it as a host-trusted nexus_relays/<name>.py plugin so a member can obtain the
# group's relay build, then run it (start_instance) and bind it (W63 validates
# the fingerprint). Import only WRITES the file — running it is the operator's
# explicit, separate action.

_MAX_RELAY_SOURCE = 1024 * 1024  # 1 MB
_MODULE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def get_module_source(name: str) -> dict:
    """Return a relay module's source so it can be shared/inspected. ``default``
    returns the bundled relay; any other name a nexus_relays/<name>.py plugin."""
    path = _bundled_relay_path() if (not name or name == "default") \
        else _plugin_path(name)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return {}
    return {"name": name or "default", "source": source,
            "fingerprint": _fingerprint_for_module(name),
            "builtin": (not name or name == "default")}


def import_module_source(name: str, source: str) -> dict:
    """Save *source* as a host-trusted nexus_relays/<name>.py plugin. Returns the
    resulting fingerprint so the operator can confirm it matches the group's
    frozen build before running it. Does NOT execute anything."""
    name = (name or "").strip()
    if not name or name == "default" or not _MODULE_NAME_RE.fullmatch(name):
        raise ValueError("invalid module name (letters, digits, - and _ only; not 'default')")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("empty source")
    if len(source.encode("utf-8")) > _MAX_RELAY_SOURCE:
        raise ValueError("source too large")
    d = _relays_plugin_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.py"
    # Normalize to LF and write raw bytes (no platform newline translation) so
    # the on-disk file is byte-identical to the source transported over the wire
    # — the W67 copy chain (publish → apply fingerprint gate → import → W63 bind)
    # depends on fingerprint_for_path(file) == fingerprint_for_bytes(source).
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalized.encode("utf-8"))
    from nexus.runtime.relay_codeprint import fingerprint_for_path
    _log.info("Imported relay module plugin '%s'", name)
    return {"name": name, "fingerprint": fingerprint_for_path(str(path)),
            "path": str(path)}


def relay_module_name_for_group(group_id: str) -> str:
    """The conventional plugin name a group's copied relay code is
    imported under — ``grp_<sanitized group id>``, clamped to the
    ``_MODULE_NAME_RE`` charset/length so ``import_module_source`` accepts it."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(group_id or ""))
    name = f"grp_{safe}"[:40]
    return name


def module_source_for_fingerprint(fingerprint: str) -> dict:
    """Return ``get_module_source`` for the local relay module whose
    code fingerprint equals *fingerprint*, or ``{}`` if none matches. Lets a
    relay host serve exactly the build the group froze (not whatever else it
    happens to have on disk)."""
    fp = (fingerprint or "").strip()
    if not fp:
        return {}
    for mod in available_relay_modules():
        if mod.get("fingerprint") == fp:
            return get_module_source(mod["name"])
    return {}


def delete_module(name: str) -> dict:
    name = (name or "").strip()
    if not name or name == "default":
        return {"ok": False, "error": "cannot delete the bundled default relay"}
    if _module_running(name):
        return {"ok": False, "error": "module is running — stop it first"}
    path = _plugin_path(name)
    if not path:
        return {"ok": False, "error": "not_found"}
    try:
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _local_host() -> str:
    """Best-effort reachable host for this node."""
    try:
        from nexus.utils.net import get_local_ip

        return get_local_ip() or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def _is_lan_only(host: str) -> bool:
    """True if *host* is a private / loopback / link-local address — i.e.
    a relay bound here is not reachable from other regions."""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        # A hostname rather than an IP — can't tell; assume public-intent.
        return False


def status() -> dict:
    """Return ``{running, port, suggested_url, lan_only, code_fingerprint}``."""
    running = is_running()
    port = _port if running else DEFAULT_RELAY_PORT
    host = _local_host()
    # /61: surface the fingerprint of the relay module actually loaded
    # (bundled or a plugin) so the UI can freeze the group's fingerprint to it.
    fp = ""
    try:
        fp = _active_fingerprint()
    except Exception:
        pass
    return {
        "running": running,
        "port": port,
        "suggested_url": f"ws://{host}:{port}" if running else "",
        "lan_only": _is_lan_only(host),
        "code_fingerprint": fp,
        "module": _module,
    }


def start(port: int, grid_key: str, module: str = "default") -> dict:
    """Start the in-process relay on *port*. Idempotent if already running.

    *module* selects the relay implementation: ``default`` (bundled) or a
    host-trusted ``nexus_relays/<module>.py`` plugin.

    *grid_key* is applied to the relay module directly (its functions read
    the module global at call time), so no env var or reload is needed.

    Raises ``OSError`` synchronously if ``port`` is already bound — without
    this, uvicorn's bind failure would happen inside the background thread
    and ``is_running()`` would briefly report ``True`` before the thread
    died, so the API caller saw "started" while nothing was actually
    serving (e.g. two nodes on the same machine fighting for port 9000).
    """
    global _server, _thread, _port, _module
    if is_running():
        return status()

    relay_mod = _load_relay_module(module)  # raises ValueError on unknown plugin

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", int(port)))
    except OSError as exc:
        raise OSError(
            f"port {port} is already in use — another process "
            f"(maybe a second Nexus node on this machine) is bound to it"
        ) from exc
    finally:
        probe.close()

    import uvicorn

    relay_mod.GRID_KEY = (grid_key or "").strip() or "nexus-beta-key"
    _module = module or "default"

    config = uvicorn.Config(
        relay_mod.app,
        host="0.0.0.0",
        port=int(port),
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    _port = int(port)
    _thread = threading.Thread(
        target=_server.run, name="nexus.local_relay", daemon=True
    )
    _thread.start()
    _log.info("Local relay started on port %s", port)
    return status()


def stop() -> dict:
    """Signal the relay server to exit and wait briefly for the thread."""
    global _server, _thread
    if _server is not None:
        _server.should_exit = True
    if _thread is not None:
        _thread.join(timeout=5.0)
    _server = None
    _thread = None
    _log.info("Local relay stopped")
    return status()


__all__ = ["DEFAULT_RELAY_PORT", "is_running", "status", "start", "stop",
           "available_relay_modules", "list_instances", "start_instance",
           "stop_instance", "fingerprint_for_url", "get_module_source",
           "import_module_source", "delete_module",
           "relay_module_name_for_group", "module_source_for_fingerprint"]
