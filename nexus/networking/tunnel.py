"""TCP-over-WebSocket tunnel.

Pumps bytes between a master-local TCP listener (``127.0.0.1:N``) and a
worker container's exposed port. The pump rides on the already-
authenticated peer connection: direct ``/peer/ws`` on LAN and the relay
frame protocol off-LAN.

Three frame types ride the existing transports:

* ``tunnel_open``  — master → worker, request a new TCP connection.
* ``tunnel_data``  — bidirectional, base64-encoded chunks.
* ``tunnel_close`` — either side; close + reason.

The pump is a single source of truth for both transports. Higher-level
code (``api/websocket.py``, ``networking/worker_client.py``,
``networking/relay_client.py``) just dispatches frames to the handlers
exported here.

Auth: ownership is checked on ``tunnel_open`` against the task's
``NEXUS_META_REQUESTED_BY`` so a peer cannot tunnel into a service it
doesn't own. The WS connection itself is already trusted-peer
authenticated (fix 2.2).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any

from nexus.core import STATE

_log = logging.getLogger("nexus.networking.tunnel")

CHUNK_BYTES = 32 * 1024  # 32 KB → ~43 KB after base64, fits 64 KB ws floor
HIGH_WATER_BYTES = 4 * 1024 * 1024  # 4 MB pending → pause reads
PUMP_BUFFER_DRAIN_SLEEP = 0.01


# ---------------------------------------------------------------------------
# Per-tunnel rate limiter (Tier 3, item 5a.3.1)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Cooperative token-bucket. ``rate_bytes_per_sec`` 0 = no limit."""

    __slots__ = ("rate", "tokens", "last", "burst")

    def __init__(self, rate_bytes_per_sec: int) -> None:
        self.rate = max(0, int(rate_bytes_per_sec))
        # Burst = 1 second of capacity; min 64 KB so small rates don't stall.
        self.burst = max(64 * 1024, self.rate)
        self.tokens = float(self.burst)
        self.last = time.monotonic()

    async def acquire(self, n: int) -> None:
        if self.rate <= 0:
            return
        while True:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep(max(0.005, (n - self.tokens) / self.rate))


def _rate_limiter_for_task(task_id: str) -> _TokenBucket | None:
    rec = STATE.service_records.get(task_id) or {}
    rate_mbps = int(rec.get("rate_limit_mb_s", 0) or 0)
    if rate_mbps <= 0:
        return None
    rate_bps = rate_mbps * 1024 * 1024
    bucket = STATE.service_rate_buckets.get(task_id)
    if bucket is None or bucket.rate != rate_bps:
        bucket = _TokenBucket(rate_bps)
        STATE.service_rate_buckets[task_id] = bucket
    return bucket


# ---------------------------------------------------------------------------
# HTTP inspector ring (Tier 3, item 5a.3.4)
# ---------------------------------------------------------------------------

_HTTP_RING_SIZE = 100
_HTTP_SNIFF_BYTES = 8 * 1024
_REPLAY_BUFFER_BYTES = 1024 * 1024  # 1 MB cap per task


def _session_replay_observe(task_id: str, direction: str, chunk: bytes) -> None:
    """Append *chunk* to the per-task replay ring (1 MB cap)."""
    rec = STATE.service_records.get(task_id) or {}
    if not rec.get("session_replay"):
        return
    state = STATE.service_replay_buffers.get(task_id)
    if state is None:
        state = {"entries": [], "total": 0}
        STATE.service_replay_buffers[task_id] = state
    state["entries"].append((time.time(), direction, chunk))
    state["total"] += len(chunk)
    while state["total"] > _REPLAY_BUFFER_BYTES and state["entries"]:
        _ts, _dir, old = state["entries"].pop(0)
        state["total"] -= len(old)


