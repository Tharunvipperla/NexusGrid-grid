"""Grid-key HMAC, join-request signing, per-IP rate limit.

Extracted from Phase-1/node_modified.py:

* ``_check_join_rate_limit`` — lines 705-716
* ``_verify_join_hmac`` — lines 719-734
* ``_sign_join_request`` — lines 737-746

The *HTTP shell* of the peer protocol (join endpoint, callback endpoint)
lives in :mod:`nexus.api.peer`. This module contains only the stateless
primitives they rely on, so they can be unit-tested without FastAPI.

The grid key is looked up via the dependency-injection hook registered by
:func:`set_grid_key_provider` — networking must not import the relay
settings module directly because the settings may be reloaded at runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable

_JOIN_RATE_LIMIT: dict[str, list[float]] = {}
_JOIN_RATE_WINDOW = 60
_JOIN_RATE_MAX = 5


GridKeyProvider = Callable[[], str]
_grid_key_provider: GridKeyProvider | None = None


def set_grid_key_provider(provider: GridKeyProvider | None) -> None:
    """Register the zero-arg callable that returns the current grid key.

    Wired from :mod:`nexus.app` at startup. When no provider is set,
    :func:`_grid_key` returns ``""`` and HMAC verification is skipped —
    matching Phase-1 behaviour when the key is unconfigured.
    """
    global _grid_key_provider
    _grid_key_provider = provider


def _grid_key() -> str:
    if _grid_key_provider is None:
        return ""
    try:
        return str(_grid_key_provider() or "")
    except Exception:
        return ""


def check_join_rate_limit(client_ip: str) -> bool:
    """Return ``True`` if the request is within the per-IP rate budget."""
    now = time.time()
    cutoff = now - _JOIN_RATE_WINDOW
    timestamps = [t for t in _JOIN_RATE_LIMIT.get(client_ip, []) if t > cutoff]
    if len(timestamps) >= _JOIN_RATE_MAX:
        _JOIN_RATE_LIMIT[client_ip] = timestamps
        return False
    timestamps.append(now)
    _JOIN_RATE_LIMIT[client_ip] = timestamps
    return True


def sign_join_request(node_uuid: str, requester_address: str) -> str:
    """Return a hex HMAC for a join request, or ``""`` if no grid key set."""
    grid_key = _grid_key()
    if not grid_key:
        return ""
    material = f"join|{node_uuid}|{requester_address}".encode("utf-8")
    return hmac.new(grid_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify_join_hmac(data: dict) -> bool:
    """Verify ``data["join_hmac"]`` against the expected signature.

    When no grid key is configured, verification is skipped (returns
    ``True``) to preserve Phase-1's opt-in security model.
    """
    grid_key = _grid_key()
    if not grid_key:
        return True
    sig = data.get("join_hmac", "")
    if not sig:
        return False
    node_uuid = data.get("node_uuid", "")
    requester_address = data.get("requester_address", "")
    material = f"join|{node_uuid}|{requester_address}".encode("utf-8")
    expected = hmac.new(
        grid_key.encode("utf-8"), material, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def sign_callback_hmac(node_uuid: str, responder_address: str) -> str:
    """Return a hex HMAC for a ``/peer/callback_*`` payload, or ``""``.

    Callback endpoints are auth-less by design (a callback is a reply to a
    join request the local node initiated), so the HMAC is the only thing
    that proves the sender knows the grid key. Returns ``""`` when no grid
    key is configured, matching Phase-1's opt-in security model.
    """
    grid_key = _grid_key()
    if not grid_key:
        return ""
    material = f"callback|{node_uuid}|{responder_address}".encode("utf-8")
    return hmac.new(grid_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify_callback_hmac(data: dict) -> bool:
    """Verify ``data["callback_hmac"]`` for a ``/peer/callback_*`` payload.

    When no grid key is configured, verification is skipped (returns
    ``True``) to preserve Phase-1's opt-in security model.
    """
    grid_key = _grid_key()
    if not grid_key:
        return True
    sig = data.get("callback_hmac", "")
    if not sig:
        return False
    node_uuid = data.get("node_uuid", "")
    responder_address = data.get("responder_address", "")
    material = f"callback|{node_uuid}|{responder_address}".encode("utf-8")
    expected = hmac.new(
        grid_key.encode("utf-8"), material, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def clear_rate_limits() -> None:
    """Tests only: drop every tracked per-IP timestamp list."""
    _JOIN_RATE_LIMIT.clear()


__all__ = [
    "set_grid_key_provider",
    "check_join_rate_limit",
    "sign_join_request",
    "verify_join_hmac",
    "sign_callback_hmac",
    "verify_callback_hmac",
    "clear_rate_limits",
]
