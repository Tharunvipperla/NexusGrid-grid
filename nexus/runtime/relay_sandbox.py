"""Sandboxed (out-of-process) execution of a relay module.

/62 run a relay module IN-PROCESS on a uvicorn daemon thread. That is
fine for the host's own trusted ``default`` build, but a foreign/imported relay
 running in-process shares the node's whole Python process — its memory,
keys, DB session. This module runs a relay module as a SEPARATE, sandboxed OS
process instead, reusing the runner registry (docker / podman / raw +
``nexus_runners/*.py`` plugins) to pick the sandbox.

* **container runners** (docker / podman / custom): the relay source directory is
  mounted read-only into a host-allowlisted python image (one that already has
  ``uvicorn`` + ``fastapi``), and uvicorn serves it on a loopback-published port.
  Hardened like (cap-drop ALL, no-new-privileges, read-only rootfs,
  mem/cpu caps, loopback-only publish). This is the path that works from the
  frozen ``.exe`` (no host python needed).
* **raw runner** (no sandbox): spawn ``python -m uvicorn`` as a child process —
  a separate process/memory space from the node, but no container. Needs a python
  interpreter on the host; the explicit "I accept no sandbox" option.

Gates mirror explicit ``agreed`` consent, the runner must be available,
and container images must be on the host's allowlist. Loopback-bound by default;
front it with the cloudflared tunnel to make it reachable while isolated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import uuid

from nexus.core.config import LOCAL_SETTINGS
from nexus.runtime import child_job
from nexus.runtime import replica_runner as rr

_log = logging.getLogger("nexus.runtime.relay_sandbox")

_MAX_SANDBOXED = 8
_CONTAINER_PORT = 9000  # the port uvicorn binds INSIDE a container

# sandbox_id -> {module, runner, port, kind, sandboxed, fingerprint,
#                container_id|None, proc|None, url}
_sandboxed: dict[str, dict] = {}


def _relay_source(module: str):
    """Return ``(source_dir, import_name)`` for a relay module, or ``None`` if
    the module isn't present. ``default`` resolves to the bundled relay; any
    other name to a ``nexus_relays/<name>.py`` plugin."""
    module = (module or "default").strip()
    if module == "default":
        from nexus.runtime.relay_codeprint import _resolve_relay_module_path
        path = _resolve_relay_module_path()
        if not path:
            return None
        return os.path.dirname(path), "server"
    from nexus.runtime import local_relay
    path = local_relay._plugin_path(module)
    if not path:
        return None
    return os.path.dirname(path), module


_python_cache: str = ""


def _find_python() -> str:
    """A python interpreter that can actually serve the relay (``import
    uvicorn`` succeeds), or "" if none. Several pythons may be on PATH (e.g.
    an msys2 ``python3`` without uvicorn next to a CPython that has it), so we
    probe each candidate rather than trusting the first name. A frozen
    ``sys.executable`` is the NexusGrid exe (can't ``-m uvicorn``), so it only
    counts when we're not frozen."""
    global _python_cache
    if _python_cache:
        return _python_cache
    candidates = [shutil.which(n) for n in ("python3", "python")]
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for py in candidates:
        if not py:
            continue
        try:
            subprocess.run([py, "-c", "import uvicorn"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        except Exception:
            continue
        _python_cache = py
        return py
    return ""


def _container_argv(engine, image, srcdir, modname, port, grid_key, allow_outbound):
    argv = [
        engine, "run", "-d", "--rm",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--memory", "512m",
        "--cpus", "1.0",
        "-v", f"{srcdir}:/relay:ro",
        "-e", f"NEXUS_GRID_KEY={grid_key}",
    ]
    if not allow_outbound:
        argv += ["--network", rr.ISOLATED_NET]  # internal net: no egress
    argv += [
        "-p", f"127.0.0.1:{port}:{_CONTAINER_PORT}",
        image,
        "uvicorn", f"{modname}:app",
        "--app-dir", "/relay", "--host", "0.0.0.0", "--port", str(_CONTAINER_PORT),
    ]
    return argv


def _fingerprint(module: str) -> str:
    try:
        from nexus.runtime import local_relay
        return local_relay._fingerprint_for_module(module)
    except Exception:
        return ""


async def run_sandboxed_relay(module, port, runner_name, grid_key, agreed,
                              image="", allow_outbound=False) -> dict:
    """Run *module* as a sandboxed, out-of-process relay on *port* via
    *runner_name*. The caller must have shown the consent panel and pass
    ``agreed=True`` (running relay code is arbitrary code execution)."""
    if not agreed:
        return {"ok": False, "error": "consent_required"}
    if len(_sandboxed) >= _MAX_SANDBOXED:
        return {"ok": False, "error": "too_many"}

    src = _relay_source(module)
    if not src:
        return {"ok": False, "error": "no_such_module"}
    srcdir, modname = src

    rr._ensure_builtins()
    rr._load_custom_runners()
    runner = rr._RUNNERS.get(str(runner_name).strip().lower())
    if not runner:
        return {"ok": False, "error": "unknown_runner"}
    if not runner["available"]():
        return {"ok": False, "error": "runner_unavailable"}

    port = int(port)
    grid_key = (grid_key or "").strip() or "nexus-beta-key"
    rid = uuid.uuid4().hex[:16]
    rec: dict = {
        "sandbox_id": rid, "module": module, "runner": runner_name, "port": port,
        "kind": runner["kind"], "sandboxed": runner["sandboxed"],
        "fingerprint": _fingerprint(module), "container_id": None, "proc": None,
        "url": f"ws://127.0.0.1:{port}",
    }

    try:
        if runner["kind"] == "container":
            image = (image or str(LOCAL_SETTINGS.get("relay_sandbox_image", "") or "")).strip()
            if not image:
                return {"ok": False, "error": "image_required"}
            if not rr._image_allowed(image):
                return {"ok": False, "error": "image_not_allowed"}
            engine = runner.get("engine") or "docker"
            if not allow_outbound:
                await rr._ensure_isolated_network(engine)
            argv = _container_argv(engine, image, srcdir, modname, port,
                                   grid_key, allow_outbound)
            out = await asyncio.to_thread(
                subprocess.check_output, argv, stderr=subprocess.STDOUT, text=True)
            rec["container_id"] = out.strip().splitlines()[-1] if out.strip() else None
        else:
            python = _find_python()
            if not python:
                return {"ok": False, "error": "no_python"}
            argv = [python, "-m", "uvicorn", f"{modname}:app",
                    "--app-dir", srcdir, "--host", "127.0.0.1", "--port", str(port)]
            # raw is the "no sandbox" option: run with the node's full
            # environment (so the interpreter finds its own site-packages,
            # incl. user-site via APPDATA) plus the relay's grid key. Isolation
            # is the container runners' job, not raw's.
            env = dict(os.environ)
            env["NEXUS_GRID_KEY"] = grid_key
            proc = await asyncio.to_thread(
                subprocess.Popen, argv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            child_job.bind(proc)  # dies with the node, even on a hard kill
            rec["proc"] = proc
    except Exception as exc:
        _log.warning("sandboxed relay spawn failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)[:300]}

    _sandboxed[rid] = rec
    return {"ok": True, "relay": _public(rec)}


def _public(rec: dict) -> dict:
    return {k: rec[k] for k in
            ("sandbox_id", "module", "runner", "port", "kind", "sandboxed",
             "fingerprint", "container_id", "url")}


def fingerprint_for_port(port: int) -> str:
    """Code fingerprint of a sandboxed relay bound to *port*, or "". Lets
    :func:`local_relay.fingerprint_for_url` validate binds to a sandboxed relay
    just like it does for the primary / instances."""
    for rec in _sandboxed.values():
        if rec["port"] == int(port):
            proc = rec.get("proc")
            alive = (proc.poll() is None) if proc else True
            if alive:
                return rec.get("fingerprint", "")
    return ""


def list_sandboxed_relays() -> dict:
    out = []
    for rec in _sandboxed.values():
        r = _public(rec)
        proc = rec.get("proc")
        r["running"] = (proc.poll() is None) if proc else True
        out.append(r)
    return {"relays": out}


async def stop_sandboxed_relay(sandbox_id: str) -> dict:
    rec = _sandboxed.get(sandbox_id)
    if not rec:
        return {"ok": False, "error": "not_found"}
    try:
        if rec.get("container_id"):
            engine = rr._RUNNERS.get(rec["runner"], {}).get("engine") or "docker"
            await asyncio.to_thread(
                subprocess.run, [engine, "stop", rec["container_id"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif rec.get("proc"):
            rec["proc"].terminate()
    except Exception:
        _log.debug("stop sandboxed relay best-effort", exc_info=True)
    _sandboxed.pop(sandbox_id, None)
    return {"ok": True}


__all__ = ["run_sandboxed_relay", "list_sandboxed_relays", "stop_sandboxed_relay",
           "fingerprint_for_port"]