def _http_inspector_observe(task_id: str, direction: str, chunk: bytes) -> None:
    """Sniff the first few KB of each TCP burst and record an entry.

    Best-effort: parses request line / status line + headers from the head
    of the chunk. Drops silently on anything that doesn't look like HTTP.
    """
    from collections import deque

    head = chunk[:_HTTP_SNIFF_BYTES]
    try:
        text = head.decode("latin-1", errors="ignore")
    except Exception:
        return
    first_line, _, _rest = text.partition("\r\n")
    if not first_line:
        return

    entry: dict | None = None
    if direction == "to_worker" and " HTTP/" in first_line:
        method, _, rest = first_line.partition(" ")
        path, _, _proto = rest.partition(" ")
        if method and path:
            entry = {
                "ts": time.time(),
                "kind": "request",
                "method": method,
                "path": path,
            }
    elif direction == "to_master" and first_line.startswith("HTTP/"):
        proto, _, rest = first_line.partition(" ")
        code, _, reason = rest.partition(" ")
        if code.isdigit():
            entry = {
                "ts": time.time(),
                "kind": "response",
                "status": int(code),
                "reason": reason.strip(),
            }
    if entry is None:
        return

    ring = STATE.service_http_inspector.get(task_id)
    if ring is None:
        ring = deque(maxlen=_HTTP_RING_SIZE)
        STATE.service_http_inspector[task_id] = ring
    ring.append(entry)


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def build_tunnel_open(tunnel_id: str, task_id: str, port: int) -> dict:
    return {
        "type": "tunnel_open",
        "tunnel_id": tunnel_id,
        "task_id": task_id,
        "port": int(port),
    }


def build_tunnel_data(tunnel_id: str, direction: str, chunk: bytes) -> dict:
    return {
        "type": "tunnel_data",
        "tunnel_id": tunnel_id,
        "dir": direction,  # "to_worker" | "to_master"
        "b64": base64.b64encode(chunk).decode("ascii"),
    }


def build_tunnel_close(tunnel_id: str, reason: str) -> dict:
    return {"type": "tunnel_close", "tunnel_id": tunnel_id, "reason": reason}


def decode_data_frame(frame: dict) -> bytes:
    raw = frame.get("b64") or ""
    if not raw:
        return b""
    return base64.b64decode(raw)


# (5a.4.3): UDP datagram tunnel frames.
def build_tunnel_udp_send(
    udp_id: str, task_id: str, port: int, datagram: bytes
) -> dict:
    return {
        "type": "tunnel_udp_send",
        "udp_id": udp_id,
        "task_id": task_id,
        "port": int(port),
        "b64": base64.b64encode(datagram).decode("ascii"),
    }


def build_tunnel_udp_recv(udp_id: str, datagram: bytes) -> dict:
    return {
        "type": "tunnel_udp_recv",
        "udp_id": udp_id,
        "b64": base64.b64encode(datagram).decode("ascii"),
    }


# ---------------------------------------------------------------------------
# Transport — pick LAN WS or Relay automatically
# ---------------------------------------------------------------------------

async def _send_to_peer(peer_id: str, frame: dict) -> bool:
    """Send *frame* to *peer_id* over LAN if connected, else relay.

    Tries the master-inbound side first (we are master, peer is worker),
    then the worker-outbound side (we are worker, peer is master), then
    the relay, then HTTP /peer/storage_inbox for storage_* frames.
    Returns True on the first transport that accepts.

    Diagnostic side-channel: every attempt result (success or failure)
    is appended to ``STATE.last_send_attempts[peer_id]`` so the
    foreign-storage deposit endpoint can surface a useful error message
    instead of an opaque 503.
    """
    attempts: list[str] = []

    # Path A: I am the master, peer is a worker connected via /peer/ws.
    inbound = STATE.inbound_peer_ws.get(peer_id)
    if inbound is not None:
        try:
            await inbound.send_json(frame)
            attempts.append("ws-inbound: ok")
            _record_send_attempts(peer_id, attempts)
            return True
        except Exception as exc:
            attempts.append(f"ws-inbound: {exc!r}")
            _log.debug("inbound send to %s failed: %s", peer_id, exc)
    else:
        attempts.append("ws-inbound: no socket")

    # Path B: I am the worker, peer is one of my masters (outbound WS).
    outbound = STATE.outbound_master_ws.get(peer_id)
    if outbound is not None:
        try:
            await outbound.send(json.dumps(frame))
            attempts.append("ws-outbound: ok")
            _record_send_attempts(peer_id, attempts)
            return True
        except Exception as exc:
            attempts.append(f"ws-outbound: {exc!r}")
            _log.debug("outbound send to %s failed: %s", peer_id, exc)
    else:
        attempts.append("ws-outbound: no socket")

    # Path C: Relay fallback. The relay routes by node_uuid; we wrap our
    # frame as a relay-routed payload (mirrors the http_request/http_response
    # convention at relay_client.py:265-285).
    try:
        from nexus.core.identity import resolve_ip_to_uuid
        from nexus.networking.relay_client import relay_send_to_peer

        target = resolve_ip_to_uuid(peer_id) or peer_id
        if STATE.relay_connected and target in STATE.relay_peers:
            ok = await relay_send_to_peer(target, frame)
            attempts.append(f"relay: {'ok' if ok else 'rejected'}")
            if ok:
                _record_send_attempts(peer_id, attempts)
                return True
        else:
            attempts.append("relay: not connected")
    except Exception as exc:
        attempts.append(f"relay: {exc!r}")
        _log.debug("relay send to %s failed: %s", peer_id, exc)

    # Path D : HTTP fallback for storage_* frames between trusted
    # peers that don't have a WS open in either direction. Lets foreign-
    # storage deposits work for plain pairing relationships (no dual mode).
    try:
        ftype = str(frame.get("type") or "")
        if ftype.startswith("storage_"):
            ok, reason = await _send_storage_frame_http(peer_id, frame)
            attempts.append(f"http-fallback: {reason}")
            _record_send_attempts(peer_id, attempts)
            return ok
        else:
            attempts.append("http-fallback: skipped (non-storage frame)")
    except Exception as exc:
        attempts.append(f"http-fallback: {exc!r}")
        _log.debug("http fallback to %s failed: %s", peer_id, exc)
    _record_send_attempts(peer_id, attempts)
    return False


