"""Persistent secret files for HMAC signing and local-API auth.

Extracted from node_modified.py (lines 126-162).

Two tokens live on disk next to :data:`nexus.core.BASE_DIR`:

* ``.nexus_secret`` — shared HMAC key. Used by :mod:`nexus.security.crypto`
  to sign / verify peer-to-peer payloads.
* ``.nexus_local_token`` — bearer token the UI (and any local CLI) presents to
  ``/local/*`` endpoints.

Both files are created on first run and chmod 0o600 on Unix.

Callers should use :func:`get_signing_secret` / :func:`get_local_api_token`
rather than reading the files directly. The module caches the values so
repeated lookups are free, and the cache can be reset in tests via
:func:`_reset_for_testing`.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

from nexus.core.paths import BASE_DIR, secure_file_permissions

SIGNING_SECRET_FILE = ".nexus_secret"
LOCAL_TOKEN_FILE = ".nexus_local_token"
SIGNING_SECRET_ENV = "NEXUS_SIGNING_SECRET"

_signing_secret: Optional[str] = None
_local_api_token: Optional[str] = None


def _resolve_path(filename: str) -> Path:
    return Path(BASE_DIR) / filename


def _read_or_create(path: Path, *, allow_env: str | None = None) -> str:
    """Return the token stored at *path*, creating a fresh one if missing.

    If *allow_env* names an environment variable and that variable is set,
    the env value wins and the file is not touched. This matches the the original implementation
    behaviour for ``NEXUS_SIGNING_SECRET``.
    """
    if allow_env:
        env_val = os.getenv(allow_env, "").strip()
        if env_val:
            return env_val
    # Fast path: file already populated. Three processes on the same host
    # racing on first launch all reach the same value here.
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            secure_file_permissions(path)
            return existing
    except FileNotFoundError:
        pass
    # Atomic create-or-fail. The loser of the race re-reads the winner's value
    # so every process ends up with the same secret in memory.
    token = secrets.token_hex(32)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        path.write_text(token, encoding="utf-8")
    secure_file_permissions(path)
    return token


def get_signing_secret() -> str:
    """Return the HMAC signing secret (cached after first call)."""
    global _signing_secret
    if _signing_secret is None:
        _signing_secret = _read_or_create(
            _resolve_path(SIGNING_SECRET_FILE), allow_env=SIGNING_SECRET_ENV
        )
    return _signing_secret


def get_local_api_token() -> str:
    """Return the local API bearer token (cached after first call)."""
    global _local_api_token
    if _local_api_token is None:
        _local_api_token = _read_or_create(_resolve_path(LOCAL_TOKEN_FILE))
    return _local_api_token


def _reset_for_testing() -> None:
    """Forget cached values. Pytest fixtures call this between tests."""
    global _signing_secret, _local_api_token
    _signing_secret = None
    _local_api_token = None
