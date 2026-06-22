"""Replication auto-run.

A consumer who copied a replicable service can stand it up on their OWN machine
from the provider's structured run-spec ({image, cmd, env, ports}). Running a
provider's code is arbitrary code execution, so this module is built around
*containment*:

* **Pluggable runners** (like pumps). Built-in: ``docker`` and ``podman``
  (hardened: cap-drop ALL, no-new-privileges, read-only rootfs, mem/cpu caps,
  loopback-only port publish) and ``raw`` (no sandbox — the explicit danger
  option). A host can register its own runner (gVisor, firejail, bubblewrap,
  Windows Sandbox, a microVM, …) by dropping a ``nexus_runners/*.py`` plugin.
* **Structured spec only** — we never execute the free-form readme/shell.
* **Explicit consent** — the API refuses unless the caller passes ``agreed``.
* **Image allowlist** — container images must be on ``allowed_images``.
* **Loopback-only network by default** — a per-replica opt-in re-enables outbound.

Nothing here runs on its own; the local API calls :func:`run_replica` only after
the user agreed in the consent panel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import uuid

from nexus.core.config import LOCAL_SETTINGS
from nexus.runtime import child_job

_log = logging.getLogger("nexus.runtime.replica_runner")

# A dedicated internal docker/podman network: containers can serve their
# published loopback ports but have no route to the internet or the LAN. Used
# for the default (no-outbound) network mode.
ISOLATED_NET = "nexus-isolated"

_DEFAULT_MEM_MB = 1024
_DEFAULT_CPUS = "1.0"
_MAX_REPLICAS = 24

# replica_id -> {provider_uuid, service, runner, sandboxed, network, endpoints,
#                container_id|None, proc|None, started_at}
_replicas: dict[str, dict] = {}

# name -> {"build": callable(ctx)->argv, "sandboxed": bool, "available": callable()->bool,
#          "kind": "container"|"process", "engine": str|None}
_RUNNERS: dict[str, dict] = {}
_custom_loaded = False


def register_runner(name, build, *, sandboxed=True, available=None,
                    kind="container", engine=None) -> None:
    """Register a runner. ``build(ctx)`` returns the argv list to spawn, where
    ``ctx`` has ``spec, host_ports, allow_outbound, mem_mb, cpus``."""
    _RUNNERS[str(name).strip().lower()] = {
        "build": build, "sandboxed": bool(sandboxed),
        "available": available or (lambda: True),
        "kind": kind, "engine": engine,
    }


def _free_loopback_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# --- built-in runners --------------------------------------------------------


def _container_argv(engine: str, ctx: dict) -> list[str]:
    spec = ctx["spec"]
    argv = [
        engine, "run", "-d", "--rm",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--memory", f"{ctx['mem_mb']}m",
        "--cpus", str(ctx["cpus"]),
    ]
    if ctx["allow_outbound"]:
        pass  # default bridge network has outbound NAT
    elif spec["ports"]:
        argv += ["--network", ISOLATED_NET]  # internal net: inbound only, no egress
    else:
        argv += ["--network", "none"]
    # GPU passthrough: the CLI form of device_requests. "all" or a count, only
    # when the run-spec asked for it. No request => no flag (unchanged launch).
    from nexus.runtime.docker_client import _gpu_device_count

    gpu_count = _gpu_device_count(spec.get("gpu"))
    if gpu_count is not None:
        argv += ["--gpus", "all" if gpu_count == -1 else str(gpu_count)]
    for cp, hp in zip(spec["ports"], ctx["host_ports"]):
        argv += ["-p", f"127.0.0.1:{hp}:{cp}"]
    for e in spec["env"]:
        argv += ["-e", e]
    if ctx.get("inputs_dir"):  # A2: cloud inputs, read-only at /nexus/inputs
        argv += ["-v", f"{ctx['inputs_dir']}:/nexus/inputs:ro"]
    argv.append(spec["image"])
    if spec["cmd"]:
        argv += spec["cmd"].split()
    return argv


def _raw_argv(ctx: dict) -> list[str]:
    # No sandbox, no image: run the declared command directly on the host.
    spec = ctx["spec"]
    cmd = spec["cmd"] or spec["image"]
    return cmd.split()


def _ensure_builtins() -> None:
    if "docker" in _RUNNERS:
        return
    register_runner("docker", lambda c: _container_argv("docker", c),
                    sandboxed=True, available=lambda: bool(shutil.which("docker")),
                    kind="container", engine="docker")
    register_runner("podman", lambda c: _container_argv("podman", c),
                    sandboxed=True, available=lambda: bool(shutil.which("podman")),
                    kind="container", engine="podman")
    register_runner("raw", _raw_argv, sandboxed=False, available=lambda: True,
                    kind="process", engine=None)


def _load_custom_runners() -> None:
    """Import host-trusted runner plugins from a ``nexus_runners/`` folder next
    to BASE_DIR (best-effort, like pumps)."""
    global _custom_loaded
    if _custom_loaded:
        return
    _custom_loaded = True
    try:
        import importlib.util

        from nexus.core.paths import BASE_DIR
        d = BASE_DIR / "nexus_runners"
        if not d.is_dir():
            return
        for f in sorted(d.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"nexus_runners.{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # plugin calls register_runner(...)
            except Exception:
                _log.warning("failed to load runner plugin %s", f.name, exc_info=True)
    except Exception:
        _log.debug("custom runner load skipped", exc_info=True)


def available_runners() -> list[dict]:
    _ensure_builtins()
    _load_custom_runners()
    return [
        {"name": n, "sandboxed": r["sandboxed"], "available": bool(r["available"]())}
        for n, r in sorted(_RUNNERS.items())
    ]


# --- the gate + spawn --------------------------------------------------------


async def _fetch_public_service(provider_uuid: str, service_name: str) -> dict | None:
    from nexus.networking.peer_http import peer_http_post
    from nexus.runtime.service_grants import resolve_peer_addr
    addr = await resolve_peer_addr(provider_uuid) or provider_uuid
    res = await peer_http_post(addr, "/peer/profile", {}, timeout=5.0)
    if res.get("status") != 200:
        return None
    body = res.get("body") or {}
    return next((s for s in (body.get("hosted_services") or [])
                 if isinstance(s, dict) and s.get("name") == service_name), None)


def _image_allowed(image: str) -> bool:
    allow = LOCAL_SETTINGS.get("allowed_images") or []
    # Match by repository (ignore the tag) so "python:3.11-slim" allows
    # "python:3.11-slim" and a bare "python" entry allows any python tag.
    repo = image.split(":", 1)[0]
    for a in allow:
        if image == a or repo == str(a).split(":", 1)[0]:
            return True
    return False


# --- A1: custom build context ------------------------------------------------
#
# A run-spec may carry a {dockerfile, files} build context the consumer builds
# locally instead of pulling a prebuilt image. Invariants (on top of the
# existing consent + sandbox): the Dockerfile's FROM base(s) must be on the
# image allowlist, and the whole context must fit ``build_max_bytes``. The
# built image is cached by a content fingerprint so a re-launch is instant.


def _from_bases(dockerfile: str) -> list[str]:
    """Return the external base image(s) a Dockerfile FROMs (skips internal
    multi-stage names and ``scratch``)."""
    bases: list[str] = []
    stages: set[str] = set()
    for line in dockerfile.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # Tokenize on ANY whitespace so a tab after FROM (which Docker accepts)
        # can't hide a base from the allowlist check. Security F-008.
        parts = s.split()
        if not parts or parts[0].upper() != "FROM":
            continue
        toks = [t for t in parts[1:] if not t.startswith("--")]
        if not toks:
            continue
        img = toks[0]
        asname = toks[2] if len(toks) >= 3 and toks[1].upper() == "AS" else ""
        if img.lower() != "scratch" and img.lower() not in stages:
            bases.append(img)
        if asname:
            stages.add(asname.lower())
    return bases


def build_context_size(build: dict) -> int:
    """Total bytes of a build context (Dockerfile text + bundled files)."""
    n = len((build.get("dockerfile") or "").encode("utf-8"))
    for v in (build.get("files") or {}).values():
        n += len(str(v).encode("utf-8"))
    return n


def validate_build(build: dict) -> tuple[bool, str]:
    """Gate a build context: non-empty Dockerfile, within the size cap, and
    every FROM base on the image allowlist. Returns ``(ok, reason)``."""
    if not build or not (build.get("dockerfile") or "").strip():
        return False, "no_dockerfile"
    size = build_context_size(build)
    cap = int(LOCAL_SETTINGS.get("build_max_bytes", 5 * 1024 * 1024) or 0)
    if cap and size > cap:
        return False, f"build_too_large:{size}>{cap}"
    bases = _from_bases(build["dockerfile"])
    if not bases:
        return False, "no_from"
    for b in bases:
        if not _image_allowed(b):
            return False, f"base_not_allowed:{b}"
    return True, ""


def build_fingerprint(build: dict) -> str:
    """Content hash of a build context — the built-image tag suffix + cache key."""
    import hashlib

    h = hashlib.sha256()
    h.update((build.get("dockerfile") or "").encode("utf-8"))
    for k in sorted((build.get("files") or {}).keys()):
        h.update(b"\x00" + k.encode("utf-8") + b"\x00")
        h.update(str(build["files"][k]).encode("utf-8"))
    return h.hexdigest()[:16]


def _write_build_dir(build: dict, dest) -> None:
    from pathlib import Path

    d = Path(dest)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text(build.get("dockerfile") or "", encoding="utf-8")
    for rel, content in (build.get("files") or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content), encoding="utf-8")


async def _run_rc(argv: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode if proc.returncode is not None else 1


async def ensure_built_image(engine: str, build: dict) -> tuple[str, str]:
    """Validate + build (or reuse) the image for *build*. Returns
    ``(tag, "")`` on success or ``("", reason)`` on failure. Cached by
    fingerprint: an already-built tag is reused without rebuilding."""
    import tempfile
    import shutil as _sh

    ok, reason = validate_build(build)
    if not ok:
        return "", reason
    tag = f"nexus_built_{build_fingerprint(build)}"
    try:
        if await _run_rc([engine, "image", "inspect", tag]) == 0:
            return tag, ""  # cache hit — already built
    except Exception:
        pass
    d = tempfile.mkdtemp(prefix="nexus_build_")
    try:
        _write_build_dir(build, d)
        _log.info("[A1] building image %s via %s", tag, engine)
        if await _run_rc([engine, "build", "-t", tag, d]) != 0:
            return "", "build_failed"
        return tag, ""
    except Exception as exc:
        return "", f"build_error:{type(exc).__name__}"
    finally:
        _sh.rmtree(d, ignore_errors=True)


async def run_replica(provider_uuid: str, service_name: str, runner_name: str,
                      allow_outbound: bool, agreed: bool) -> dict:
    """Stand up a replica of *service_name* (offered by *provider_uuid*) using
    *runner_name*. The caller must have shown the consent panel and pass
    ``agreed=True``."""
    if not agreed:
        return {"ok": False, "error": "consent_required"}
    if len(_replicas) >= _MAX_REPLICAS:
        return {"ok": False, "error": "too_many_replicas"}

    _ensure_builtins()
    _load_custom_runners()
    runner = _RUNNERS.get(str(runner_name).strip().lower())
    if not runner:
        return {"ok": False, "error": "unknown_runner"}
    if not runner["available"]():
        return {"ok": False, "error": "runner_unavailable"}

    svc = await _fetch_public_service(provider_uuid, service_name)
    if not svc:
        return {"ok": False, "error": "no_such_service"}
    if not svc.get("replicable"):
        return {"ok": False, "error": "not_replicable"}
    spec = svc.get("run") or {}
    build = spec.get("build") or {}
    if not spec.get("image") and not spec.get("cmd") and not build:
        return {"ok": False, "error": "no_run_spec"}

    # A1: a build context means we build the image locally instead of pulling.
    # Consent (agreed) already gated this call; the FROM base(s) are allowlist-
    # checked inside ensure_built_image and the result runs in the same sandbox.
    built_image = ""
    if build and runner["kind"] == "container":
        tag, err = await ensure_built_image(runner.get("engine") or "docker", build)
        if not tag:
            return {"ok": False, "error": f"build_failed:{err}"}
        built_image = tag

    # Container runners pull/launch an image — gate it on the allowlist so a
    # provider can't make us run an arbitrary image. A locally-built image
    # skips the tag check (its FROM base was already allowlist-validated).
    if runner["kind"] == "container" and not built_image and not _image_allowed(spec.get("image", "")):
        return {"ok": False, "error": "image_not_allowed"}

    # A2: download any declared cloud inputs into a per-replica dir the runner
    # mounts read-only (container) or runs in (raw). A failed download aborts
    # the launch loudly before anything is spawned.
    inputs_dir = ""
    if spec.get("inputs"):
        import tempfile

        from nexus.runtime import cloud_connector
        inputs_dir = tempfile.mkdtemp(prefix="nexus_inputs_")
        for it in spec["inputs"]:
            dest = os.path.join(inputs_dir, *it["dest"].split("/"))
            ok, reason = await cloud_connector.download(it["uri"], dest)
            if not ok:
                shutil.rmtree(inputs_dir, ignore_errors=True)
                return {"ok": False, "error": f"input_download_failed:{reason}"}

    host_ports = [_free_loopback_port() for _ in (spec.get("ports") or [])]
    # C4: resolve any ``secret://NAME`` env refs to their vault values now,
    # just before the runner materializes the env. Specs without secret refs
    # pass through unchanged; an unknown ref fails the launch loudly.
    from nexus.runtime import secrets_vault
    try:
        resolved_env = await secrets_vault.resolve_refs(spec.get("env", []))
    except secrets_vault.SecretError as exc:
        return {"ok": False, "error": f"secret_unresolved:{exc}"}
    ctx = {"spec": {"image": built_image or spec.get("image", ""), "cmd": spec.get("cmd", ""),
                    "env": resolved_env, "ports": spec.get("ports", []), "gpu": spec.get("gpu")},
           "host_ports": host_ports, "allow_outbound": bool(allow_outbound),
           "mem_mb": _DEFAULT_MEM_MB, "cpus": _DEFAULT_CPUS, "inputs_dir": inputs_dir}
    argv = runner["build"](ctx)

    rid = uuid.uuid4().hex[:16]
    rec: dict = {
        "replica_id": rid, "provider_uuid": provider_uuid, "service": service_name,
        "runner": runner_name, "sandboxed": runner["sandboxed"],
        "network": "outbound" if allow_outbound else "loopback",
        "endpoints": [{"container_port": cp, "host": "127.0.0.1", "port": hp}
                      for cp, hp in zip(spec.get("ports", []), host_ports)],
        "container_id": None, "proc": None, "inputs_dir": inputs_dir,
    }

    try:
        if runner["kind"] == "container":
            if not allow_outbound and spec.get("ports"):
                await _ensure_isolated_network(runner.get("engine") or "docker")
            out = await asyncio.to_thread(
                subprocess.check_output, argv, stderr=subprocess.STDOUT, text=True)
            rec["container_id"] = out.strip().splitlines()[-1] if out.strip() else None
        else:
            # raw: minimal env (host secrets not inherited) + the spec env.
            env = {"PATH": os.environ.get("PATH", "")}
            if os.name == "nt":
                env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            for e in spec.get("env", []):
                k, _, v = e.partition("=")
                env[k] = v
            proc = await asyncio.to_thread(
                subprocess.Popen, argv,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                cwd=inputs_dir or None)  # A2: raw runner sees inputs in cwd
            child_job.bind(proc)  # dies with the node, even on a hard kill
            rec["proc"] = proc
    except Exception as exc:
        _log.warning("replica spawn failed: %s", exc, exc_info=True)
        if inputs_dir:
            shutil.rmtree(inputs_dir, ignore_errors=True)
        return {"ok": False, "error": "spawn_failed", "detail": str(exc)[:300]}

    _replicas[rid] = rec
    return {"ok": True, "replica": _public_replica(rec)}


async def _ensure_isolated_network(engine: str) -> None:
    """Create the internal (no-egress) network once; ignore "already exists"."""
    try:
        await asyncio.to_thread(
            subprocess.run, [engine, "network", "create", "--internal", ISOLATED_NET],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _log.debug("ensure isolated network best-effort", exc_info=True)


def _public_replica(rec: dict) -> dict:
    return {k: rec[k] for k in
            ("replica_id", "provider_uuid", "service", "runner", "sandboxed",
             "network", "endpoints", "container_id")}


def list_replicas() -> dict:
    out = []
    for rec in _replicas.values():
        r = _public_replica(rec)
        proc = rec.get("proc")
        r["running"] = (proc.poll() is None) if proc else True
        out.append(r)
    return {"replicas": out}


async def stop_replica(replica_id: str) -> dict:
    rec = _replicas.get(replica_id)
    if not rec:
        return {"ok": False, "error": "not_found"}
    try:
        if rec.get("container_id"):
            engine = _RUNNERS.get(rec["runner"], {}).get("engine") or "docker"
            await asyncio.to_thread(
                subprocess.run, [engine, "stop", rec["container_id"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif rec.get("proc"):
            rec["proc"].terminate()
    except Exception:
        _log.debug("stop replica best-effort", exc_info=True)
    if rec.get("inputs_dir"):  # A2: drop the downloaded cloud inputs
        shutil.rmtree(rec["inputs_dir"], ignore_errors=True)
    _replicas.pop(replica_id, None)
    return {"ok": True}


__all__ = [
    "register_runner", "available_runners", "run_replica",
    "list_replicas", "stop_replica",
]