def _record_send_attempts(peer_id: str, attempts: list[str]) -> None:
    """Stash recent transport attempts so error-surfacing endpoints can
    explain *why* a frame couldn't be delivered. Bounded to 16 peers to
    avoid leaking memory in long-running nodes."""
    cache = getattr(STATE, "last_send_attempts", None)
    if cache is None:
        STATE.last_send_attempts = {}  # type: ignore[attr-defined]
        cache = STATE.last_send_attempts
    cache[peer_id] = attempts
    if len(cache) > 16:
        # Drop the oldest entry (insertion order).
        first = next(iter(cache))
        cache.pop(first, None)


async def _send_storage_frame_http(peer_id: str, frame: dict) -> tuple[bool, str]:
    """POST a storage_* frame to a trusted peer's /peer/storage_inbox.

    Returns ``(ok, reason)`` so the caller can surface a useful error.
    """
    import httpx
    from sqlalchemy import select

    from nexus.core.identity import get_node_identity, resolve_uuid_to_ip
    from nexus.networking.worker_client import _peer_request
    from nexus.storage import Peer, get_session

    trusted_statuses = ["trusted", "trusted_pending_in", "trusted_pending_out"]
    target_norm = (peer_id or "").strip()
    async with get_session() as db:
        existing = (
            (
                await db.execute(
                    select(Peer).filter(Peer.status.in_(trusted_statuses))
                )
            )
            .scalars()
            .all()
        )
        # Python-side match so we can normalize whitespace/case and match
        # against ip, resolved_ip, or display_name. SQLAlchemy filters are
        # exact-match and miss values stored with a trailing newline or
        # mismatched case.
        peer = None
        for p in existing:
            ip_norm = (p.ip or "").strip()
            r_norm = (p.resolved_ip or "").strip()
            dn_norm = (p.display_name or "").strip()
            if (
                ip_norm == target_norm
                or r_norm == target_norm
                or (dn_norm and dn_norm == target_norm)
                or ip_norm.lower() == target_norm.lower()
            ):
                peer = p
                break
        if peer is None:
            tags = [
                f"ip={p.ip!r} resolved={p.resolved_ip!r} status={p.status!r}"
                for p in existing[:8]
            ]
            return False, (
                f"peer row not found for target_peer={peer_id!r} "
                f"(trusted peers in DB: [{'; '.join(tags) or 'none'}])"
            )
    if not peer.their_auth_token:
        return False, "no their_auth_token in peer row (re-pair the connection)"
    resolved = resolve_uuid_to_ip(peer_id)
    if resolved == peer_id:
        resolved = peer.resolved_ip or peer_id
    if resolved == peer_id:
        return False, f"could not resolve {peer_id[:16]}... to an IP:port"
    headers = {
        "X-Cluster-Key": peer.their_auth_token,
        "X-Node-Address": get_node_identity(),
    }
    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            res = await _peer_request(
                client, "POST", peer_id, resolved,
                "/peer/storage_inbox", json=frame, headers=headers,
            )
            if res.status_code == 200:
                return True, "ok"
            return False, f"POST /peer/storage_inbox -> {res.status_code} (peer may be on an older build without this endpoint)"
    except Exception as exc:
        _log.debug("storage_inbox POST to %s failed: %s", peer_id, exc)
        return False, f"transport error: {exc!r}"


