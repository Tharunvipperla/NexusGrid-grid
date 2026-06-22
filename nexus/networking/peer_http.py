"""Direct-HTTP peer request with relay fallback.

Ported from node_modified.py (``_peer_http_post`` at lines 5659-5683).

Attempts a direct POST to ``https://<resolved_ip><path>`` first (TLS by
default since Step 6). The peer cert's SHA-256 fingerprint is
verified against ``Peer.cert_fingerprint`` when one is recorded — a
mismatch is treated like a connection failure (trust pinning). On TLS
errors the call falls through to plain ``http://`` for backward compat
with pre-Step-6 peers, and finally to the relay path.

Returns a dict with shape ``{"status": int, "body": dict}``.
"""

from __future__ import annotations

import logging
import time

import httpx
from sqlalchemy import select

from nexus.core import STATE
from nexus.core.identity import resolve_ip_to_uuid, resolve_uuid_to_ip

_log = logging.getLogger("nexus.networking.peer_http")

# Cache (ip, port) -> (fingerprint, expiry_ts) so consecutive requests don't
# re-open a probe socket per call. TTL kept short enough to catch a cert
# rotation within a minute.
_FP_CACHE_TTL = 60.0
_fingerprint_cache: dict[tuple[str, int], tuple[str, float]] = {}


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    if ":" in target and not target.startswith("["):
        host, _, port = target.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return target, default_port
    return target, default_port


async def _fetch_cached_fingerprint(host: str, port: int) -> str | None:
    cached = _fingerprint_cache.get((host, port))
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    import asyncio

    from nexus.security.tls import fetch_peer_fingerprint

    try:
        fp = await asyncio.to_thread(fetch_peer_fingerprint, host, port)
    except Exception as exc:
        _log.debug("fingerprint fetch failed for %s:%d: %s", host, port, exc)
        return None
    _fingerprint_cache[(host, port)] = (fp, now + _FP_CACHE_TTL)
    return fp


async def _lookup_expected_fingerprint(target_ip: str) -> str | None:
    """Look up the stored fingerprint for *target_ip*. ``None`` if unknown."""
    try:
        from nexus.storage.database import get_session
        from nexus.storage.models import Peer
    except Exception:
        return None
    try:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(Peer).filter(
                        (Peer.ip == target_ip) | (Peer.resolved_ip == target_ip)
                    )
                )
            ).scalar_one_or_none()
        if row and row.cert_fingerprint:
            return str(row.cert_fingerprint).strip().lower() or None
    except Exception:
        return None
    return None


async def _is_peer_paused(target: str) -> bool:
    """True if the local user has paused this peer.

    Looked up by both ``Peer.ip`` and ``Peer.resolved_ip``. Any error
    falls back to False — don't accidentally silence outbound on a
    transient DB issue.
    """
    if not target:
        return False
    try:
        from nexus.storage.database import get_session
        from nexus.storage.models import Peer
    except Exception:
        return False
    try:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(Peer).filter(
                        (Peer.ip == target) | (Peer.resolved_ip == target)
                    )
                )
            ).scalar_one_or_none()
        return bool(row and getattr(row, "paused", 0))
    except Exception:
        return False


async def _lookup_peer_relay_urls(target: str) -> set[str] | None:
    """Peer's advertised relay-pool URL set, or None if unknown.

    Used by the relay fallback in :func:`peer_http_post` so we can pick
    a relay both ends are subscribed to. ``None`` (not empty set) means
    "no advertisement on file" — caller falls back to pool-wide routing
    rather than refusing to send.
    """
    if not target:
        return None
    try:
        from nexus.storage.database import get_session
        from nexus.storage.models import Peer
    except Exception:
        return None
    try:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(Peer).filter(
                        (Peer.ip == target) | (Peer.resolved_ip == target)
                    )
                )
            ).scalar_one_or_none()
        if not row or not row.peer_relay_urls:
            return None
        import json as _json

        raw = _json.loads(row.peer_relay_urls or "[]")
        if not isinstance(raw, list):
            return None
        urls = {str(u).strip() for u in raw if isinstance(u, str) and u}
        return urls or None
    except Exception:
        return None


