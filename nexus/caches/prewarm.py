"""Background venv prewarm worker.

Ported from Phase-1/node_modified.py:

* ``PREWARM_JOBS`` dict — line 7854
* ``_prewarm_job_set`` / ``_prewarm_job_append`` — lines 7857-7865
* ``_prewarm_run`` — lines 7868-7951

A prewarm job builds a venv for a ``requirements.txt`` string and copies
the result into the shared venv cache so later task dispatches with the
same hash can reuse it. Jobs run as ``asyncio.create_task`` background
coroutines; the live log is streamed into ``PREWARM_JOBS[job_id]["log"]``
for the UI to poll.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from nexus.caches.paths import (
    detect_uv,
    pip_wheel_cache_dir,
    venv_cache_key,
    venv_cache_root,
)
from nexus.utils import timestamp


# Process-local registry of background prewarm jobs.
# Shape: { job_id: {"log": str, "status": str, "started_at": float,
#                   "key": str | None} }
PREWARM_JOBS: dict[str, dict[str, Any]] = {}


def job_set(job_id: str, **updates: Any) -> None:
    """Record or update a prewarm job entry."""
    job = PREWARM_JOBS.setdefault(
        job_id,
        {"log": "", "status": "starting", "started_at": time.time()},
    )
    job.update(updates)


def job_append(job_id: str, text: str) -> None:
    """Append *text* to the live log (capped at ~20KB tail)."""
    job = PREWARM_JOBS.get(job_id)
    if job is not None:
        job["log"] = (job["log"] + text)[-20000:]


async def run_prewarm(job_id: str, req_text: str) -> None:
    """Build a venv for *req_text* and copy it into the shared cache."""
    job_set(job_id, status="running")
    cache_key = venv_cache_key(req_text)
    cache_entry = os.path.join(str(venv_cache_root()), cache_key)
    if os.path.isdir(cache_entry) and os.path.isfile(
        os.path.join(cache_entry, "pyvenv.cfg")
    ):
        job_append(
            job_id,
            f"[{timestamp()}] Cache entry {cache_key} already exists — nothing to do.\n",
        )
        job_set(job_id, status="done", key=cache_key)
        return

    # Pick a Python interpreter. When frozen, we have no ``sys.executable``
    # to speak of, so fall back to whatever is on PATH.
    venv_python: str | None = None
    if getattr(sys, "frozen", False):
        for cand in ("python", "python3", "py"):
            found = shutil.which(cand)
            if found:
                venv_python = found
                break
    else:
        venv_python = sys.executable
    if not venv_python:
        job_append(job_id, "No system Python interpreter on PATH.\n")
        job_set(job_id, status="failed")
        return

    uv_bin = detect_uv()
    pip_cache = str(pip_wheel_cache_dir())

    with tempfile.TemporaryDirectory() as tmp:
        staging = os.path.join(tmp, "_nexus_venv")
        req_file = os.path.join(tmp, "requirements.txt")
        with open(req_file, "w", encoding="utf-8") as f:
            f.write(req_text)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PIP_CACHE_DIR"] = pip_cache

        # --- venv creation ----------------------------------------------
        job_append(
            job_id,
            f"[{timestamp()}] Creating venv at {staging}"
            f"{' via uv' if uv_bin else ''}...\n",
        )
        if uv_bin:
            venv_cmd = [uv_bin, "venv", "--python", venv_python, staging]
        else:
            venv_cmd = [venv_python, "-m", "venv", staging]
        proc = await asyncio.create_subprocess_exec(
            *venv_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        async for line in proc.stdout:
            job_append(job_id, line.decode("utf-8", errors="replace"))
        await proc.wait()
        if proc.returncode != 0:
            job_append(job_id, f"venv creation failed (exit {proc.returncode}).\n")
            job_set(job_id, status="failed")
            return

        # --- pip install -------------------------------------------------
        if sys.platform == "win32":
            venv_py_exe = os.path.join(staging, "Scripts", "python.exe")
            pip_path = os.path.join(staging, "Scripts", "pip.exe")
        else:
            venv_py_exe = os.path.join(staging, "bin", "python")
            pip_path = os.path.join(staging, "bin", "pip")

        job_append(
            job_id,
            f"[{timestamp()}] Installing packages"
            f"{' via uv' if uv_bin else ''}...\n",
        )
        if uv_bin:
            install_cmd = [
                uv_bin, "pip", "install", "--python", venv_py_exe, "-r", req_file,
            ]
        else:
            install_cmd = [pip_path, "install", "--progress-bar", "on", "-r", req_file]
        pip_proc = await asyncio.create_subprocess_exec(
            *install_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        assert pip_proc.stdout is not None
        async for line in pip_proc.stdout:
            job_append(job_id, line.decode("utf-8", errors="replace"))
        await pip_proc.wait()
        if pip_proc.returncode != 0:
            job_append(job_id, f"pip install failed (exit {pip_proc.returncode}).\n")
            job_set(job_id, status="failed")
            return

        # --- copy to shared cache ---------------------------------------
        job_append(
            job_id,
            f"[{timestamp()}] Copying to cache entry {cache_key}...\n",
        )
        try:
            await asyncio.to_thread(
                shutil.copytree, staging, cache_entry, symlinks=False, dirs_exist_ok=False
            )
        except FileExistsError:
            pass
        except Exception as se:
            job_append(job_id, f"Cache copy failed: {se}\n")
            job_set(job_id, status="failed")
            return

    job_append(job_id, f"[{timestamp()}] Done. Cache entry: {cache_key}\n")
    job_set(job_id, status="done", key=cache_key)


__all__ = ["PREWARM_JOBS", "job_set", "job_append", "run_prewarm"]