# ---------------------------------------------------------------------------
# Master side: per-task local TCP listener + per-stream pump
# ---------------------------------------------------------------------------

async def ensure_local_listener(task_id: str, peer_id: str) -> int:
    """Idempotently open ``127.0.0.1:0`` for a service. Return the bound port.

    Subsequent calls return the cached port. The listener stays open until
    :func:`close_local_listener` is called. Wraps :func:`ensure_local_listeners`
    and returns the host port of the first manifest-declared container port.
    """
    mapping = await ensure_local_listeners(task_id, peer_id)
    if not mapping:
        raise RuntimeError(
            f"ensure_local_listener: no exposed ports for {task_id}"
        )
    first_container = sorted(mapping.keys())[0]
    return int(mapping[first_container])


async def ensure_local_listeners(
    task_id: str, peer_id: str
) -> dict[int, int]:
    """Open one ``127.0.0.1:0`` listener per manifest container port.

    Returns ``{container_port: host_port}``. Idempotent: existing listeners
    are reused. Each accept handler tunnels to the matching container port.
    when service record sets ``tls_terminate=True`` the
    listener is wrapped with the node's self-signed cert.
    """
    rec = STATE.service_records.get(task_id) or {}
    raw_ports = rec.get("expose_ports") or []
    container_ports: list[int] = []
    for p in raw_ports:
        try:
            container_ports.append(int(p))
        except (TypeError, ValueError):
            continue
    if not container_ports:
        return {}

    ssl_ctx = None
    if rec.get("tls_terminate"):
        try:
            import ssl

            from nexus.security.tls import ensure_local_cert

            cert, key = ensure_local_cert()
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        except Exception as exc:
            _log.warning(
                "[tunnel] tls_terminate requested for %s but cert load failed: %s",
                task_id,
                exc,
            )
            ssl_ctx = None

    async with STATE.service_lock:
        existing = STATE.service_tunnels.get(task_id)
        listeners = (existing or {}).get("listeners") or {}

    out: dict[int, int] = {}
    for cport in container_ports:
        if cport in listeners and listeners[cport].get("server"):
            out[cport] = int(listeners[cport]["host_port"])
            continue

        def _accept_factory(captured_port: int):
            async def _on_accept(reader, writer):
                await _master_handle_local_client(
                    task_id, peer_id, reader, writer, captured_port
                )
            return _on_accept

        server = await asyncio.start_server(
            _accept_factory(cport),
            host="127.0.0.1",
            port=0,
            ssl=ssl_ctx,
        )
        bound_port = server.sockets[0].getsockname()[1]

        async with STATE.service_lock:
            tunnels = STATE.service_tunnels.setdefault(
                task_id,
                {"peer_id": peer_id, "streams": {}, "listeners": {}},
            )
            tunnels["peer_id"] = peer_id
            tunnels.setdefault("streams", {})
            tunnels.setdefault("listeners", {})
            tunnels["listeners"][cport] = {
                "server": server,
                "host_port": bound_port,
            }
            # Backward-compat: keep the first listener's port at the top
            # level so legacy callers (UI, _first_exposed_port) still work.
            first_cport = sorted(tunnels["listeners"].keys())[0]
            tunnels["server"] = tunnels["listeners"][first_cport]["server"]
            tunnels["port"] = tunnels["listeners"][first_cport]["host_port"]

        _log.info(
            "[tunnel] master listener %s/%d on 127.0.0.1:%d -> peer %s",
            task_id,
            cport,
            bound_port,
            peer_id,
        )
        out[cport] = bound_port

    return out


async def close_local_listener(task_id: str) -> None:
    async with STATE.service_lock:
        rec = STATE.service_tunnels.pop(task_id, None)
    if not rec:
        return
    listeners = rec.get("listeners") or {}
    for entry in listeners.values():
        server = entry.get("server")
        if server is None:
            continue
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
    # Legacy single-listener fallback in case no `listeners` mapping was set.
    if not listeners:
        server = rec.get("server")
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
    for tunnel_id, stream in list(rec.get("streams", {}).items()):
        await _close_stream(stream, "listener_closed")


