"""Docker security profiles.

Extracted from node_modified.py (lines 1458-1475).

Four profiles map to the UI radio group in ``index.html``:

* ``maximum`` — read-only root, non-root user, tmpfs mounts, cap-drop ALL.
* ``service_friendly`` — relaxes ``maximum`` for daemon-style service images
  that need writable root (postgres init scripts, redis AOF rewrites, mongo
  journal). Drops ``read_only`` and the non-root user constraint while
  keeping cap-drop + no-new-privileges + ``pids_limit``. Auto-picked by
  ``service_runner._pick_service_profile`` for ``runtime: service`` tasks
  when the node's global profile is ``maximum``; operators can force a
  different choice via the ``service_security_profile`` setting.
* ``standard`` — cap-drop ALL + no-new-privileges; keep root + writable root.
* ``relaxed`` — no hardening (developer opt-in; not the default).

A *profile* is just a dict of kwargs for ``docker.containers.run``. The
runtime merges these with per-task kwargs (image, command, env). Adding a
new profile is a matter of adding one entry to :data:`PROFILES` and wiring
it into the settings UI.
"""

from __future__ import annotations

from typing import Any


PROFILES: dict[str, dict[str, Any]] = {
    "maximum": {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": 256,
        "read_only": True,
        "tmpfs": {
            # ``exec`` is required on /tmp because pip --user installs land
            # under /tmp/.local (HOME=/tmp), and Python loads native
            # extensions like numpy's _multiarray_umath.so via mmap which
            # needs the mount to be executable. Size is bumped to 1g so
            # data-science deps (pandas + numpy) fit. The remaining
            # cap-drop + no-new-privileges + read-only root constraints
            # still prevent privilege escalation; ``exec`` here is no
            # weaker than the existing executable workspace bind mount.
            "/tmp": "size=1g,exec",
            "/var/tmp": "size=64m",
            "/root": "size=16m",
        },
        "user": "65534:65534",
    },
    "service_friendly": {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": 512,
    },
    "standard": {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "pids_limit": 512,
    },
    "relaxed": {},
}


def get_docker_security_opts(profile: str) -> dict[str, Any]:
    """Return the Docker container kwargs for *profile*.

    Unknown profiles fall back to ``standard`` rather than ``relaxed``, so a
    config typo never accidentally disables hardening.
    """
    if profile == "relaxed":
        return dict(PROFILES["relaxed"])
    return dict(PROFILES.get(profile, PROFILES["standard"]))
