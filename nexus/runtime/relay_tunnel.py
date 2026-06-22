"""Auto-tunnel: make a local relay publicly reachable.

A founder behind NAT can expose their in-process relay to the
public internet with one click — no account, no port-forwarding. We run
a Cloudflare *quick tunnel* (``cloudflared tunnel --url …``): it dials
*out* to Cloudflare's edge and returns a public ``*.trycloudflare.com``
URL that proxies straight to the local relay.

``cloudflared`` is a single external binary, downloaded on demand from
Cloudflare's official GitHub releases over HTTPS and cached beside the
node's other local files. It runs as an ordinary subprocess.

Quick-tunnel URLs are **ephemeral** — they change every run. Fine for
testing and small groups; a stable production relay still wants a real
public host (see ``docs/guides/relay-deploy.md``).
"""

from __future__ import annotations

import atexit
import logging
import platform
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx

from nexus.runtime import child_job

_log = logging.getLogger("nexus.runtime.relay_tunnel")

_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_proc: Optional[subprocess.Popen] = None
_url: str = ""


def _cloudflared_asset() -> str:
    """GitHub release asset name for this OS / arch."""
    system = platform.system()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "Windows":
        return f"cloudflared-windows-{arch}.exe"
    if system == "Darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def _cloudflared_path() -> Path:
    """Local cache path for the cloudflared binary."""
    ext = ".exe" if platform.system() == "Windows" else ""
    return (Path.cwd() / f".nexus_cloudflared{ext}").resolve()


def _ensure_cloudflared() -> Path:
    """Return the path to cloudflared, downloading it on first use."""
    path = _cloudflared_path()
    if path.exists() and path.stat().st_size > 0:
        return path
    asset = _cloudflared_asset()
    url = (
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        + asset
    )
    _log.info("Downloading cloudflared: %s", url)
    tmp = path.with_name(path.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    if asset.endswith(".tgz"):
        import tarfile

        with tarfile.open(tmp) as tar:
            member = next(
                m for m in tar.getmembers() if m.name.endswith("cloudflared")
            )
            src = tar.extractfile(member)
            with open(path, "wb") as dst:
                dst.write(src.read())
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(path)
    if platform.system() != "Windows":
        path.chmod(0o755)
    return path


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def status() -> dict:
    """Return ``{running, public_url, relay_url}``.

    ``relay_url`` is the ``wss://`` form a group binds to.
    """
    running = is_running()
    return {
        "running": running,
        "public_url": _url if running else "",
        "relay_url": (
            _url.replace("https://", "wss://", 1) if running and _url else ""
        ),
    }


def start(local_port: int, *, timeout: float = 45.0) -> dict:
    """Open a quick tunnel to ``localhost:local_port``. Idempotent.

    Blocking — downloads cloudflared on first use, then waits for the
    tunnel URL. Call from a worker thread, not the event loop.
    """
    global _proc, _url
    if is_running():
        return status()

    cf = _ensure_cloudflared()
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    _proc = subprocess.Popen(
        [
            str(cf), "tunnel", "--no-autoupdate",
            "--url", f"http://localhost:{local_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )
    # Tie cloudflared's lifetime to ours so it can never outlive the app,
    # even on a force-kill that skips the graceful shutdown path.
    child_job.bind(_proc)

    found = threading.Event()
    captured = {"url": ""}

    def _drain() -> None:
        # Read + drain stderr so cloudflared's pipe never blocks; grab
        # the trycloudflare URL the first time it appears.
        for line in _proc.stderr:  # type: ignore[union-attr]
            if not captured["url"]:
                match = _TUNNEL_URL_RE.search(line)
                if match:
                    captured["url"] = match.group(0)
                    found.set()
        found.set()  # process ended — unblock any waiter

    threading.Thread(
        target=_drain, name="nexus.relay_tunnel.drain", daemon=True
    ).start()

    if not found.wait(timeout) or not captured["url"]:
        stop()
        raise RuntimeError("cloudflared did not return a tunnel URL in time")

    _url = captured["url"]
    _log.info("Relay tunnel up: %s", _url)
    return status()


def stop() -> dict:
    """Terminate the tunnel subprocess."""
    global _proc, _url
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5.0)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None
    _url = ""
    return status()


# Safety net for graceful interpreter exit (Ctrl+C, normal shutdown) in case
# the app's lifespan teardown didn't run. The kill-job covers force-kill.
atexit.register(stop)


__all__ = ["is_running", "status", "start", "stop"]