async def ensure_dependency_tunnel(
    dep_task_id: str,
    dep_primary: str,
    container_port: int,
) -> int:
    """Worker-side dep tunnel listener.

    The current node is consuming service *dep_task_id* (which lives on
    *dep_primary*). Open a ``127.0.0.1:0`` listener on this node that
    forwards each accepted TCP connection to *dep_primary*'s container
    port via the existing tunnel pump. Returns the bound local port.

    If a listener already exists for this dep, reroute it to *dep_primary*
    instead of opening a new one — this is the path failover takes when
    ``service_dep_changed`` arrives.
    """
    async with STATE.service_lock:
        STATE.service_records.setdefault(
            dep_task_id,
            {
                "task_id": dep_task_id,
                "expose_ports": [int(container_port)],
                "master_ip": "",
                "worker_id": dep_primary,
            },
        )
        STATE.service_records[dep_task_id]["expose_ports"] = [int(container_port)]

    existing = STATE.service_tunnels.get(dep_task_id)
    if existing and existing.get("server"):
        if existing.get("peer_id") != dep_primary:
            await reroute_tunnel(dep_task_id, dep_primary)
        return int(existing["port"])

    return await ensure_local_listener(dep_task_id, dep_primary)


async def reroute_tunnel(task_id: str, new_peer_id: str) -> int:
    """Repoint the listener for *task_id* at *new_peer_id*.

    Drops every open stream (clients see a TCP reset and must reconnect),
    keeps the listener bound on the same local port, and updates the cached
    ``peer_id`` so future local clients dial the promoted worker. Returns
    the count of streams that were closed.
    """
    async with STATE.service_lock:
        rec = STATE.service_tunnels.get(task_id)
        if rec is None:
            return 0
        old_peer = rec.get("peer_id", "")
        rec["peer_id"] = new_peer_id
        streams = list(rec.get("streams", {}).items())
        rec["streams"] = {}

    for tunnel_id, stream in streams:
        # Tell the (former) primary its half of the stream is dead, so it
        # closes its container-side socket. Best-effort: if it's offline,
        # we don't care.
        if old_peer:
            try:
                await _send_to_peer(
                    old_peer, build_tunnel_close(tunnel_id, "rerouted")
                )
            except Exception:
                pass
        await _close_stream(stream, "rerouted")

    _log.info(
        "[tunnel] rerouted %s: %s -> %s (closed %d streams)",
        task_id,
        old_peer,
        new_peer_id,
        len(streams),
    )
    return len(streams)


async def _master_handle_local_client(
    task_id: str,
    peer_id: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    container_port: int | None = None,
) -> None:
    """A local TCP client connected; create a tunnel and pump bytes."""
    tunnel_id = uuid.uuid4().hex
    stream = {
        "side": "master",
        "task_id": task_id,
        "peer_id": peer_id,
        "writer": writer,
        "pending_bytes": 0,
        "created_at": time.time(),
    }
    async with STATE.service_lock:
        rec = STATE.service_tunnels.get(task_id)
        if rec is None:
            writer.close()
            return
        rec["streams"][tunnel_id] = stream

    # Each listener captures its own container_port. Legacy
    # callers (no captured port) fall back to the manifest's first port.
    if container_port is None:
        container_port = _first_exposed_port(task_id)
    if container_port is None:
        await _close_stream(stream, "no_port")
        async with STATE.service_lock:
            STATE.service_tunnels[task_id]["streams"].pop(tunnel_id, None)
        return

    open_frame = build_tunnel_open(tunnel_id, task_id, container_port)
    if not await _send_to_peer(peer_id, open_frame):
        await _close_stream(stream, "peer_unreachable")
        async with STATE.service_lock:
            STATE.service_tunnels[task_id]["streams"].pop(tunnel_id, None)
        return

    # Pump local socket → tunnel_data frames.
    pump = asyncio.create_task(
        _pump_local_to_remote(
            tunnel_id, task_id, peer_id, reader, "to_worker"
        ),
        name=f"nexus.tunnel.master.pump.{tunnel_id}",
    )
    stream["pump_task"] = pump
    try:
        await pump
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _log.warning("[tunnel] master pump %s ended: %s", tunnel_id, exc)


