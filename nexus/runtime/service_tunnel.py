"""Service data-plane tunnel (Phase B).

Carries raw bytes between a consumer-local ``127.0.0.1:<port>`` listener and the
provider's advertised service, gated by an approved :class:`ServiceGrant`. It is
protocol-agnostic — HTTP, HTTPS, streaming, raw TCP all just flow — because it
forwards bytes beneath the application protocol, reusing the same peer transport
(``_send_to_peer``: LAN ``/peer/ws`` or relay) as the task tunnel.

Security:
* every byte requires an ``approved`` grant whose ``consumer_uuid`` matches the
  sending peer — a peer can't ride someone else's grant;
* the provider only ever dials its own service's ``local_host:local_port`` (a
  named target it configured) — never anything the consumer supplies (no SSRF);
* :func:`close_grant_streams` is called on revoke, cutting live streams at once.

Metering: the consumer measures connection-seconds + bytes and signs a
``kind="service"`` / ``kind="service_bytes"`` usage receipt on disconnect, so
"services used" is counterparty-attested like the rest of the ledger.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid

from nexus.networking.tunnel import _send_to_peer

_log = logging.getLogger("nexus.runtime.service_tunnel")

_CHUNK = 32 * 1024

# tunnel_id -> {writer, role, grant_id, peer_id, transform}
_streams: dict[str, dict] = {}
# grant_id -> {server, port, opened_at, bytes, tunnel_ids:set}
_consumer: dict[str, dict] = {}


# --- pumps: pluggable per-service byte processors ---------------------------
#
# A pump is a ``transform(direction, chunk) -> bytes | None`` callable run on the
# PROVIDER side for that node's own service (``direction`` is ``"to_consumer"``
# for bytes coming out of the service, ``"to_provider"`` for bytes going in).
# The default forwards bytes unchanged — that's the best general default. A host
# can ship a smarter pump for their own service (compress, redact, add headers,
# rate-limit, log) and name it on the service via ``pump:``. Returning ``None``
# drops the chunk. Pumps only ever run the HOST'S OWN code on the HOST'S machine.

def _default_transform(direction: str, chunk: bytes) -> bytes:
    return chunk


_PUMPS: dict[str, "callable"] = {"default": lambda: _default_transform}
_pumps_loaded = False


def register_pump(name: str, factory) -> None:
    """Register a pump factory. ``factory()`` returns a
    ``transform(direction, chunk)`` callable. Call this from a file in the
    node-local ``nexus_pumps/`` directory."""
    _PUMPS[str(name)] = factory


def _load_custom_pumps() -> None:
    """Lazily import every ``nexus_pumps/*.py`` next to the node so a host can
    drop in their own pumps. Host-trusted code on the host's own machine."""
    global _pumps_loaded
    if _pumps_loaded:
        return
    _pumps_loaded = True
    import pathlib
    import importlib.util
    d = pathlib.Path("nexus_pumps")
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"nexus_pumps.{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            _log.warning("failed to load custom pump %s", f, exc_info=True)


def _get_transform(name: str):
    _load_custom_pumps()
    factory = _PUMPS.get(str(name or "default")) or _PUMPS["default"]
    try:
        return factory()
    except Exception:
        _log.warning("pump %s factory failed; using default", name, exc_info=True)
        return _default_transform


def _open_frame(tunnel_id, grant_id, component=""):
    return {"type": "svc_open", "tunnel_id": tunnel_id,
            "grant_id": grant_id, "component": component}


def _data_frame(tunnel_id, chunk):
    return {"type": "svc_data", "tunnel_id": tunnel_id,
            "b64": base64.b64encode(chunk).decode("ascii")}


def _close_frame(tunnel_id, reason=""):
    return {"type": "svc_close", "tunnel_id": tunnel_id, "reason": reason}


# --- consumer side ----------------------------------------------------------


async def _provider_components(provider_uuid: str, service_name: str) -> list[str]:
    """Fetch the provider's PUBLIC component names for a service (no local
    targets — those stay on the host). Empty list = a simple (non-composite)
    service."""
    from nexus.runtime.service_grants import resolve_peer_addr
    from nexus.networking.peer_http import peer_http_post

    addr = await resolve_peer_addr(provider_uuid)
    if not addr:
        return []
    try:
        res = await peer_http_post(addr, "/peer/profile", {}, timeout=4.0)
    except Exception:
        return []
    if res.get("status") != 200:
        return []
    for svc in (res.get("body") or {}).get("hosted_services") or []:
        if isinstance(svc, dict) and svc.get("name") == service_name:
            return [str(c.get("name")) for c in (svc.get("components") or [])
                    if isinstance(c, dict) and c.get("name")]
    return []


async def open_tunnel(grant_id: str) -> dict:
    """Open a local listener per service component (one grant → a tunnel per
    sub-service). Returns ``{ok, endpoints:[{name, host, port}]}``. Idempotent."""
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage import get_session
    from nexus.storage.models import ServiceGrant

    if grant_id in _consumer and _consumer[grant_id].get("listeners"):
        return {"ok": True, "endpoints": _endpoints(grant_id)}

    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        if g is None or g.consumer_pubkey != get_local_group_pubkey():
            return {"ok": False, "error": "not_your_grant"}
        if g.status != "approved":
            return {"ok": False, "error": f"grant {g.status}"}
        provider_uuid, service_name = g.provider_uuid, g.service_name
        provider_pubkey = g.provider_pubkey

    rec = {"opened_at": time.time(), "bytes": 0, "tunnel_ids": set(),
           "provider_uuid": provider_uuid, "provider_pubkey": provider_pubkey,
           "service_name": service_name, "grant_id": grant_id, "listeners": []}

    # Composite → one listener per component; simple → a single listener.
    components = await _provider_components(provider_uuid, service_name)
    targets = components or [""]  # "" = the top-level service target
    for comp in targets:
        def _factory(component):
            async def _on_accept(reader, writer):
                await _consumer_stream(grant_id, component, reader, writer)
            return _on_accept
        server = await asyncio.start_server(_factory(comp), host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        rec["listeners"].append({"name": comp or service_name, "component": comp,
                                 "server": server, "port": port})
    _consumer[grant_id] = rec
    _log.info("[svc-tunnel] consumer opened %d listener(s) for %s",
              len(rec["listeners"]), service_name)
    return {"ok": True, "endpoints": _endpoints(grant_id)}


def _endpoints(grant_id: str) -> list[dict]:
    rec = _consumer.get(grant_id) or {}
    return [{"name": l["name"], "host": "127.0.0.1", "port": l["port"]}
            for l in rec.get("listeners", [])]


async def _consumer_stream(grant_id, component, reader, writer):
    rec = _consumer.get(grant_id)
    if not rec:
        writer.close()
        return
    tunnel_id = uuid.uuid4().hex
    _streams[tunnel_id] = {"writer": writer, "role": "consumer",
                           "grant_id": grant_id, "peer_id": rec["provider_uuid"]}
    rec["tunnel_ids"].add(tunnel_id)
    if not await _send_to_peer(rec["provider_uuid"],
                               _open_frame(tunnel_id, grant_id, component)):
        _streams.pop(tunnel_id, None)
        writer.close()
        return
    await _pump(tunnel_id, reader, rec["provider_uuid"], grant_id)


async def disconnect_tunnel(grant_id: str) -> dict:
    """Tear down all of a grant's listeners + streams and bill the session."""
    rec = _consumer.pop(grant_id, None)
    if not rec:
        return {"ok": True}
    for l in rec.get("listeners", []):
        try:
            l["server"].close()
        except Exception:
            pass
    for tid in list(rec["tunnel_ids"]):
        await _shutdown_stream(tid, "disconnect", notify=True)
    await _bill_session(rec)
    return {"ok": True}


async def _bill_session(rec: dict) -> None:
    secs = int(time.time() - rec.get("opened_at", time.time()))
    nbytes = int(rec.get("bytes", 0))
    if secs <= 0 and nbytes <= 0:
        return
    from nexus.runtime.usage_receipts import make_receipt, store_and_apply
    from nexus.runtime.service_grants import resolve_peer_addr

    provider = rec["provider_pubkey"]
    ref = rec["service_name"]
    addr = await resolve_peer_addr(rec["provider_uuid"])
    for kind, amount in (("service", secs), ("service_bytes", nbytes)):
        if amount <= 0:
            continue
        receipt, sig = make_receipt(group_id="", provider_pubkey=provider,
                                    kind=kind, ref_id=ref, amount=amount)
        await store_and_apply(receipt, sig)
        if addr:
            from nexus.networking.peer_http import peer_http_post
            try:
                await peer_http_post(addr, "/peer/usage_receipt", {"receipt": receipt, "sig": sig})
            except Exception:
                _log.debug("service receipt push failed", exc_info=True)


def tunnel_status(grant_id: str) -> dict:
    rec = _consumer.get(grant_id)
    if not rec or not rec.get("listeners"):
        return {"open": False}
    return {"open": True, "endpoints": _endpoints(grant_id),
            "bytes": int(rec.get("bytes", 0)),
            "secs": int(time.time() - rec.get("opened_at", time.time()))}


# --- provider side ----------------------------------------------------------


async def _handle_open(peer_id: str, frame: dict) -> None:
    from sqlalchemy import select
    from nexus.core.config import LOCAL_SETTINGS
    from nexus.security.group_keys import get_local_group_pubkey
    from nexus.storage import get_session
    from nexus.storage.models import ServiceGrant

    tunnel_id = str(frame.get("tunnel_id") or "")
    grant_id = str(frame.get("grant_id") or "")
    if not tunnel_id or not grant_id:
        return
    async with get_session() as s:
        g = await s.get(ServiceGrant, grant_id)
        # Gate: approved grant I issued, to THIS peer (no riding others' grants).
        if (g is None or g.provider_pubkey != get_local_group_pubkey()
                or g.status != "approved" or g.consumer_uuid != peer_id):
            await _send_to_peer(peer_id, _close_frame(tunnel_id, "denied"))
            return
        service_name = g.service_name
    component = str(frame.get("component") or "")
    svc = None
    for it in LOCAL_SETTINGS.get("hosted_services") or []:
        if isinstance(it, dict) and it.get("name") == service_name:
            svc = it
            break
    if not svc:
        await _send_to_peer(peer_id, _close_frame(tunnel_id, "no_target"))
        return
    # Composite: resolve the named component's own target (a named target the
    # host configured — never anything the consumer supplies, so no SSRF).
    target = svc
    if component:
        target = next((c for c in (svc.get("components") or [])
                       if isinstance(c, dict) and c.get("name") == component), None)
        if target is None:
            await _send_to_peer(peer_id, _close_frame(tunnel_id, "no_component"))
            return
    if not target.get("local_port"):
        await _send_to_peer(peer_id, _close_frame(tunnel_id, "no_target"))
        return
    host = str(target.get("local_host") or "127.0.0.1")
    port = int(target.get("local_port") or 0)
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        await _send_to_peer(peer_id, _close_frame(tunnel_id, "dial_failed"))
        return
    _streams[tunnel_id] = {"writer": writer, "role": "provider",
                           "grant_id": grant_id, "peer_id": peer_id,
                           "transform": _get_transform(target.get("pump") or svc.get("pump"))}
    asyncio.create_task(_pump(tunnel_id, reader, peer_id, grant_id),
                         name=f"svc.provider.pump.{tunnel_id}")


# --- shared pump + dispatch -------------------------------------------------


async def _pump(tunnel_id, reader, peer_id, grant_id):
    """Read from the local socket, forward as svc_data frames to *peer_id*."""
    try:
        while True:
            chunk = await reader.read(_CHUNK)
            if not chunk:
                break
            st = _streams.get(tunnel_id)
            # Provider's own pump shapes bytes leaving its service.
            if st and st.get("transform"):
                chunk = st["transform"]("to_consumer", chunk)
                if not chunk:
                    continue
            rec = _consumer.get(grant_id)
            if rec is not None:
                rec["bytes"] = rec.get("bytes", 0) + len(chunk)
            if not await _send_to_peer(peer_id, _data_frame(tunnel_id, chunk)):
                break
    except Exception:
        pass
    finally:
        await _shutdown_stream(tunnel_id, "eof", notify=True)


async def dispatch_service_frame(peer_id: str, frame: dict) -> None:
    """Entry point from every transport (inbound WS / outbound WS / relay)."""
    t = str(frame.get("type") or "")
    if t == "svc_open":
        await _handle_open(peer_id, frame)
    elif t == "svc_data":
        st = _streams.get(str(frame.get("tunnel_id") or ""))
        if not st:
            return
        try:
            chunk = base64.b64decode(frame.get("b64") or "")
            # Provider's pump also shapes bytes going INTO its service.
            if st.get("role") == "provider" and st.get("transform"):
                chunk = st["transform"]("to_provider", chunk)
                if not chunk:
                    return
            st["writer"].write(chunk)
            await st["writer"].drain()
            rec = _consumer.get(st["grant_id"])
            if rec is not None and st["role"] == "consumer":
                rec["bytes"] = rec.get("bytes", 0) + len(chunk)
        except Exception:
            await _shutdown_stream(str(frame.get("tunnel_id") or ""), "write_err", notify=True)
    elif t == "svc_close":
        await _shutdown_stream(str(frame.get("tunnel_id") or ""), "remote_close", notify=False)


async def _shutdown_stream(tunnel_id, reason, notify):
    st = _streams.pop(tunnel_id, None)
    if not st:
        return
    if notify:
        try:
            await _send_to_peer(st["peer_id"], _close_frame(tunnel_id, reason))
        except Exception:
            pass
    try:
        st["writer"].close()
    except Exception:
        pass
    rec = _consumer.get(st.get("grant_id"))
    if rec is not None:
        rec["tunnel_ids"].discard(tunnel_id)


async def close_grant_streams(grant_id: str) -> None:
    """Revoke hook: cut every live stream for *grant_id* (both roles)."""
    for tid, st in list(_streams.items()):
        if st.get("grant_id") == grant_id:
            await _shutdown_stream(tid, "revoked", notify=True)
    rec = _consumer.get(grant_id)
    if rec and rec.get("listeners"):
        await disconnect_tunnel(grant_id)


__all__ = [
    "open_tunnel", "disconnect_tunnel", "tunnel_status",
    "dispatch_service_frame", "close_grant_streams", "register_pump",
]
