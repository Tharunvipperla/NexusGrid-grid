"""Top-level execution dispatcher.

Ported from Phase-1/node_modified.py (``execute_bundle_with_watchdog`` at
lines 2817-3785, ~970 LOC). One coroutine that:

1. Parses ``task.json`` out of the workspace dir.
2. Resolves the P2P cache (if ``cloud_uri`` is set).
3. Enforces the security profile and pre-execution threat scan.
4. Dispatches to the Docker / native / WASM path.
5. Enforces a watchdog loop: interrupt/preempt/OOM check, RAM clamp updates,
   child-process snapshots for the UI.
6. Archives the workspace and returns ``(status_meta, archive_path)``.

Every Phase-1 helper is routed through its Phase-2 home — no behaviour
change, same log shape, same exit semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import psutil

from nexus.caches import (
    detect_uv,
    node_cache_key,
    node_cache_root,
    pip_wheel_cache_dir,
    venv_cache_key,
    venv_cache_root,
)
from nexus.core import LOCAL_SETTINGS, STATE
from nexus.runtime.capacity import image_allowed
from nexus.runtime.docker_client import docker_security_opts, get_docker_client
from nexus.runtime.native_sandbox import (
    SandboxUnavailable,
    assign_to_job_object,
    make_resource_limits,
    release_job_object,
    wrap_command_with_sandbox,
)
from nexus.runtime.process_tree import kill_process_tree, snapshot_proc_children
from nexus.runtime.service_runner import (
    ServiceManifestError,
    is_service_manifest,
    start_service,
    validate_service_manifest,
)
from nexus.runtime.workspace import resolve_p2p_cache
from nexus.runtime.worker_state import (
    register_running_container,
    register_running_proc,
    unregister_running_container,
    unregister_running_proc,
    update_local_task_children,
    update_local_task_stage,
)
from nexus.security.entrypoint import (
    EntrypointError,
    validate_entrypoint,
    validate_setup_cmd,
)
from nexus.security.threat_scanner import (
    is_scan_required,
    scan_workspace_for_threats,
)
from nexus.tasks.lifecycle import is_task_interrupted, is_task_preempted
from nexus.telemetry.logs import LogStream, unstreamed_tail
from nexus.utils.text import mask_ips_in_log, prepare_multiline_command
from nexus.utils.time import timestamp

_log = logging.getLogger("nexus.runtime.executor")


async def execute_bundle_with_watchdog(
    workspace_dir: str,
    task_id: str,
    dynamic_env: dict,
    master_ip: str,
) -> tuple[dict, str]:
    """Run the task bundle in *workspace_dir*; return ``(status_meta, archive_path)``."""
    manifest_path = os.path.join(workspace_dir, "task.json")
    try:
        with open(manifest_path, "r") as f:
            m = json.load(f)
    except Exception:
        m = {}

    runtime = m.get("runtime", "docker")
    entrypoint = m.get("entrypoint", "python main.py")
    docker_image = m.get("image", "python:3.11-slim")
    setup_cmd = m.get("setup_cmd", "")
    req_ram = int(m.get("ram_limit_mb", 512) or 512)
    req_cpu = int(m.get("cpu_limit_pct", 100) or 100)
    cloud_uri = m.get("cloud_uri", "")
    network_required = bool(m.get("network_required", False))

    sys_ram = psutil.virtual_memory().total // (1024 * 1024)
    abs_max = int(sys_ram * (LOCAL_SETTINGS["max_ram_pct"] / 100.0))
    free_ram = psutil.virtual_memory().available // (1024 * 1024)
    safe_ram = min(req_ram, abs_max, max(128, free_ram - 256))

    merged_env = {"NEXUS_TASK_ID": task_id}
    merged_env.update(dynamic_env)

    elastic_log = LogStream(task_id)
    elastic_log += f"[{timestamp()}] [WORKER] Initializing Engine: {runtime.upper()}\n"
    await update_local_task_stage(task_id, "initializing")
    if dynamic_env:
        elastic_log += mask_ips_in_log(
            f"[{timestamp()}] [DAG PLANNER] Sliced Variables Injected: {dynamic_env}\n"
        )

    if cloud_uri:
        elastic_log += f"[{timestamp()}] [P2P MESH] Resolving {cloud_uri}...\n"
        cache_path = await resolve_p2p_cache(cloud_uri, master_ip)
        merged_env["NEXUS_LOCAL_CACHE"] = cache_path
        elastic_log += (
            f"[{timestamp()}] [P2P MESH] Dataset resolved to local fast-cache.\n"
        )

    security_profile = LOCAL_SETTINGS.get("security_profile", "maximum")
    # A task may request a STRICTER profile than this worker's default;
    # requests to relax are ignored — the worker's posture is the floor.
    _PROFILE_RANK = {"relaxed": 0, "standard": 1, "maximum": 2}
    _req_profile = str(m.get("security_profile") or "").strip()
    if _PROFILE_RANK.get(_req_profile, -1) > _PROFILE_RANK.get(str(security_profile), 2):
        security_profile = _req_profile
    status_meta: dict = {"status": "fatal_error", "output": str(elastic_log)}

    try:
        if await is_task_interrupted(task_id):
            archive = await asyncio.to_thread(
                shutil.make_archive,
                base_name=workspace_dir + "_out",
                format="zip",
                root_dir=workspace_dir,
            )
            return {
                "status": "failed",
                "output": str(elastic_log)
                + f"[{timestamp()}] [WORKER] Task disrupted before launch.\n",
            }, archive

        # --- SECURITY: Network-required gate ---
        if network_required and not LOCAL_SETTINGS.get("allow_network_tasks", False):
            elastic_log += (
                f"[{timestamp()}] [SECURITY] Task requests network access "
                "but worker disallows network tasks.\n"
            )
            await unregister_running_container(task_id)
            archive = await asyncio.to_thread(
                shutil.make_archive,
                base_name=workspace_dir + "_out",
                format="zip",
                root_dir=workspace_dir,
            )
            return (
                {"status": "failed", "output": mask_ips_in_log(str(elastic_log))},
                archive,
            )

        # --- SECURITY: Pre-execution threat scan ---
        await update_local_task_stage(task_id, "scanning")
        if is_scan_required(
            security_profile,
            bool(
                LOCAL_SETTINGS.get("enable_task_scanning", True)
                or m.get("enable_task_scanning", False)
            ),
        ):
            scan_findings = await scan_workspace_for_threats(
                workspace_dir, profile=security_profile
            )
            if scan_findings:
                elastic_log += (
                    f"[{timestamp()}] [SECURITY SCAN] "
                    f"{len(scan_findings)} threat(s) detected:\n"
                )
                for _f in scan_findings:
                    elastic_log += (
                        f"  - {_f['threat']} in {_f['file']}: {_f['sample']}\n"
                    )
                elastic_log += (
                    f"[{timestamp()}] [SECURITY] Task blocked due to threat detection.\n"
                )
                await unregister_running_container(task_id)
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return (
                    {
                        "status": "failed",
                        "output": mask_ips_in_log(str(elastic_log)),
                    },
                    archive,
                )
            else:
                elastic_log += (
                    f"[{timestamp()}] [SECURITY SCAN] No threats detected.\n"
                )

        # --- SECURITY: Entrypoint / setup command validation ---
        try:
            validate_entrypoint(entrypoint, security_profile)
            validate_setup_cmd(setup_cmd, security_profile)
        except EntrypointError as exc:
            elastic_log += (
                f"[{timestamp()}] [SECURITY] Entrypoint rejected: {exc}\n"
            )
            await unregister_running_container(task_id)
            archive = await asyncio.to_thread(
                shutil.make_archive,
                base_name=workspace_dir + "_out",
                format="zip",
                root_dir=workspace_dir,
            )
            return (
                {
                    "status": "failed",
                    "output": mask_ips_in_log(str(elastic_log)),
                },
                archive,
            )

        # ------------------------------------------------------------------
        # SERVICE PATH 
        # ------------------------------------------------------------------
        if is_service_manifest(m):
            try:
                spec = validate_service_manifest(m)
            except ServiceManifestError as exc:
                elastic_log += (
                    f"[{timestamp()}] [SERVICE] Manifest invalid: {exc}\n"
                )
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return (
                    {"status": "failed", "output": str(elastic_log)},
                    archive,
                )

            elastic_log += (
                f"[{timestamp()}] [SERVICE] Launching {spec['image']} on ports "
                f"{spec['expose_ports']} (duration={spec['duration_sec']}s, "
                f"idle_timeout={spec['idle_timeout_sec']}s)\n"
            )
            try:
                record = await start_service(
                    task_id, m, env=merged_env, master_ip=master_ip
                )
            except Exception as exc:
                elastic_log += (
                    f"[{timestamp()}] [SERVICE] Start failed: {exc}\n"
                )
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return (
                    {"status": "failed", "output": str(elastic_log)},
                    archive,
                )

            port_map = STATE.service_port_mappings.get(task_id, {})
            elastic_log += (
                f"[{timestamp()}] [SERVICE] Started; container_port→host_port "
                f"map: {port_map}\n"
            )

            # Block until the watchdog terminates the service (duration limit,
            # idle timeout, manual stop, or crash). The watchdog is the single
            # owner of stop_service, so we just wait for it.
            watchdog = STATE.service_watchdog_tasks.get(task_id)
            if watchdog is not None:
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — surface in log
                    elastic_log += (
                        f"[{timestamp()}] [SERVICE] Watchdog error: {exc}\n"
                    )

            async with STATE.service_lock:
                final_record = STATE.service_records.get(task_id) or record
            stop_reason = str(final_record.get("stop_reason") or "exited")
            final_status = (
                "completed"
                if stop_reason in ("duration_limit", "idle_timeout", "manual")
                else "failed"
            )
            elastic_log += (
                f"[{timestamp()}] [SERVICE] Stopped ({stop_reason}). "
                f"final_status={final_status}\n"
            )
            archive = await asyncio.to_thread(
                shutil.make_archive,
                base_name=workspace_dir + "_out",
                format="zip",
                root_dir=workspace_dir,
            )
            return (
                {"status": final_status, "output": str(elastic_log)},
                archive,
            )

        # ------------------------------------------------------------------
        # DOCKER PATH
        # ------------------------------------------------------------------
        if runtime == "docker":
            try:
                docker_client = get_docker_client()
            except RuntimeError as e:
                status_meta = {
                    "status": "fatal_error",
                    "output": str(elastic_log)
                    + f"\n[{timestamp()}] [WORKER] Docker runtime unavailable: {e}\n"
                    + f"[{timestamp()}] [HINT] Start Docker Desktop / Docker Engine, "
                    "or use a different runtime (native/wasm).",
                }
                await unregister_running_container(task_id)
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return status_meta, archive

            if not image_allowed(docker_image):
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return (
                    {
                        "status": "failed",
                        "output": str(elastic_log)
                        + f"[{timestamp()}] [SECURITY] Image '{docker_image}' "
                        "blocked by local allowlist.\n",
                    },
                    archive,
                )

            elastic_log += (
                f"[{timestamp()}] [DOCKER] Running task in allowed path: "
                f"{workspace_dir} (image={docker_image})\n"
            )
            elastic_log += (
                f"[{timestamp()}] [WORKER] RAM request {req_ram} MB; "
                f"local safety ceiling {abs_max} MB; container clamp {safe_ram} MB.\n"
            )
            elastic_log += (
                f"[{timestamp()}] [SECURITY] Profile: {security_profile.upper()} | "
                f"Network cut: "
                f"{'NO (network_required)' if network_required else 'YES after setup'}\n"
            )
            safe_entrypoint = prepare_multiline_command(entrypoint)
            safe_setup = prepare_multiline_command(setup_cmd)

            # Pull image if not present
            try:
                await asyncio.to_thread(docker_client.images.get, docker_image)
            except Exception:
                await update_local_task_stage(task_id, "pulling_image")
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Downloading environment image "
                    f"({docker_image})...\n"
                )
                await asyncio.to_thread(docker_client.images.pull, docker_image)

            # --- TWO-PHASE HARDENED EXECUTION ---
            # Phase A: Create container with keep-alive command, network ON for setup
            sec_opts = docker_security_opts(security_profile)
            # Maximum profile mounts a read-only root and runs as user 65534
            # (nobody) whose ``$HOME`` is /nonexistent. Pip needs a writable
            # home for ``--user`` installs AND that mount must be exec-enabled
            # so Python can ``mmap()`` native extensions like numpy's
            # ``_multiarray_umath.so``. Docker's tmpfs ``exec`` flag is not
            # reliably honored on Windows Docker Desktop, so we bind a
            # per-task host directory at /home/runner instead — host bind
            # mounts inherit the host filesystem's exec capability and
            # require no daemon-side flag negotiation.
            container_volumes = {workspace_dir: {"bind": "/workspace", "mode": "rw"}}
            home_bind_dir: str | None = None
            if security_profile == "maximum":
                home_bind_dir = tempfile.mkdtemp(
                    prefix=f"nexus-home-{task_id[:12]}-",
                    dir=os.path.dirname(workspace_dir) or None,
                )
                container_volumes[home_bind_dir] = {
                    "bind": "/home/runner",
                    "mode": "rw",
                }
                merged_env.setdefault("HOME", "/home/runner")
                merged_env.setdefault("PIP_USER", "1")
                merged_env.setdefault("PIP_NO_CACHE_DIR", "1")
                # Pip's 15s default read timeout fails on big wheels
                # (numpy, torch) when the network is shared with a foreign-
                # storage transfer. 180s is generous but still bounded.
                merged_env.setdefault("PIP_DEFAULT_TIMEOUT", "180")
                merged_env.setdefault("PIP_RETRIES", "5")
                # Make the user-site bin dir reachable for any installed CLI
                # tools (pytest, etc.) the entrypoint might call.
                merged_env.setdefault(
                    "PATH",
                    "/home/runner/.local/bin:/usr/local/sbin:/usr/local/bin:"
                    "/usr/sbin:/usr/bin:/sbin:/bin",
                )
            container_kwargs = {
                "image": docker_image,
                "command": "sleep infinity",
                "working_dir": "/workspace",
                "environment": merged_env,
                "volumes": container_volumes,
                "network_mode": "bridge",
                "mem_limit": f"{safe_ram}m",
                "cpu_quota": int((req_cpu / 100.0) * 100000),
                "detach": True,
            }
            container_kwargs.update(sec_opts)
            container = await asyncio.to_thread(
                docker_client.containers.run, **container_kwargs
            )
            await register_running_container(task_id, container)
            # Stash the home bind dir so unregister_running_container removes
            # it on every exit path.
            if home_bind_dir:
                async with STATE.running_container_lock:
                    STATE.running_task_cleanup_dirs[task_id] = home_bind_dir

            # Phase B: Setup command WITH network
            setup_output = ""
            if safe_setup:
                await update_local_task_stage(task_id, "installing_deps")
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Running setup (network ON): "
                    f"{safe_setup}\n"
                )
                setup_exec = await asyncio.to_thread(
                    container.exec_run,
                    ["sh", "-c", safe_setup],
                    workdir="/workspace",
                    environment=merged_env,
                )
                setup_output = (
                    setup_exec.output.decode("utf-8", errors="replace")
                    if setup_exec.output
                    else ""
                )
                if setup_exec.exit_code != 0:
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Setup failed "
                        f"(exit {setup_exec.exit_code}).\n---\n{setup_output}"
                    )
                    try:
                        await asyncio.to_thread(container.stop, timeout=2)
                    except Exception:
                        pass
                    try:
                        await asyncio.to_thread(container.remove, force=True)
                    except Exception:
                        pass
                    await unregister_running_container(task_id)
                    archive = await asyncio.to_thread(
                        shutil.make_archive,
                        base_name=workspace_dir + "_out",
                        format="zip",
                        root_dir=workspace_dir,
                    )
                    return {"status": "failed", "output": str(elastic_log)}, archive
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Setup completed successfully.\n"
                )
                # Surface setup (dependency install) output in the live log too,
                # so the final result keeps it once we stop appending
                # terminal_output below.
                if setup_output.strip():
                    elastic_log += setup_output

            # Phase C: Disconnect network BEFORE main entrypoint
            if not network_required:
                try:
                    bridge = await asyncio.to_thread(
                        docker_client.networks.get, "bridge"
                    )
                    await asyncio.to_thread(bridge.disconnect, container)
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Network disconnected — "
                        "main task runs in isolation.\n"
                    )
                except Exception as net_err:
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Warning: Could not "
                        f"disconnect network: {net_err}\n"
                    )
            else:
                elastic_log += (
                    f"[{timestamp()}] [SECURITY] Network kept ON "
                    "(task requested network_required).\n"
                )

            # Phase D: Execute main entrypoint WITHOUT network
            _output_log_path = "/tmp/nexus_exec_output.log"
            await update_local_task_stage(task_id, "executing")
            main_exec = await asyncio.to_thread(
                container.exec_run,
                [
                    "sh",
                    "-c",
                    f"{safe_entrypoint} > {_output_log_path} 2>&1",
                ],
                workdir="/workspace",
                environment=merged_env,
                detach=True,
            )
            # Docker SDK exec_run(detach=True) returns ExecResult(exit_code=None, output=exec_id)
            exec_id = (
                main_exec.output
                if hasattr(main_exec, "output") and isinstance(main_exec.output, str)
                else (main_exec.id if hasattr(main_exec, "id") else None)
            )
            elastic_log += (
                f"[{timestamp()}] [WORKER] Entrypoint launched "
                f"(exec_id={'set' if exec_id else 'NONE — will poll container status'}).\n"
            )

            if not exec_id:
                try:
                    c_info = await asyncio.to_thread(
                        docker_client.api.inspect_container, container.id
                    )
                    exec_ids = c_info.get("ExecIDs") or []
                    if exec_ids:
                        exec_id = exec_ids[-1]
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Recovered exec_id from "
                            "container inspect.\n"
                        )
                except Exception:
                    pass

            last_ram, preempted, disrupted = safe_ram, False, False
            watchdog_start = time.time()
            streamed = 0  # bytes of the entrypoint's output file already live-tailed

            async def _stream_docker_output() -> None:
                """Append any new bytes of the redirected output file to the live
                log so Docker tasks stream like native ones (B4). Best-effort."""
                nonlocal elastic_log, streamed
                try:
                    cat = await asyncio.to_thread(
                        container.exec_run, f"cat {_output_log_path}", user="root"
                    )
                    full = cat.output or b""
                    if isinstance(full, str):
                        full = full.encode("utf-8", "replace")
                    new, streamed = unstreamed_tail(full, streamed)
                    if new:
                        elastic_log += new.decode("utf-8", errors="replace")
                except Exception:
                    pass

            while True:
                if exec_id:
                    try:
                        exec_info = await asyncio.to_thread(
                            docker_client.api.exec_inspect, exec_id
                        )
                        if not exec_info.get("Running", False):
                            break
                    except Exception:
                        break
                else:
                    await asyncio.to_thread(container.reload)
                    if container.status not in ("created", "running"):
                        break
                    if time.time() - watchdog_start > 300:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Watchdog timeout — "
                            "no exec_id and container still running.\n"
                        )
                        try:
                            await asyncio.to_thread(container.stop, timeout=2)
                        except Exception:
                            pass
                        break

                if await is_task_interrupted(task_id):
                    disrupted = True
                    try:
                        await asyncio.to_thread(container.stop, timeout=1)
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Task disrupted by master request.\n"
                    )
                    break
                if await is_task_preempted(task_id):
                    preempted = True
                    try:
                        await asyncio.to_thread(container.stop, timeout=1)
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                    elastic_log += (
                        f"\n[{timestamp()}] [WORKER] Task preempted locally "
                        "to save checkpoint."
                    )
                    break

                curr_free = psutil.virtual_memory().available // (1024 * 1024)
                curr_safe = min(req_ram, abs_max, max(128, curr_free - 256))

                if LOCAL_SETTINGS["mode"] == "user" and curr_free < 500:
                    preempted = True
                    await asyncio.to_thread(container.stop)
                    elastic_log += (
                        f"\n[{timestamp()}] [SYSTEM] Host RAM critical. "
                        "Task preempted to save OS."
                    )
                    break
                if (
                    LOCAL_SETTINGS["mode"] == "user"
                    and abs(curr_safe - last_ram) > 250
                ):
                    try:
                        await asyncio.to_thread(
                            container.update, mem_limit=f"{curr_safe}m"
                        )
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Dynamic RAM clamp "
                            f"adjusted to {curr_safe} MB.\n"
                        )
                        last_ram = curr_safe
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                # Live-tail the entrypoint's output as it grows.
                await _stream_docker_output()
                await asyncio.sleep(2)

            # Determine exit code from exec BEFORE running any more execs
            exec_exit_code = 1
            if exec_id and not preempted and not disrupted:
                try:
                    exec_info = await asyncio.to_thread(
                        docker_client.api.exec_inspect, exec_id
                    )
                    raw_code = exec_info.get("ExitCode")
                    exec_exit_code = int(raw_code) if raw_code is not None else 1
                except Exception:
                    pass

            # Read the final exec output from the redirected file: used for the
            # stdout.txt artifact and to flush any tail the live loop didn't
            # reach before the process exited. The main output is already in
            # elastic_log via live streaming, so we DON'T re-append it below
            # (that would duplicate it in the stored result).
            exec_output = ""
            if not preempted:
                full = b""
                try:
                    cat_result = await asyncio.to_thread(
                        container.exec_run, f"cat {_output_log_path}", user="root"
                    )
                    full = cat_result.output or b""
                    if isinstance(full, str):
                        full = full.encode("utf-8", "replace")
                except Exception:
                    try:
                        full = await asyncio.to_thread(container.logs)
                    except Exception:
                        full = b""
                exec_output = full.decode("utf-8", errors="replace")
                new, streamed = unstreamed_tail(full, streamed)
                if new:
                    elastic_log += new.decode("utf-8", errors="replace")
            terminal_output = setup_output + exec_output

            if terminal_output.strip():
                try:
                    with open(
                        os.path.join(workspace_dir, "stdout.txt"),
                        "w",
                        encoding="utf-8",
                    ) as _of:
                        _of.write(terminal_output)
                except Exception:
                    _log.debug("Failed to write stdout.txt", exc_info=True)

            if preempted:
                status_meta = {"status": "preempted", "output": str(elastic_log)}
            elif disrupted:
                status_meta = {
                    "status": "failed",
                    "output": str(elastic_log)
                    + f"[{timestamp()}] [WORKER] Container exited after disruption.\n",
                }
            else:
                status_meta = {
                    "status": "success" if exec_exit_code == 0 else "failed",
                    "output": str(elastic_log)
                    + f"[{timestamp()}] [WORKER] Container exited with code "
                    f"{exec_exit_code}.\n",
                }
            try:
                await asyncio.to_thread(container.stop, timeout=2)
            except Exception:
                _log.debug("Operation failed", exc_info=True)
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                _log.debug("Operation failed", exc_info=True)

        # ------------------------------------------------------------------
        # WASM PATH (early-exit if wasmtime missing)
        # ------------------------------------------------------------------
        elif runtime == "wasm":
            if shutil.which("wasmtime") is None:
                status_meta = {
                    "status": "fatal_error",
                    "output": str(elastic_log)
                    + f"\n[{timestamp()}] [WORKER] WASM runtime unavailable: "
                    "'wasmtime' not found in PATH.\n"
                    + f"[{timestamp()}] [HINT] Install wasmtime "
                    "(https://wasmtime.dev) or use a different runtime.",
                }
                await unregister_running_container(task_id)
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return status_meta, archive

        # ------------------------------------------------------------------
        # UNKNOWN RUNTIME → fatal
        # ------------------------------------------------------------------
        elif runtime != "native":
            status_meta = {
                "status": "fatal_error",
                "output": str(elastic_log)
                + f"\n[{timestamp()}] [WORKER] Unknown runtime '{runtime}'. "
                "Supported: docker, native, wasm.",
            }
            await unregister_running_container(task_id)
            archive = await asyncio.to_thread(
                shutil.make_archive,
                base_name=workspace_dir + "_out",
                format="zip",
                root_dir=workspace_dir,
            )
            return status_meta, archive

        # ------------------------------------------------------------------
        # NATIVE / WASM shared execution path
        # ------------------------------------------------------------------
        if runtime in ("wasm", "native"):
            if runtime == "native" and not LOCAL_SETTINGS.get(
                "native_runtime_enabled", False
            ):
                status_meta = {
                    "status": "fatal_error",
                    "output": str(elastic_log)
                    + f"\n[{timestamp()}] [SECURITY] Native runtime is disabled "
                    "in node settings. Enable 'native_runtime_enabled' to allow.",
                }
                await unregister_running_container(task_id)
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return status_meta, archive

            safe_entrypoint_n = prepare_multiline_command(entrypoint)
            safe_setup_n = prepare_multiline_command(setup_cmd)
            if runtime == "native":
                elastic_log += (
                    f"[{timestamp()}] [NATIVE] ⚠ Executing sandboxed OS subprocess "
                    "on host — no container isolation.\n"
                )
                elastic_log += (
                    f"[{timestamp()}] [NATIVE] ⚠ Task code runs under this OS user; "
                    f"file system access is limited to {workspace_dir} but NOT "
                    "enforced by the kernel.\n"
                )
                elastic_log += (
                    f"[{timestamp()}] [NATIVE] ⚠ Network egress is host-level; "
                    "firewall rules, if any, are the only boundary.\n"
                )
                elastic_log += (
                    f"[{timestamp()}] [NATIVE] ⚠ Python venvs are cached and reused "
                    "across tasks — a malicious dep can persist between runs.\n"
                )
                elastic_log += (
                    f"[{timestamp()}] [SECURITY] Profile: "
                    f"{security_profile.upper()} | Env sanitized, process tree managed\n"
                )
            elif runtime == "wasm":
                elastic_log += (
                    f"[{timestamp()}] [WASM] Running task in WASM sandbox "
                    f"({workspace_dir}).\n"
                )

            # --- Sandboxed environment ---
            _WIN_ENV_ALLOW = {"SYSTEMROOT", "COMSPEC", "PATHEXT"}
            _NIX_ENV_ALLOW = {"LANG", "TERM"}
            _allow_keys = (
                _WIN_ENV_ALLOW if sys.platform == "win32" else _NIX_ENV_ALLOW
            )
            safe_env: dict[str, str] = {}
            for key in _allow_keys:
                if key in os.environ:
                    safe_env[key] = os.environ[key]
            if sys.platform == "win32":
                safe_env["PATH"] = os.path.join(
                    os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32"
                )
            else:
                safe_env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
            _tmp_dir = os.path.join(workspace_dir, "_nexus_tmp")
            os.makedirs(_tmp_dir, exist_ok=True)
            safe_env["HOME"] = workspace_dir
            safe_env["USERPROFILE"] = workspace_dir
            safe_env["TMPDIR"] = _tmp_dir
            safe_env["TEMP"] = _tmp_dir
            safe_env["TMP"] = _tmp_dir
            safe_env["PYTHONIOENCODING"] = "utf-8"
            safe_env.update({str(k): str(v) for k, v in merged_env.items()})

            # Setup command gets broader env (needs pip/npm/etc. in PATH)
            setup_env = dict(safe_env)
            if sys.platform == "win32":
                setup_env["PATH"] = os.environ.get("PATH", safe_env["PATH"])
            else:
                setup_env["PATH"] = os.environ.get(
                    "PATH", "/usr/local/bin:/usr/bin:/bin"
                )
            _pip_cache = str(pip_wheel_cache_dir())
            setup_env["PIP_CACHE_DIR"] = _pip_cache
            safe_env["PIP_CACHE_DIR"] = _pip_cache

            # --- VENV ISOLATION (native only) ---
            if runtime == "native":
                await update_local_task_stage(task_id, "venv_setup")
            _venv_dir = os.path.join(workspace_dir, "_nexus_venv")
            _venv_created = False
            _venv_cache_hit = False
            # Per-task requests, worker-sovereign: a task may DEMAND venv
            # isolation (stricter than the node default is always honored)
            # or opt OUT of the venv cache (a fresh env for itself only).
            # It can never relax the worker's own settings.
            _require_venv = bool(
                LOCAL_SETTINGS.get("require_venv_isolation", False)
                or m.get("require_venv_isolation", False)
            )
            _cache_venvs = bool(
                LOCAL_SETTINGS.get("cache_venvs", False)
                and not m.get("no_venv_cache", False)
            )
            if sys.platform == "win32":
                _venv_bin = os.path.join(_venv_dir, "Scripts")
                _venv_pip = os.path.join(_venv_bin, "pip.exe")
            else:
                _venv_bin = os.path.join(_venv_dir, "bin")
                _venv_pip = os.path.join(_venv_bin, "pip")

            _req_path_pre = os.path.join(workspace_dir, "requirements.txt")
            _venv_cache_entry = None
            if (
                runtime == "native"
                and _cache_venvs
                and os.path.isfile(_req_path_pre)
            ):
                try:
                    with open(
                        _req_path_pre, "r", encoding="utf-8", errors="replace"
                    ) as _rf:
                        _req_text = _rf.read()
                    _cache_key = venv_cache_key(_req_text)
                    _venv_cache_entry = os.path.join(
                        str(venv_cache_root()), _cache_key
                    )
                    if os.path.isdir(_venv_cache_entry) and os.path.isfile(
                        os.path.join(_venv_cache_entry, "pyvenv.cfg")
                    ):
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Venv cache HIT "
                            f"({_cache_key}) — copying pre-built environment...\n"
                        )
                        await asyncio.to_thread(
                            shutil.copytree,
                            _venv_cache_entry,
                            _venv_dir,
                            symlinks=False,
                            dirs_exist_ok=False,
                        )
                        _venv_created = True
                        _venv_cache_hit = True
                        safe_env["PATH"] = (
                            _venv_bin + os.pathsep + safe_env.get("PATH", "")
                        )
                        setup_env["PATH"] = (
                            _venv_bin + os.pathsep + setup_env.get("PATH", "")
                        )
                        safe_env["VIRTUAL_ENV"] = _venv_dir
                        elastic_log += (
                            f"[{timestamp()}] [SECURITY] Task will execute "
                            "inside cached isolated venv.\n"
                        )
                except Exception as _ce:
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Venv cache lookup failed: {_ce}. "
                        "Falling back to fresh creation.\n"
                    )
                    _venv_cache_hit = False

            _venv_python: str | None = None
            if runtime == "native" and not _venv_cache_hit:
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Creating isolated venv for "
                    "native execution...\n"
                )
                if getattr(sys, "frozen", False):
                    for _cand in ("python", "python3", "py"):
                        _found = shutil.which(_cand)
                        if _found:
                            _venv_python = _found
                            break
                else:
                    _venv_python = sys.executable
                if not _venv_python:
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] No system Python interpreter "
                        "found on PATH — skipping venv creation.\n"
                    )
                    if _require_venv:
                        elastic_log += (
                            f"[{timestamp()}] [SECURITY] Venv isolation required "
                            "but no Python available — task blocked.\n"
                        )
                        status_meta = {
                            "status": "failed",
                            "output": mask_ips_in_log(str(elastic_log)),
                        }
                        await unregister_running_container(task_id)
                        archive = await asyncio.to_thread(
                            shutil.make_archive,
                            base_name=workspace_dir + "_out",
                            format="zip",
                            root_dir=workspace_dir,
                        )
                        return status_meta, archive
                    _venv_python = None

            if runtime == "native" and not _venv_cache_hit and _venv_python:
                _venv_creation_env = dict(os.environ)
                _venv_creation_env["PYTHONIOENCODING"] = "utf-8"
                _venv_creation_env["PIP_CACHE_DIR"] = _pip_cache
                _uv_bin = detect_uv()
                if _uv_bin:
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Using uv for venv creation "
                        "(hardlinked global store active).\n"
                    )
                    venv_proc = await asyncio.create_subprocess_exec(
                        _uv_bin, "venv", "--python", _venv_python, _venv_dir,
                        cwd=workspace_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=_venv_creation_env,
                    )
                else:
                    venv_proc = await asyncio.create_subprocess_exec(
                        _venv_python, "-m", "venv", _venv_dir,
                        cwd=workspace_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=_venv_creation_env,
                    )
                venv_out, venv_err = await venv_proc.communicate()
                if venv_proc.returncode == 0:
                    _venv_created = True
                    _pip_ok = True
                    _req_path = os.path.join(workspace_dir, "requirements.txt")
                    if os.path.isfile(_req_path):
                        await update_local_task_stage(task_id, "installing_deps")
                        _venv_py_exe = os.path.join(
                            _venv_bin,
                            "python" + (".exe" if sys.platform == "win32" else ""),
                        )
                        if _uv_bin:
                            pip_cmd = [
                                _uv_bin, "pip", "install",
                                "--python", _venv_py_exe,
                                "-r", _req_path,
                            ]
                        else:
                            pip_cmd = [_venv_pip, "install", "-r", _req_path]
                        pip_proc = await asyncio.create_subprocess_exec(
                            *pip_cmd,
                            cwd=workspace_dir,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=setup_env,
                        )
                        pip_out, pip_err = await pip_proc.communicate()
                        if pip_proc.returncode == 0:
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] requirements.txt "
                                "installed in venv successfully"
                                f"{' (via uv)' if _uv_bin else ''}.\n"
                            )
                        else:
                            _pip_ok = False
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] Warning: pip install "
                                f"failed (exit {pip_proc.returncode}).\n"
                            )
                            elastic_log += pip_err.decode("utf-8", errors="replace")
                    if (
                        _cache_venvs
                        and _pip_ok
                        and _venv_cache_entry
                        and not os.path.isdir(_venv_cache_entry)
                    ):
                        try:
                            await asyncio.to_thread(
                                shutil.copytree,
                                _venv_dir,
                                _venv_cache_entry,
                                symlinks=False,
                                dirs_exist_ok=False,
                            )
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] Seeded venv cache "
                                f"entry {os.path.basename(_venv_cache_entry)}.\n"
                            )
                        except FileExistsError:
                            pass
                        except Exception as _se:
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] Cache seed skipped: "
                                f"{_se}.\n"
                            )
                    safe_env["PATH"] = (
                        _venv_bin + os.pathsep + safe_env.get("PATH", "")
                    )
                    setup_env["PATH"] = (
                        _venv_bin + os.pathsep + setup_env.get("PATH", "")
                    )
                    safe_env["VIRTUAL_ENV"] = _venv_dir
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Task will execute inside "
                        "isolated venv.\n"
                    )
                else:
                    _venv_err_msg = venv_err.decode("utf-8", errors="replace").strip()
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Venv creation failed "
                        f"(exit {venv_proc.returncode}).\n"
                    )
                    if _venv_err_msg:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] venv stderr: {_venv_err_msg}\n"
                        )
                    if _require_venv:
                        elastic_log += (
                            f"[{timestamp()}] [SECURITY] Venv isolation required "
                            "but failed — task blocked.\n"
                        )
                        status_meta = {
                            "status": "failed",
                            "output": mask_ips_in_log(str(elastic_log)),
                        }
                        await unregister_running_container(task_id)
                        archive = await asyncio.to_thread(
                            shutil.make_archive,
                            base_name=workspace_dir + "_out",
                            format="zip",
                            root_dir=workspace_dir,
                        )
                        return status_meta, archive
                    else:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Proceeding without venv "
                            "isolation.\n"
                        )

            # --- node_modules cache + auto-install ---
            _pkg_json_path = os.path.join(workspace_dir, "package.json")
            _node_modules_path = os.path.join(workspace_dir, "node_modules")
            if (
                runtime == "native"
                and os.path.isfile(_pkg_json_path)
                and not os.path.isdir(_node_modules_path)
                and (not safe_setup_n or "npm install" not in safe_setup_n)
            ):
                _node_cache_entry = None
                _node_cache_hit = False
                if _cache_venvs:
                    try:
                        _lock_path = os.path.join(
                            workspace_dir, "package-lock.json"
                        )
                        if os.path.isfile(_lock_path):
                            with open(
                                _lock_path,
                                "r",
                                encoding="utf-8",
                                errors="replace",
                            ) as _lf:
                                _hash_src = _lf.read()
                        else:
                            with open(
                                _pkg_json_path,
                                "r",
                                encoding="utf-8",
                                errors="replace",
                            ) as _lf:
                                _hash_src = _lf.read()
                        _node_key = node_cache_key(_hash_src)
                        _node_cache_entry = os.path.join(
                            str(node_cache_root()), _node_key, "node_modules"
                        )
                        if os.path.isdir(_node_cache_entry):
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] node_modules cache "
                                f"HIT ({_node_key}) — copying...\n"
                            )
                            await asyncio.to_thread(
                                shutil.copytree,
                                _node_cache_entry,
                                _node_modules_path,
                                symlinks=False,
                                dirs_exist_ok=False,
                            )
                            _node_cache_hit = True
                    except Exception as _nce:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] node_modules cache lookup "
                            f"failed: {_nce}. Falling back to fresh install.\n"
                        )
                if not _node_cache_hit:
                    _npm_bin = shutil.which("npm")
                    if not _npm_bin:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] npm not on PATH — "
                            "skipping node_modules install.\n"
                        )
                    else:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Running npm install "
                            "for detected package.json...\n"
                        )
                        _npm_cmd = (
                            [_npm_bin, "ci"]
                            if os.path.isfile(
                                os.path.join(workspace_dir, "package-lock.json")
                            )
                            else [_npm_bin, "install"]
                        )
                        npm_proc = await asyncio.create_subprocess_exec(
                            *_npm_cmd,
                            cwd=workspace_dir,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            env=setup_env,
                        )
                        _nout, _nerr = await npm_proc.communicate()
                        if npm_proc.returncode == 0:
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] npm install completed.\n"
                            )
                            if (
                                _cache_venvs
                                and _node_cache_entry
                                and not os.path.isdir(_node_cache_entry)
                            ):
                                try:
                                    os.makedirs(
                                        os.path.dirname(_node_cache_entry),
                                        exist_ok=True,
                                    )
                                    await asyncio.to_thread(
                                        shutil.copytree,
                                        _node_modules_path,
                                        _node_cache_entry,
                                        symlinks=False,
                                        dirs_exist_ok=False,
                                    )
                                    elastic_log += (
                                        f"[{timestamp()}] [WORKER] Seeded "
                                        "node_modules cache.\n"
                                    )
                                except FileExistsError:
                                    pass
                                except Exception as _nse:
                                    elastic_log += (
                                        f"[{timestamp()}] [WORKER] node cache "
                                        f"seed skipped: {_nse}.\n"
                                    )
                        else:
                            elastic_log += (
                                f"[{timestamp()}] [WORKER] npm install failed "
                                f"(exit {npm_proc.returncode}).\n"
                            )
                            elastic_log += _nerr.decode("utf-8", errors="replace")

            # Run setup command (native-side, broader PATH for installs)
            if safe_setup_n:
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Running setup: {safe_setup_n}\n"
                )
                setup_shell = (
                    ("cmd", "/c") if sys.platform == "win32" else ("sh", "-c")
                )
                setup_proc = await asyncio.create_subprocess_exec(
                    *setup_shell, safe_setup_n,
                    cwd=workspace_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=setup_env,
                )
                setup_out, setup_err = await setup_proc.communicate()
                if setup_proc.returncode != 0:
                    status_meta = {
                        "status": "failed",
                        "output": str(elastic_log)
                        + f"[{timestamp()}] [WORKER] Setup command failed "
                        f"(exit {setup_proc.returncode}).\n---\n"
                        + setup_out.decode("utf-8", errors="replace")
                        + setup_err.decode("utf-8", errors="replace"),
                    }
                    await unregister_running_container(task_id)
                    archive = await asyncio.to_thread(
                        shutil.make_archive,
                        base_name=workspace_dir + "_out",
                        format="zip",
                        root_dir=workspace_dir,
                    )
                    return status_meta, archive

            # Build command list (respect `&&` chains, wasmtime invocation)
            _is_chained = "&&" in safe_entrypoint_n
            if runtime == "wasm":
                if _is_chained:
                    if sys.platform == "win32":
                        cmd_parts = ["cmd", "/c", safe_entrypoint_n]
                    else:
                        cmd_parts = ["sh", "-c", safe_entrypoint_n]
                else:
                    cmd_parts = ["wasmtime"] + shlex.split(safe_entrypoint_n)
            elif _is_chained:
                if sys.platform == "win32":
                    cmd_parts = ["cmd", "/c", safe_entrypoint_n]
                else:
                    cmd_parts = ["sh", "-c", safe_entrypoint_n]
            else:
                cmd_parts = shlex.split(safe_entrypoint_n)

            # --- VENV EXECUTABLE RESOLUTION ---
            if (
                _venv_created
                and cmd_parts
                and not _is_chained
                and runtime == "native"
            ):
                _interp_map = {"python", "python3", "py", "pip", "pip3"}
                _head = os.path.basename(cmd_parts[0]).lower()
                if _head.endswith(".exe"):
                    _head = _head[:-4]
                if _head in _interp_map:
                    _exe_name = _head + (
                        ".exe" if sys.platform == "win32" else ""
                    )
                    _venv_exe = os.path.join(_venv_bin, _exe_name)
                    if _head == "py" and not os.path.isfile(_venv_exe):
                        _venv_exe = os.path.join(
                            _venv_bin,
                            "python"
                            + (".exe" if sys.platform == "win32" else ""),
                        )
                    if os.path.isfile(_venv_exe):
                        cmd_parts[0] = _venv_exe
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Rewrote entrypoint "
                            f"interpreter to venv: {_venv_exe}\n"
                        )
                    else:
                        elastic_log += (
                            f"[{timestamp()}] [WORKER] Warning: venv executable "
                            f"not found at {_venv_exe} — task may not see "
                            "installed packages.\n"
                        )
            elif _venv_created and _is_chained and runtime == "native":
                elastic_log += (
                    f"[{timestamp()}] [WORKER] Chained entrypoint — relying on "
                    "PATH-prepended venv for interpreter resolution.\n"
                )

            # --- Network isolation (Linux best-effort) ---
            if (
                not network_required
                and sys.platform != "win32"
                and runtime == "native"
            ):
                if shutil.which("unshare"):
                    cmd_parts = ["unshare", "--net", "--"] + cmd_parts
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Network isolated via "
                        "unshare --net.\n"
                    )
                else:
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Warning: 'unshare' not "
                        "available — network unrestricted for native task.\n"
                    )
            elif (
                sys.platform == "win32"
                and runtime == "native"
                and not network_required
            ):
                elastic_log += (
                    f"[{timestamp()}] [SECURITY] Note: Network isolation not "
                    "available on Windows for native tasks.\n"
                )

            # --- Resource limits (Linux preexec_fn) ---
            _rlimit_fn = (
                make_resource_limits(safe_ram)
                if security_profile != "relaxed"
                else None
            )

            # --- bwrap wrapping (Linux, profile-aware) ---
            try:
                cmd_parts, _sandbox_log = wrap_command_with_sandbox(
                    cmd_parts,
                    workspace_dir=workspace_dir,
                    profile=security_profile,
                    extra_env_passthrough=("PATH", "HOME", "LANG"),
                )
            except SandboxUnavailable as exc:
                elastic_log += (
                    f"[{timestamp()}] [SECURITY] Native sandbox required but "
                    f"unavailable: {exc}\n"
                )
                await unregister_running_container(task_id)
                archive = await asyncio.to_thread(
                    shutil.make_archive,
                    base_name=workspace_dir + "_out",
                    format="zip",
                    root_dir=workspace_dir,
                )
                return (
                    {
                        "status": "failed",
                        "output": mask_ips_in_log(str(elastic_log)),
                    },
                    archive,
                )
            elastic_log += f"[{timestamp()}] {_sandbox_log}\n"

            await update_local_task_stage(task_id, "executing")
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                cwd=workspace_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=safe_env,
                start_new_session=(sys.platform != "win32"),
                preexec_fn=_rlimit_fn,
            )
            await register_running_proc(task_id, proc)
            if sys.platform == "win32" and security_profile != "relaxed":
                if assign_to_job_object(proc.pid, ram_limit_mb=safe_ram):
                    elastic_log += (
                        f"[{timestamp()}] [SECURITY] Native task assigned to "
                        "Windows Job Object (kill-on-job-close).\n"
                    )
            preempted, disrupted = False, False
            _collected_stdout: list[str] = []

            async def _stream_reader():
                nonlocal elastic_log
                assert proc.stdout is not None
                while True:
                    try:
                        line = await proc.stdout.readline()
                    except Exception:
                        break
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    _collected_stdout.append(decoded)
                    elastic_log += decoded

            reader_task = asyncio.create_task(_stream_reader())

            while proc.returncode is None:
                if await is_task_interrupted(task_id):
                    disrupted = True
                    try:
                        await kill_process_tree(proc)
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                    elastic_log += (
                        f"[{timestamp()}] [WORKER] Process disrupted by master request.\n"
                    )
                    break
                if await is_task_preempted(task_id):
                    preempted = True
                    try:
                        await kill_process_tree(proc)
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                    elastic_log += (
                        f"\n[{timestamp()}] [WORKER] Process preempted locally "
                        "to save checkpoint."
                    )
                    break

                curr_free = psutil.virtual_memory().available // (1024 * 1024)
                if LOCAL_SETTINGS["mode"] == "user" and curr_free < 500:
                    preempted = True
                    try:
                        await kill_process_tree(proc)
                    except Exception:
                        _log.debug("Operation failed", exc_info=True)
                    elastic_log += (
                        f"\n[{timestamp()}] [SYSTEM] Host RAM critical. "
                        "Task preempted to save OS."
                    )
                    break

                try:
                    await update_local_task_children(
                        task_id, snapshot_proc_children(proc)
                    )
                except Exception:
                    _log.debug("child snapshot failed", exc_info=True)

                await asyncio.sleep(2)

            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.wait_for(reader_task, timeout=2)
            except (asyncio.TimeoutError, Exception):
                reader_task.cancel()
            await update_local_task_stage(task_id, "finalizing")
            terminal_output = "".join(_collected_stdout)

            if terminal_output.strip():
                try:
                    with open(
                        os.path.join(workspace_dir, "stdout.txt"),
                        "w",
                        encoding="utf-8",
                    ) as _of:
                        _of.write(terminal_output)
                except Exception:
                    _log.debug("Failed to write stdout.txt", exc_info=True)

            if preempted:
                status_meta = {
                    "status": "preempted",
                    "output": str(elastic_log),
                }
            elif disrupted:
                status_meta = {
                    "status": "failed",
                    "output": str(elastic_log)
                    + f"[{timestamp()}] [WORKER] Process disrupted.\n",
                }
            else:
                status_meta = {
                    "status": "success" if proc.returncode == 0 else "failed",
                    "output": str(elastic_log)
                    + f"[{timestamp()}] [WORKER] Process exited with code "
                    f"{proc.returncode}.\n",
                }

    except Exception as e:
        status_meta = {
            "status": "fatal_error",
            "output": str(elastic_log) + "\n" + str(e),
        }

    # --- Defensive native-proc cleanup ---
    try:
        _lingering_proc = None
        async with STATE.running_container_lock:
            _lingering_proc = STATE.running_task_procs.get(task_id)
        if _lingering_proc is not None:
            try:
                if getattr(_lingering_proc, "returncode", None) is None:
                    await kill_process_tree(_lingering_proc)
                else:
                    try:
                        _leftover = psutil.Process(_lingering_proc.pid).children(
                            recursive=True
                        )
                        for _c in _leftover:
                            try:
                                _c.kill()
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                    except (psutil.NoSuchProcess, ProcessLookupError):
                        pass
            except Exception:
                _log.debug("Defensive native proc sweep failed", exc_info=True)
        await unregister_running_proc(task_id)
        try:
            release_job_object(proc.pid)
        except Exception:
            pass
    except Exception:
        _log.debug("Native proc cleanup failed", exc_info=True)

    # --- Temp venv cleanup ---
    _venv_cleanup_dir = os.path.join(workspace_dir, "_nexus_venv")
    if os.path.isdir(_venv_cleanup_dir):
        try:
            await asyncio.to_thread(
                shutil.rmtree, _venv_cleanup_dir, ignore_errors=True
            )
            elastic_log += f"[{timestamp()}] [WORKER] Temp venv cleaned up.\n"
        except Exception:
            _log.debug("Failed to clean up venv", exc_info=True)

    await unregister_running_container(task_id)
    archive_path = await asyncio.to_thread(
        shutil.make_archive,
        base_name=workspace_dir + "_out",
        format="zip",
        root_dir=workspace_dir,
    )
    if "output" in status_meta:
        status_meta["output"] = mask_ips_in_log(status_meta["output"])
    return status_meta, archive_path


__all__ = ["execute_bundle_with_watchdog"]
