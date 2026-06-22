"""Content and object hashing helpers.

Phase-2 addition: these primitives are implicit in Phase-1 (SHA-256 calls are
inlined throughout). Centralizing them here means cache-key derivation stays
consistent across runtimes and caches.
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