def _first_exposed_port(task_id: str) -> int | None:
    """Return the first exposed (container) port from the service record."""
    rec = STATE.service_records.get(task_id)
    if not rec:
        return None
    ports = rec.get("expose_ports") or []
    return int(ports[0]) if ports else None


async def handle_master_tunnel_data(frame: dict) -> None:
    """Master received bytes from the worker — write to the local socket."""
    tunnel_id = str(frame.get("tunnel_id") or "")
    if not tunnel_id:
        return
    stream = _find_stream(tunnel_id, side="master")
    if stream is None:
        return
    chunk = decode_data_frame(frame)
    writer = stream.get("writer")
    if writer is None:
        return
    try:
        writer.write(chunk)
        await writer.drain()
    except Exception as exc:
        _log.debug("[tunnel] master write %s failed: %s", tunnel_id, exc)
        await _close_stream(stream, "write_error")
        await _drop_stream(tunnel_id, side="master")
        await _send_to_peer(
            stream["peer_id"], build_tunnel_close(tunnel_id, "write_error")
        )


async def handle_master_tunnel_close(frame: dict) -> None:
    tunnel_id = str(frame.get("tunnel_id") or "")
    stream = _find_stream(tunnel_id, side="master")
    if not stream:
        return
    await _close_stream(stream, str(frame.get("reason") or "peer_close"))
    await _drop_stream(tunnel_id, side="master")


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

async def handle_worker_tunnel_open(peer_id: str, frame: dict) -> None:
    """Worker got a tunnel_open from a master — dial the container."""
    tunnel_id = str(frame.get("tunnel_id") or "")
    task_id = str(frame.get("task_id") or "")
    container_port = int(frame.get("port") or 0)

    # Ownership check: only the trusted peer that requested this task may tunnel.
    if not _peer_owns_service(peer_id, task_id):
        await _send_to_peer(peer_id, build_tunnel_close(tunnel_id, "unauthorized_owner"))
        return

    port_map = STATE.service_port_mappings.get(task_id, {})
    host_port = port_map.get(container_port)
    if not host_port:
        await _send_to_peer(peer_id, build_tunnel_close(tunnel_id, "no_port"))
        return

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", host_port), timeout=5.0
        )
    except (OSError, asyncio.TimeoutError) as exc:
        _log.warning("[tunnel] worker dial %s failed: %s", task_id, exc)
        await _send_to_peer(peer_id, build_tunnel_close(tunnel_id, "dial_failed"))
        return

    stream = {
        "side": "worker",
        "task_id": task_id,
        "peer_id": peer_id,
        "writer": writer,
        "pending_bytes": 0,
        "created_at": time.time(),
    }
    async with STATE.service_lock:
        rec = STATE.service_tunnels.setdefault(
            task_id, {"streams": {}, "peer_id": peer_id}
        )
        rec["streams"][tunnel_id] = stream

    pump = asyncio.create_task(
        _pump_local_to_remote(tunnel_id, task_id, peer_id, reader, "to_master"),
        name=f"nexus.tunnel.worker.pump.{tunnel_id}",
    )
    stream["pump_task"] = pump


async def handle_worker_tunnel_data(frame: dict) -> None:
    """Worker received bytes from the master — write to the container socket."""
    tunnel_id = str(frame.get("tunnel_id") or "")
    stream = _find_stream(tunnel_id, side="worker")
    if not stream:
        return
    chunk = decode_data_frame(frame)
    writer = stream.get("writer")
    if writer is None:
        return
    try:
        writer.write(chunk)
        await writer.drain()
    except Exception as exc:
        _log.debug("[tunnel] worker write %s failed: %s", tunnel_id, exc)
        await _close_stream(stream, "write_error")
        await _drop_stream(tunnel_id, side="worker")
        await _send_to_peer(
            stream["peer_id"], build_tunnel_close(tunnel_id, "write_error")
        )


async def handle_worker_tunnel_close(frame: dict) -> None:
    tunnel_id = str(frame.get("tunnel_id") or "")
    stream = _find_stream(tunnel_id, side="worker")
    if not stream:
        return
    await _close_stream(stream, str(frame.get("reason") or "peer_close"))
    await _drop_stream(tunnel_id, side="worker")


# ---------------------------------------------------------------------------
# Shared pump
# ---------------------------------------------------------------------------

