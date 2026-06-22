"""Code fingerprint for this node's bundled relay implementation.

A NexusGrid group can freeze the fingerprint at first relay bind. Every
subsequent relay registration MUST present a matching fingerprint or be
rejected. That stops a rogue host from substituting different code under
a group's nose while still allowing deliberate group-wide upgrades
through the founder's "freeze new fingerprint" path.

The fingerprint is deterministic:

    sha256(b"nexus-relay-codeprint:" + version + b":" + relay_module_bytes)[:32]

Two nodes that build from the same NexusGrid source produce the same
fingerprint even on different machines and different build times. The
prefix is a domain separator so a future feature that hashes other artifacts
can't collide.
"""

from __future__ import annotations

import hashlib
import os
import sys

from nexus import __version__ as _NEXUS_VERSION

_FINGERPRINT_LEN = 32  # hex chars; 16 bytes of entropy


def _resolve_relay_module_path() -> str:
    """Return the absolute path to the bundled relay source (``nexus/relay/server.py``).

    Looks first inside the PyInstaller bundle (``sys._MEIPASS/nexus/relay``) so a
    frozen build hashes the file that was bundled at PyInstaller time,
    then walks up from this module's directory as a source-tree fallback
    for ``python -m nexus``.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = []
    if meipass:
        candidates.append(os.path.join(meipass, "nexus", "relay", "server.py"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(
        os.path.normpath(os.path.join(here, "..", "relay", "server.py"))
    )
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


def fingerprint_for_bytes(body: bytes, version: str = _NEXUS_VERSION) -> str:
    """Return the 32-char hex digest for relay module *body* bytes.

    The deterministic core shared by :func:`fingerprint_for_path` and the
    relay-code-copy path, which fingerprints source it received over
    the wire (channel copy / live-host pull) without first writing it to disk.
    """
    digest = hashlib.sha256(
        b"nexus-relay-codeprint:"
        + version.encode("utf-8")
        + b":"
        + body
    ).hexdigest()
    return digest[:_FINGERPRINT_LEN]


def fingerprint_for_path(path: str, version: str = _NEXUS_VERSION) -> str:
    """Return the 32-char hex digest for the relay module at *path*.

    Empty string if *path* is missing/unreadable — callers treat that as
    "fingerprinting unavailable, skip the check". Used for both the bundled
    relay and pluggable ``nexus_relays/*.py`` modules so a group's
    freeze/propose/accept governance can distinguish a custom relay's code.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError:
        return ""
    return fingerprint_for_bytes(body, version)


def compute_fingerprint(version: str = _NEXUS_VERSION) -> str:
    """Return the 32-char hex digest for the bundled relay code."""
    return fingerprint_for_path(_resolve_relay_module_path(), version)


# Computed once at import time; same value for the life of the process.
# Recompute lazily via :func:`compute_fingerprint` if a test needs to
# verify against an alternate version string.
CURRENT_FINGERPRINT = compute_fingerprint()


__all__ = [
    "CURRENT_FINGERPRINT",
    "compute_fingerprint",
    "fingerprint_for_bytes",
    "fingerprint_for_path",
]
