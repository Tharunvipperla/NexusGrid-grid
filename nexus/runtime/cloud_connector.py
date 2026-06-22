"""A2 — cloud connector: pull cloud objects in / push results out.

A **cloud URI** is one of two forms, so no provider is hardcoded:

* ``http://…`` / ``https://…`` — fetched directly (public links, presigned
  S3 URLs, Drive direct-download links, …).
* ``remote:path`` — handed to ``rclone``. The user configures any of rclone's
  70+ backends once (``rclone config``); that config *is* the credential store,
  so the connector never has to know about Drive/S3/Dropbox specifics.

Two verbs, both returning ``(ok, reason)``:

* :func:`download` — fetch a cloud object to a local path.
* :func:`upload` — push a local path to a cloud object.

This is the shared layer A2 reuses across task inputs, foreign-storage sources,
and DAG step IO. The first call site is task inputs (run-spec ``inputs``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
from urllib.parse import urljoin, urlparse

_log = logging.getLogger("nexus.runtime.cloud_connector")

DOWNLOAD_TIMEOUT_S = 600
_MAX_REDIRECTS = 6


def _host_blocked(host: str) -> bool:
    """SSRF guard (F-012): block fetches whose host resolves to a private /
    loopback / link-local (e.g. the 169.254.169.254 cloud-metadata endpoint) /
    reserved / multicast address. Task-input URLs are attacker-controlled and
    run on the worker, so an unrestricted fetch would expose the worker's cloud
    IAM credentials and internal services. Unresolvable hosts are blocked."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False
# An rclone remote ref: a backend name (>=2 chars, alnum/_/-) then ':'. The
# length>=2 rule keeps a Windows drive letter ("C:\…") from looking like a
# remote.
_RCLONE_RE = re.compile(r"^[A-Za-z0-9_-]{2,}:")


def classify(uri: str) -> str:
    """Return ``"http"``, ``"rclone"``, or ``""`` (not a cloud URI)."""
    u = (uri or "").strip()
    low = u.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return "http"
    if "://" not in u and _RCLONE_RE.match(u):
        return "rclone"
    return ""


def rclone_available() -> bool:
    return shutil.which("rclone") is not None


async def _run(argv: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return 127, str(exc)
    _, err = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT_S)
    rc = proc.returncode if proc.returncode is not None else 1
    return rc, (err or b"").decode("utf-8", "replace")[:300]


async def download(uri: str, dest: str) -> tuple[bool, str]:
    """Fetch *uri* to local *dest*. Returns ``(ok, reason)``."""
    kind = classify(uri)
    if not kind:
        return False, "bad_uri"
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)

    if kind == "http":
        import httpx

        # Follow redirects manually so EVERY hop's host is SSRF-checked — a
        # public URL that 302s to 169.254.169.254 must not slip through.
        try:
            current = uri
            async with httpx.AsyncClient(follow_redirects=False) as client:
                for _ in range(_MAX_REDIRECTS):
                    if _host_blocked(urlparse(current).hostname or ""):
                        return False, "blocked_host"
                    async with client.stream(
                        "GET", current, timeout=DOWNLOAD_TIMEOUT_S
                    ) as res:
                        if res.is_redirect:
                            current = urljoin(current, res.headers.get("location", ""))
                            continue
                        res.raise_for_status()
                        with open(dest, "wb") as f:
                            async for chunk in res.aiter_bytes():
                                f.write(chunk)
                        return True, ""
            return False, "too_many_redirects"
        except Exception as exc:
            _log.debug("http download failed: %s", uri, exc_info=True)
            return False, f"http:{type(exc).__name__}"

    # rclone remote:path
    if not rclone_available():
        return False, "rclone_unavailable"
    rc, err = await _run(["rclone", "copyto", uri, dest])
    if rc == 0:
        return True, ""
    _log.debug("rclone download rc=%s: %s", rc, err)
    return False, f"rclone_rc_{rc}"


async def upload(src: str, uri: str) -> tuple[bool, str]:
    """Push local *src* to cloud *uri*. HTTP(S) targets are not writable;
    use an ``rclone`` remote for uploads. Returns ``(ok, reason)``."""
    if not os.path.isfile(src):
        return False, "src_missing"
    kind = classify(uri)
    if kind == "http":
        return False, "http_upload_unsupported"
    if kind != "rclone":
        return False, "bad_uri"
    if not rclone_available():
        return False, "rclone_unavailable"
    rc, err = await _run(["rclone", "copyto", src, uri])
    if rc == 0:
        return True, ""
    _log.debug("rclone upload rc=%s: %s", rc, err)
    return False, f"rclone_rc_{rc}"


__all__ = ["classify", "rclone_available", "download", "upload"]
