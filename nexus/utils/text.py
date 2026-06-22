"""String sanitization, masking, and multi-line command helpers.

Extracted from Phase-1/node_modified.py (lines 771–788, 932–977).
"""

from __future__ import annotations

import os
import re
import shlex
import zipfile

MASKED_IP_PLACEHOLDER = (
    "\u2022\u2022\u2022.\u2022\u2022\u2022.\u2022\u2022\u2022.\u2022\u2022\u2022"
)

_IP_PATTERN = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?\b")
_IP_SAFE_LIST = {"127.0.0.1", "0.0.0.0", "8.8.8.8"}

_SHELL_SAFE_PATTERN = re.compile(r"^[a-zA-Z0-9_./ =:@{}\[\],-]+$")


def mask_ips_in_log(log_text: str) -> str:
    """Replace IP addresses (and optional ``:port``) in *log_text* with bullets.

    Loopback / broadcast / public-sentinel addresses in ``_IP_SAFE_LIST`` are left
    untouched so operators can still see localhost / 8.8.8.8 probes.
    """

    def _replacer(m: re.Match[str]) -> str:
        ip = m.group(1)
        if ip in _IP_SAFE_LIST:
            return m.group(0)
        port_suffix = m.group(2) or ""
        masked_port = ":****" if port_suffix else ""
        return MASKED_IP_PLACEHOLDER + masked_port

    return _IP_PATTERN.sub(_replacer, log_text)


def sanitize_shell_token(value: str, label: str = "command") -> str:
    """Validate and quote a shell token to prevent injection.

    Accepts a safe subset of characters verbatim; anything else gets ``shlex.quote``.
    Empty input returns ``""``.
    """
    value = str(value).strip()
    if not value:
        return ""
    if _SHELL_SAFE_PATTERN.match(value):
        return value
    return shlex.quote(value)


def prepare_multiline_command(raw_cmd: str) -> str:
    """Join a multi-line command string into a single ``&&``-chained command.

    Blank lines and shell comments (``# ...``) are dropped. Every surviving line
    is passed through ``sanitize_shell_token``.
    """
    lines = [
        line.strip()
        for line in raw_cmd.strip().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        return ""
    if len(lines) == 1:
        return sanitize_shell_token(lines[0], "command")
    return " && ".join(sanitize_shell_token(line, "command") for line in lines)


def split_csv(value: str) -> list[str]:
    """Split a comma-separated string, trimming whitespace and dropping empties."""
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def safe_extractall(zf: zipfile.ZipFile, target_dir: str) -> None:
    """Extract *zf* into *target_dir*, rejecting entries with path traversal.

    Raises ``ValueError`` if any archive entry resolves outside *target_dir*.
    """
    abs_target = os.path.realpath(target_dir)
    for member in zf.namelist():
        member_path = os.path.realpath(os.path.join(target_dir, member))
        if (
            not member_path.startswith(abs_target + os.sep)
            and member_path != abs_target
        ):
            raise ValueError(f"Zip path traversal detected: {member}")
    zf.extractall(target_dir)