async def _pump_local_to_remote(
    tunnel_id: str,
    task_id: str,
    peer_id: str,
    reader: asyncio.StreamReader,
    direction: str,
) -> None:
    """Read from the local TCP socket, ship as ``tunnel_data`` frames."""
    bucket = _rate_limiter_for_task(task_id)
    try:
        while True:
            chunk = await reader.read(CHUNK_BYTES)
            if not chunk:
                # Local side EOF — tell the peer we're done.
                await _send_to_peer(
                    peer_id, build_tunnel_close(tunnel_id, "local_eof")
                )
                break

            if bucket is not None:
                await bucket.acquire(len(chunk))

            # Backpressure: spin until pending_bytes is below the cap.
            stream = _find_stream(
                tunnel_id, side="master" if direction == "to_worker" else "worker"
            )
            if stream is None:
                break
            while stream.get("pending_bytes", 0) > HIGH_WATER_BYTES:
                await asyncio.sleep(PUMP_BUFFER_DRAIN_SLEEP)
                stream = _find_stream(
                    tunnel_id,
                    side="master" if direction == "to_worker" else "worker",
                )
                if stream is None:
                    return

            stream["pending_bytes"] = stream.get("pending_bytes", 0) + len(chunk)
            sent = await _send_to_peer(
                peer_id, build_tunnel_data(tunnel_id, direction, chunk)
            )
            stream["pending_bytes"] = max(0, stream["pending_bytes"] - len(chunk))
            if not sent:
                break
            # HTTP inspector tap.
            if STATE.service_records.get(task_id, {}).get("service_kind") == "http":
                _http_inspector_observe(task_id, direction, chunk)
            # (5a.4.1): session-replay tap.
            _session_replay_observe(task_id, direction, chunk)
            # Update activity for idle-timeout watchdog (worker-local task).
            if task_id in STATE.service_last_activity:
                STATE.service_last_activity[task_id] = time.time()
    finally:
        await _drop_stream(
            tunnel_id, side="master" if direction == "to_worker" else "worker"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_stream(tunnel_id: str, *, side: str) -> dict | None:
    if not tunnel_id:
        return None
    for rec in STATE.service_tunnels.values():
        streams = rec.get("streams") or {}
        stream = streams.get(tunnel_id)
        if stream and stream.get("side") == side:
            return stream
    return None


async def _drop_stream(tunnel_id: str, *, side: str) -> None:
    async with STATE.service_lock:
        for rec in STATE.service_tunnels.values():
            streams = rec.get("streams") or {}
            if tunnel_id in streams and streams[tunnel_id].get("side") == side:
                streams.pop(tunnel_id, None)
                return


async def _close_stream(stream: dict, reason: str) -> None:
    pump: asyncio.Task | None = stream.get("pump_task")
    if pump and not pump.done():
        pump.cancel()
    writer = stream.get("writer")
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass
        try:
            await writer.wait_closed()
        except Exception:
            pass
    stream["closed_reason"] = reason


# ---------------------------------------------------------------------------
# UDP datagram tunnel (Tier 4, item 5a.4.3)
# ---------------------------------------------------------------------------

class _MasterUdpProtocol(asyncio.DatagramProtocol):
    """Master-side UDP listener. Each inbound datagram → ``tunnel_udp_send``.

    The reply path is driven by ``handle_master_tunnel_udp_recv`` which
    looks up the source address by ``udp_id``.
    """

    def __init__(self, task_id: str, peer_id: str, container_port: int) -> None:
        self.task_id = task_id
        self.peer_id = peer_id
        self.container_port = container_port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        udp_id = uuid.uuid4().hex
        rec = STATE.service_udp_listeners.get(self.task_id)
        if rec is None:
            return
        rec.setdefault("pending", {})[udp_id] = (addr, time.time() + 5.0)
        # Best-effort send. UDP semantics: drop on overflow.
        asyncio.create_task(
            _send_to_peer(
                self.peer_id,
                build_tunnel_udp_send(
                    udp_id, self.task_id, self.container_port, data
                ),
            )
        )


async def ensure_local_udp_listener(
    task_id: str, peer_id: str, container_port: int
) -> int:
    """Bind a 127.0.0.1:0 UDP socket for *task_id*. Returns the host port."""
    existing = STATE.service_udp_listeners.get(task_id)
    if existing and existing.get("transport"):
        return int(existing["host_port"])

    loop = asyncio.get_running_loop()
    transport, _proto = await loop.create_datagram_endpoint(
        lambda: _MasterUdpProtocol(task_id, peer_id, container_port),
        local_addr=("127.0.0.1", 0),
    )
    host_port = transport.get_extra_info("sockname")[1]
    STATE.service_udp_listeners[task_id] = {
        "transport": transport,
        "host_port": host_port,
        "peer_id": peer_id,
        "container_port": container_port,
        "pending": {},
    }
    _log.info(
        "[tunnel-udp] master listener for %s on 127.0.0.1:%d -> peer %s",
        task_id,
        host_port,
        peer_id,
    )
    return host_port


async def close_local_udp_listener(task_id: str) -> None:
    rec = STATE.service_udp_listeners.pop(task_id, None)
    if not rec:
        return
    transport = rec.get("transport")
    if transport is not None:
        transport.close()


async def handle_worker_tunnel_udp_send(peer_id: str, frame: dict) -> None:
    """Worker received a datagram from the master — relay to the container."""
    udp_id = str(frame.get("udp_id") or "")
    task_id = str(frame.get("task_id") or "")
    container_port = int(frame.get("port") or 0)
    if not (udp_id and task_id and container_port):
        return
    if not _peer_owns_service(peer_id, task_id):
        return

    port_map = STATE.service_port_mappings.get(task_id, {})
    host_port = port_map.get(container_port)
    if not host_port:
        return
    payload = decode_data_frame(frame)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class _ClientProto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc) -> None:
            if not fut.done():
                fut.set_exception(exc)

    try:
        transport, _proto = await loop.create_datagram_endpoint(
            _ClientProto, remote_addr=("127.0.0.1", int(host_port))
        )
    except Exception as exc:
        _log.warning("[tunnel-udp] worker dial %s failed: %s", task_id, exc)
        return
    try:
        transport.sendto(payload)
        try:
            reply = await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError:
            return
        await _send_to_peer(peer_id, build_tunnel_udp_recv(udp_id, reply))
    finally:
        transport.close()


