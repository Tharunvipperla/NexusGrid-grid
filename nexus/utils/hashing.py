"""Content and object hashing helpers.

Centralizing these primitives here means cache-key derivation stays consistent
across runtimes and caches, rather than inlining SHA-256 calls throughout.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(data: bytes | str) -> str:
    """Return the SHA-256 hex digest of *data*.

    Strings are encoded as UTF-8. Use this whenever a stable content-addressed
    key is required (venv caches, pip wheels, workspace bundles).
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_hash(obj: Any) -> str:
    """Hash a JSON-serializable object deterministically.

    Sorting keys + default separators ensures identical objects produce
    identical digests across Python runs.
    """
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return content_hash(blob)
