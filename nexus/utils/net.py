"""Local-host network helpers.

Extracted from Phase-1/node_modified.py (lines 673–692, 749–768).
"""

from __future__ import annotations

import ipaddress
import os
import socket


def get_local_ip() -> str:
    """Return the best-guess local LAN IP.

    Uses a UDP connect-trick: connecting a UDP socket to an outside address
    doesn't actually send traffic but does make the OS pick the outgoing
    interface, whose IP we can read via ``getsockname``.

    Falls back to ``127.0.0.1`` if no interface is usable.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Accepts ``1``/``true``/``yes``/``on`` as true, anything else as false.
    Unset (or blank) returns ``default``.
    """
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def is_private_or_loopback_host(host: str) -> bool:
    """Return ``True`` if *host* resolves to an RFC1918 / loopback address.

    The string ``localhost`` counts as loopback. Zone suffixes (``fe80::1%eth0``)
    are stripped before parsing.
    """
    host = str(host or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private)


def client_host(scope_obj) -> str:
    """Extract the client IP from a Starlette/FastAPI scope/request object.

    Returns the lowercase host string, or ``""`` if unknown.
    """
    client = getattr(scope_obj, "client", None)
    return str(getattr(client, "host", "") or "").strip().lower()