async def handle_master_tunnel_udp_recv(frame: dict) -> None:
    """Master received a UDP reply from the worker — write back to client."""
    udp_id = str(frame.get("udp_id") or "")
    if not udp_id:
        return
    payload = decode_data_frame(frame)
    for rec in STATE.service_udp_listeners.values():
        pending = rec.get("pending") or {}
        if udp_id not in pending:
            continue
        addr, _deadline = pending.pop(udp_id)
        transport = rec.get("transport")
        if transport is not None:
            try:
                transport.sendto(payload, addr)
            except Exception as exc:
                _log.debug("[tunnel-udp] sendto %s failed: %s", addr, exc)
        return


def _peer_owns_service(peer_id: str, task_id: str) -> bool:
    """Check that *peer_id* is the trusted requester of *task_id*.

    also accept peers the master has explicitly granted
    dep-tunnel access via ``service_dep_grant``.

    (5a.4.2): when the manifest sets ``shared_tunnel: true``,
    accept any trusted peer (the inbound WS auth already gates this).
    """
    rec = STATE.service_records.get(task_id)
    if not rec:
        return False
    if rec.get("master_ip") == peer_id:
        return True
    grants = STATE.service_dep_grants.get(task_id) or set()
    if peer_id in grants:
        return True
    if rec.get("shared_tunnel"):
        # Trusted-peer filter happens at the WS auth layer; if a frame
        # made it here at all, the peer is in our trusted set. Allow.
        return True
    return False


__all__ = [
    "CHUNK_BYTES",
    "HIGH_WATER_BYTES",
    "build_tunnel_open",
    "build_tunnel_data",
    "build_tunnel_close",
    "build_tunnel_udp_send",
    "build_tunnel_udp_recv",
    "decode_data_frame",
    "ensure_local_listener",
    "ensure_local_listeners",
    "ensure_local_udp_listener",
    "close_local_listener",
    "close_local_udp_listener",
    "reroute_tunnel",
    "handle_master_tunnel_data",
    "handle_master_tunnel_close",
    "handle_master_tunnel_udp_recv",
    "handle_worker_tunnel_open",
    "handle_worker_tunnel_data",
    "handle_worker_tunnel_close",
    "handle_worker_tunnel_udp_send",
]
