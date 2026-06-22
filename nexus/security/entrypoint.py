"""Validation for user-supplied entrypoint and setup commands.

Phase-1's :func:`prepare_multiline_command` already :func:`shlex.quote`-s
unsafe characters and joins multi-line input with ``&&``. That blunts the
basic cases but leaves a tightening gap for in
``maximum`` profile we want a hard allowlist, and in *every* profile we
want explicit rejection of well-known injection sequences (``;``, ``|``,
backticks, ``$()``, redirects, embedded ``&&``/``||`` chains) inside a
single line — multi-line entries are still chained intentionally by the
caller, but a *single* line is treated as one command.

The validator runs **before** :func:`prepare_multiline_command`. On
violation it raises :class:`EntrypointError` so the executor can record a
clean task failure and surface the reason in audit/UI.
"""

from __future__ import annotations

import re
import shlex


class EntrypointError(ValueError):
    """Raised when an entrypoint or setup command fails validation."""


# Sequences that must never appear inside a single user-typed line.
# ``&&`` is the join operator used between *separate* lines of the multi-line
# entrypoint — its presence inside one line means the user is trying to
# smuggle a chained command past the per-line check. Same logic for ``||``.
_FORBIDDEN_TOKENS: tuple[str, ...] = (
    ";",
    "`",
    "$(",
    "${",
    ">>",
    "&&",
    "||",
    "<(",
    ">(",
)

# Characters whose presence flags shell redirection / piping. We allow ``|``
# inside argument values via ``shlex`` only when fully quoted; the simple
# substring test is intentionally conservative.
_REDIRECT_CHARS: frozenset[str] = frozenset({"|", ">", "<"})

# Maximum-profile allowlist: head must be one of these binaries (or a
# relative-path script), and the line must look like ``head <args>``.
_MAX_PROFILE_RE = re.compile(
    r"^(?:python|python3|py|node|npm|sh|bash|wasmtime|"
    r"\./[A-Za-z0-9_./-]+)"
    r"(?:\s+\S.*)?$"
)


def _looks_quoted(line: str) -> bool:
    """Return True when *line* tokenizes cleanly under POSIX rules."""
    try:
        shlex.split(line, posix=True)
    except ValueError:
        return False
    return True


def _line_has_unquoted(line: str, chars: frozenset[str]) -> bool:
    """True when any of *chars* appears outside a quoted segment of *line*."""
    in_single = False
    in_double = False
    escape = False
    for ch in line:
        if escape:
            escape = False
            continue
        if ch == "\\" and not in_single:
            escape = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double and ch in chars:
            return True
    return False


def _validate_line(line: str, profile: str) -> None:
    if not _looks_quoted(line):
        raise EntrypointError(f"Cannot tokenize entrypoint line: {line!r}")

    for token in _FORBIDDEN_TOKENS:
        if token in line:
            # ``&&``/``||`` inside a single line is the smuggling case we
            # specifically want to block; the others are unsafe in any form.
            raise EntrypointError(
                f"Forbidden shell sequence {token!r} in entrypoint line: {line!r}"
            )

    if _line_has_unquoted(line, _REDIRECT_CHARS):
        raise EntrypointError(
            f"Unquoted redirect/pipe character in entrypoint line: {line!r}"
        )

    if profile == "maximum" and not _MAX_PROFILE_RE.match(line):
        raise EntrypointError(
            "Entrypoint not on maximum-profile allowlist (allowed heads: "
            "python, python3, py, node, npm, sh, bash, wasmtime, "
            "./<script>): "
            f"{line!r}"
        )


def validate_entrypoint(entrypoint: str, profile: str = "standard") -> list[str]:
    """Validate *entrypoint* and return its non-comment, non-blank lines.

    *profile* is the ``security_profile`` value from ``LOCAL_SETTINGS`` —
    only ``maximum`` activates the allowlist; any other value enforces only
    the universal forbidden-token check.

    Raises :class:`EntrypointError` on any violation.
    """
    if entrypoint is None or not str(entrypoint).strip():
        raise EntrypointError("Entrypoint is empty.")
    lines = [
        ln.strip()
        for ln in str(entrypoint).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        raise EntrypointError("Entrypoint is empty after stripping comments.")
    for line in lines:
        _validate_line(line, profile)
    return lines


def validate_setup_cmd(setup_cmd: str, profile: str = "standard") -> list[str]:
    """Validate a ``setup_cmd`` (e.g. ``pip install -r req.txt``).

    Setup commands typically run package managers and are validated under
    the same forbidden-token rules. The maximum-profile allowlist is
    relaxed slightly: ``pip``, ``apt``, ``apk``, ``go``, ``cargo``, and
    ``make`` are also accepted as command heads.
    """
    if setup_cmd is None or not str(setup_cmd).strip():
        return []
    lines = [
        ln.strip()
        for ln in str(setup_cmd).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    extended_re = re.compile(
        r"^(?:python|python3|py|node|npm|sh|bash|wasmtime|"
        r"pip|pip3|apt|apt-get|apk|go|cargo|make|"
        r"\./[A-Za-z0-9_./-]+)"
        r"(?:\s+\S.*)?$"
    )
    for line in lines:
        if not _looks_quoted(line):
            raise EntrypointError(f"Cannot tokenize setup line: {line!r}")
        for token in _FORBIDDEN_TOKENS:
            if token in line:
                raise EntrypointError(
                    f"Forbidden shell sequence {token!r} in setup line: {line!r}"
                )
        if _line_has_unquoted(line, _REDIRECT_CHARS):
            raise EntrypointError(
                f"Unquoted redirect/pipe character in setup line: {line!r}"
            )
        if profile == "maximum" and not extended_re.match(line):
            raise EntrypointError(
                f"Setup line not on maximum-profile allowlist: {line!r}"
            )
    return lines


__all__ = ["EntrypointError", "validate_entrypoint", "validate_setup_cmd"]
