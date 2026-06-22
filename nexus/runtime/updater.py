"""Central, signed auto-update.

Flow:
  * ``check()`` fetches the manifest from ``NEXUS_UPDATE_MANIFEST_URL``, verifies
    the root → cert → release-key chain, and compares versions.
  * ``apply()`` re-verifies, downloads the new exe, checks its sha256 against the
    *signed* manifest, drops it next to the running exe, spawns a tiny detached
    helper that waits for this process to exit, swaps the file (keeping the old
    as a backup for rollback) and relaunches with the same args.

Privacy: the only outbound contact is a GET to the release host you configure —
no telemetry. Security: nothing is trusted unless the manifest verifies against
the baked root key (see :mod:`nexus.security.app_update`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

from nexus import __version__
from nexus.security.app_update import verify_release

_log = logging.getLogger(__name__)

# Default release channel (public; serves manifest.json + the signed exe). The
# "latest" alias always points at the newest release, so this URL is stable across
# versions. ``NEXUS_UPDATE_MANIFEST_URL`` overrides it (handy for testing / forks);
# set that to a path or empty string to point elsewhere or disable.
DEFAULT_MANIFEST_URL = "https://github.com/Tharunvipperla/NexusGrid-releases/releases/latest/download/manifest.json"


def manifest_url() -> str:
    override = os.getenv("NEXUS_UPDATE_MANIFEST_URL")
    return override.strip() if override is not None else DEFAULT_MANIFEST_URL


def _platform_key() -> str:
    """Map the running OS to a manifest ``platforms`` key."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _pick_download(m: dict) -> tuple[str, str] | None:
    """The (url, sha256) for this OS, or None if the release has no binary for it.

    Prefers the signed per-platform map; falls back to the legacy top-level
    url/sha256 (Windows-only manifests). Returns None on other platforms when
    the map lacks an entry — there is nothing safe to install.
    """
    key = _platform_key()
    entry = (m.get("platforms") or {}).get(key)
    if isinstance(entry, dict) and entry.get("url") and entry.get("sha256"):
        return str(entry["url"]), str(entry["sha256"])
    if key == "windows" and m.get("url") and m.get("sha256"):
        return str(m["url"]), str(m["sha256"])
    return None


def _ver_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _read_source(url: str) -> bytes:
    """Read an http(s) URL or a local path / file:// URL into bytes."""
    if url.startswith(("http://", "https://")):
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (operator-configured)
            return r.read()
    path = url[7:] if url.startswith("file://") else url
    with open(path, "rb") as f:
        return f.read()


def _fetch_manifest_sync() -> dict | None:
    url = manifest_url()
    if not url:
        return None
    try:
        data = json.loads(_read_source(url).decode())
    except Exception as exc:  # noqa: BLE001 — any fetch/parse error = no update
        _log.warning("[UPDATE] manifest fetch failed: %s", exc)
        return None
    ok, reason = verify_release(data)
    if not ok:
        _log.warning("[UPDATE] manifest rejected (%s) — ignoring", reason)
        return None
    return data


async def _fetch_manifest() -> dict | None:
    return await asyncio.to_thread(_fetch_manifest_sync)


async def check() -> dict:
    cur = __version__
    m = await _fetch_manifest()
    if not m:
        return {"current": cur, "latest": cur, "available": False, "notes_url": ""}
    latest = str(m.get("version", cur))
    # Only offer the update if it's newer AND this OS actually has a binary.
    available = _ver_tuple(latest) > _ver_tuple(cur) and _pick_download(m) is not None
    return {
        "current": cur,
        "latest": latest,
        "available": available,
        "notes_url": m.get("notes_url", ""),
        # A release can flag itself as potentially destructive (schema/data
        # changes) so the UI can warn the user to back up first. Optional —
        # defaults to a plain update.
        "breaking": bool(m.get("breaking", False)),
        "breaking_note": str(m.get("breaking_note", "")),
    }


def _exe_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


def _spawn_swap(target: str, new_exe: str) -> None:
    """Detached helper: wait for *target* to unlock, back it up, swap, relaunch."""
    exe_dir = os.path.dirname(target)
    args = " ".join(f'"{a}"' if " " in a else a for a in sys.argv[1:])
    if os.name == "nt":
        bat = (
            "@echo off\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            ":retry\r\n"
            f'ren "{target}" "NexusGrid.old.exe" 2>nul\r\n'
            f'if exist "{target}" ( timeout /t 1 /nobreak >nul & goto retry )\r\n'
            f'move /y "{new_exe}" "{target}" >nul\r\n'
            f'start "" "{target}" {args}\r\n'
            'del "%~f0"\r\n'
        )
        bat_path = os.path.join(exe_dir, "_nexus_update.bat")
        with open(bat_path, "w") as f:
            f.write(bat)
        # DETACHED_PROCESS so the helper outlives this exiting process.
        subprocess.Popen(["cmd", "/c", bat_path], cwd=exe_dir, creationflags=0x00000008)
    else:
        sh = (
            "#!/bin/sh\n"
            "sleep 1\n"
            f'while ! mv "{new_exe}" "{target}" 2>/dev/null; do sleep 1; done\n'
            f'chmod +x "{target}"\n'
            f'"{target}" {args} &\n'
        )
        sh_path = os.path.join(exe_dir, "_nexus_update.sh")
        with open(sh_path, "w") as f:
            f.write(sh)
        os.chmod(sh_path, 0o755)
        subprocess.Popen(["/bin/sh", sh_path], cwd=exe_dir, start_new_session=True)


def _die_soon() -> None:
    # Give the HTTP response time to flush, then exit so the helper can swap the
    # locked exe. The OS Job Object cleans up child procs (cloudflared etc.).
    def _exit():
        time.sleep(2)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()


async def apply() -> dict:
    m = await _fetch_manifest()
    if not m:
        raise RuntimeError("no verified update manifest")
    latest = str(m.get("version", ""))
    if _ver_tuple(latest) <= _ver_tuple(__version__):
        raise RuntimeError("already up to date")

    picked = _pick_download(m)
    if picked is None:
        raise RuntimeError(f"no build published for this platform ({_platform_key()})")
    url, want_sha = picked

    data = await asyncio.to_thread(_read_source, url)
    got_sha = hashlib.sha256(data).hexdigest()
    if got_sha != want_sha:
        raise RuntimeError(f"hash mismatch: expected {want_sha[:12]}…, got {got_sha[:12]}…")

    target = _exe_path()
    new_exe = os.path.join(os.path.dirname(target), "NexusGrid.new.exe")
    with open(new_exe, "wb") as f:
        f.write(data)

    _log.info("[UPDATE] verified %s → swapping and relaunching", latest)
    _spawn_swap(target, new_exe)
    _die_soon()
    return {"status": "applying", "version": latest}