async def _lookup_peer_transient_creds(target: str) -> tuple[list[str], str] | None:
    """``(relay_urls, grid_key)`` for transient-WS fallback, or None.

    Returns the *list* (not set) so the caller can try in stable order,
    and ``grid_key`` is the peer's relay credential shared at pair-accept
    time. Returns None if either piece is missing — caller skips the
    fallback rather than failing loudly.
    """
    if not target:
        return None
    try:
        from nexus.storage.database import get_session
        from nexus.storage.models import Peer
    except Exception:
        return None
    try:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(Peer).filter(
                        (Peer.ip == target) | (Peer.resolved_ip == target)
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        grid_key = (getattr(row, "peer_grid_key", "") or "").strip()
        if not grid_key:
            return None
        import json as _json

        raw = _json.loads(row.peer_relay_urls or "[]")
        if not isinstance(raw, list):
            return None
        urls = [str(u).strip() for u in raw if isinstance(u, str) and u]
        if not urls:
            return None
        return urls, grid_key
    except Exception:
        return None


async def peer_http_post(
    target_ip: str,
    path: str,
    body: dict,
    timeout: float = 5.0,
    skip_pin: bool = False,
) -> dict:
    """POST *body* to *path* on *target_ip* (direct first, relay fallback).

    ``skip_pin=True`` bypasses the cert-fingerprint pin check. Reserved for
    Accept actions where the user explicitly opts in to trust the peer's
    current cert (e.g. after the peer regenerated their identity by
    reinstalling). The fresh fingerprint is re-stored from the response.

    short-circuits with a 503-equivalent when the local user
    has paused this peer — no heartbeats, no RPC, no tunnels. The peer's
    side sees their requests time out, which matches "looks offline".
    """
    if await _is_peer_paused(target_ip):
        return {
            "status": 503,
            "body": {"error": "Peer paused locally"},
            "paused": True,
        }
    resolved_ip = resolve_uuid_to_ip(target_ip)
    expected_fp = (
        None if skip_pin else await _lookup_expected_fingerprint(target_ip)
    )

    host, port = _split_host_port(resolved_ip, 8000)

    # HTTPS first (TLS by default). verify=False because peers use
    # self-signed certs — pinning replaces CA-chain trust.
    try:
        if expected_fp:
            served = await _fetch_cached_fingerprint(host, port)
            if served and served != expected_fp:
                _log.warning(
                    "fingerprint mismatch for %s (%s vs stored %s)",
                    target_ip,
                    served,
                    expected_fp,
                )
                return {
                    "status": 495,
                    "body": {"error": "Peer certificate fingerprint mismatch"},
                }
        async with httpx.AsyncClient(verify=False) as client:
            res = await client.post(
                f"https://{resolved_ip}{path}", json=body, timeout=timeout
            )
            return {"status": res.status_code, "body": res.json()}
    except Exception:
        pass

    # Fall back to plain HTTP for pre-Step-6 peers.
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"http://{resolved_ip}{path}", json=body, timeout=timeout
            )
            return {"status": res.status_code, "body": res.json()}
    except Exception:
        pass

    # Relay fallback via the relay client's http_request frame
    target_uuid = resolve_ip_to_uuid(target_ip)
    pool_result: dict | None = None
    if STATE.relay_connected and (
        target_uuid in STATE.relay_peers
        or target_uuid in STATE.discovered_peers
        or target_ip in STATE.relay_peers
        or target_ip in STATE.discovered_peers
    ):
        try:
            from nexus.networking.relay_client import relay_http_request

            relay_target = target_uuid if target_uuid in STATE.relay_peers else target_ip
            # Restrict relay candidates to the peer's
            # advertised relay-pool URL set (intersection with ours
            # happens inside relay_send_to_peer). If no advertisement
            # is on file, allowed=None → pool-wide fallback.
            allowed = await _lookup_peer_relay_urls(target_ip)
            pool_result = await relay_http_request(
                relay_target, "POST", path, body, timeout=10.0,
                allowed_relay_urls=allowed,
            )
            if pool_result and pool_result.get("status") not in (503, 504):
                return pool_result
        except Exception:
            pass

    # Last-resort transient-WS fallback. Triggered when the
    # pool path returned 503/504 (no shared relay subscribed OR target
    # not on any pool relay) AND we have the peer's grid_key from the
    # pair-accept exchange. Open a fresh WS to each of their advertised
    # relays in order; first non-503/504 response wins.
    creds = await _lookup_peer_transient_creds(target_ip)
    if creds is not None:
        relay_urls, peer_grid_key = creds
        try:
            from nexus.networking.relay_client import (
                relay_http_request_one_shot,
            )

            transient_target = target_uuid or target_ip
            for relay_url in relay_urls:
                try:
                    resp = await relay_http_request_one_shot(
                        relay_url, peer_grid_key, transient_target,
                        "POST", path, body, timeout=10.0,
                    )
                    if resp and resp.get("status") not in (503, 504):
                        return resp
                except Exception:
                    continue
        except Exception:
            pass

    return pool_result or {
        "status": 503,
        "body": {"error": "Peer unreachable (direct + pool + transient all failed)"},
    }


def _reset_fingerprint_cache() -> None:
    _fingerprint_cache.clear()


__all__ = ["peer_http_post"]
