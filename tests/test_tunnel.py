"""TCP-over-WS tunnel tests (Wave 4 Step 9b / item 3.1)."""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest

from nexus.core import STATE
from nexus.networking import tunnel
from nexus.networking.tunnel import (
    CHUNK_BYTES,
    build_tunnel_close,
    build_tunnel_data,
    build_tunnel_open,
    decode_data_frame,
    handle_master_tunnel_close,
    handle_master_tunnel_data,
    handle_worker_tunnel_close,
    handle_worker_tunnel_data,
    handle_worker_tunnel_open,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.service_records.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_tunnels.clear()
    STATE.inbound_peer_ws.clear()
    STATE.outbound_master_ws.clear()
    yield
    STATE.service_records.clear()
    STATE.service_port_mappings.clear()
    STATE.service_last_activity.clear()
    STATE.service_tunnels.clear()
    STATE.inbound_peer_ws.clear()
    STATE.outbound_master_ws.clear()


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def test_build_tunnel_open():
    f = build_tunnel_open("t1", "svc-1", 6379)
    assert f == {"type": "tunnel_open", "tunnel_id": "t1", "task_id": "svc-1", "port": 6379}


def test_build_tunnel_data_base64_round_trip():
    payload = b"hello\x00world\xff"
    f = build_tunnel_data("t1", "to_worker", payload)
    assert f["type"] == "tunnel_data"
    assert f["dir"] == "to_worker"
    assert decode_data_frame(f) == payload


def test_build_tunnel_close():
    assert build_tunnel_close("t1", "client_eof") == {
        "type": "tunnel_close",
        "tunnel_id": "t1",
        "reason": "client_eof",
    }


def test_decode_empty_frame_returns_empty_bytes():
    assert decode_data_frame({"b64": ""}) == b""
    assert decode_data_frame({}) == b""


# ---------------------------------------------------------------------------
# Worker-side: ownership check on tunnel_open
# ---------------------------------------------------------------------------

def test_worker_tunnel_open_unauthorized_owner():
    """Master B trying to tunnel into a service that master A requested."""
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "expose_ports": [6379],
        "master_ip": "master-A",
    }
    STATE.service_port_mappings["svc-1"] = {6379: 49000}

    sent_frames: list[dict] = []

    async def _send(peer_id, frame):
        sent_frames.append((peer_id, frame))
        return True

    async def _scenario():
        with patch.object(tunnel, "_send_to_peer", _send):
            await handle_worker_tunnel_open(
                "master-B",  # impostor
                build_tunnel_open("t1", "svc-1", 6379),
            )

    _run(_scenario())
    assert len(sent_frames) == 1
    assert sent_frames[0][1]["type"] == "tunnel_close"
    assert sent_frames[0][1]["reason"] == "unauthorized_owner"


def test_worker_tunnel_open_unknown_service():
    """No service_records entry → close with no_port (record check fails first)."""
    sent_frames: list[dict] = []

    async def _send(peer_id, frame):
        sent_frames.append((peer_id, frame))
        return True

    async def _scenario():
        with patch.object(tunnel, "_send_to_peer", _send):
            await handle_worker_tunnel_open(
                "master-A",
                build_tunnel_open("t1", "no-such-svc", 6379),
            )

    _run(_scenario())
    assert sent_frames[0][1]["reason"] == "unauthorized_owner"


def test_worker_tunnel_open_unmapped_port():
    """Service exists but the requested port isn't in the host-port map."""
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "expose_ports": [6379],
        "master_ip": "master-A",
    }
    STATE.service_port_mappings["svc-1"] = {6379: 49000}  # only 6379 mapped

    sent_frames: list[dict] = []

    async def _send(peer_id, frame):
        sent_frames.append((peer_id, frame))
        return True

    async def _scenario():
        with patch.object(tunnel, "_send_to_peer", _send):
            await handle_worker_tunnel_open(
                "master-A",
                build_tunnel_open("t1", "svc-1", 9999),  # not mapped
            )

    _run(_scenario())
    assert sent_frames[0][1]["reason"] == "no_port"


def test_worker_tunnel_open_dial_failure():
    """Service mapped to a port nothing is listening on → dial_failed."""
    STATE.service_records["svc-1"] = {
        "task_id": "svc-1",
        "expose_ports": [6379],
        "master_ip": "master-A",
    }
    # Pick a port that is highly unlikely to be open on 127.0.0.1.
    STATE.service_port_mappings["svc-1"] = {6379: 1}  # privileged, refused

    sent_frames: list[dict] = []

    async def _send(peer_id, frame):
        sent_frames.append((peer_id, frame))
        return True

    async def _scenario():
        with patch.object(tunnel, "_send_to_peer", _send):
            await handle_worker_tunnel_open(
                "master-A",
                build_tunnel_open("t1", "svc-1", 6379),
            )

    _run(_scenario())
    assert sent_frames[0][1]["reason"] == "dial_failed"


# ---------------------------------------------------------------------------
# Worker tunnel_open — happy path with a real local server
# ---------------------------------------------------------------------------

def test_worker_tunnel_open_dials_real_server_and_relays_data():
    """Spin a local echo server, have the worker dial it via tunnel_open.

    Verifies: end-to-end byte path from master → tunnel_data → worker write
    → echo server. Also exercises pump from echo server → worker → master.
    """
    received_from_master: asyncio.Queue = asyncio.Queue()

    async def _scenario():
        # 1. Spin up an echo server on 127.0.0.1:0
        async def echo(reader, writer):
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = server.sockets[0].getsockname()[1]

        STATE.service_records["svc-1"] = {
            "task_id": "svc-1",
            "expose_ports": [6379],
            "master_ip": "master-A",
        }
        STATE.service_port_mappings["svc-1"] = {6379: echo_port}
        STATE.service_last_activity["svc-1"] = 0.0

        # 2. Capture frames "sent to peer" — these would normally hit the master.
        async def _send(peer_id, frame):
            await received_from_master.put(frame)
            return True

        with patch.object(tunnel, "_send_to_peer", _send):
            # Worker handles tunnel_open: dials echo_port, sets up pump.
            await handle_worker_tunnel_open(
                "master-A",
                build_tunnel_open("t1", "svc-1", 6379),
            )
            # Master "sends" some data through.
            await handle_worker_tunnel_data(
                build_tunnel_data("t1", "to_worker", b"PING\n")
            )
            # Wait for the echo to bounce back.
            for _ in range(50):
                if not received_from_master.empty():
                    break
                await asyncio.sleep(0.02)
            await handle_worker_tunnel_close(build_tunnel_close("t1", "client_eof"))

        server.close()
        await server.wait_closed()

        # Drain queued frames.
        frames = []
        while not received_from_master.empty():
            frames.append(received_from_master.get_nowait())
        return frames

    frames = _run(_scenario())
    # We expect at least one "to_master" data frame containing PING.
    data_frames = [f for f in frames if f["type"] == "tunnel_data"]
    assert any(decode_data_frame(f) == b"PING\n" for f in data_frames), frames


# ---------------------------------------------------------------------------
# Master tunnel_data — write to local socket
# ---------------------------------------------------------------------------

def test_master_tunnel_data_writes_to_writer():
    """Master receives data from worker → writes to the local TCP writer."""
    written: list[bytes] = []

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            return None

    STATE.service_tunnels["svc-1"] = {
        "streams": {
            "t1": {
                "side": "master",
                "task_id": "svc-1",
                "peer_id": "worker-A",
                "writer": _FakeWriter(),
                "pending_bytes": 0,
            }
        }
    }

    async def _scenario():
        await handle_master_tunnel_data(
            build_tunnel_data("t1", "to_master", b"hello")
        )

    _run(_scenario())
    assert written == [b"hello"]


def test_master_tunnel_close_drops_stream():
    """tunnel_close removes the stream from STATE."""

    class _NoopWriter:
        def close(self):
            pass

        async def wait_closed(self):
            return None

    STATE.service_tunnels["svc-1"] = {
        "streams": {
            "t1": {
                "side": "master",
                "task_id": "svc-1",
                "peer_id": "worker-A",
                "writer": _NoopWriter(),
                "pending_bytes": 0,
            }
        }
    }

    async def _scenario():
        await handle_master_tunnel_close(build_tunnel_close("t1", "peer_eof"))

    _run(_scenario())
    assert "t1" not in STATE.service_tunnels["svc-1"]["streams"]


def test_master_tunnel_data_unknown_tunnel_is_silent():
    async def _scenario():
        # No stream registered — must not raise.
        await handle_master_tunnel_data(build_tunnel_data("nope", "to_master", b"x"))

    _run(_scenario())


# ---------------------------------------------------------------------------
# Chunk size sanity
# ---------------------------------------------------------------------------

def test_chunk_bytes_fits_under_ws_floor():
    """32 KB raw → ~43 KB base64 → fits the 64 KB ws_max_size floor."""
    raw = b"x" * CHUNK_BYTES
    b64 = base64.b64encode(raw).decode()
    overhead = len('{"type":"tunnel_data","tunnel_id":"...","dir":"to_worker","b64":""}') + len(b64)
    assert overhead < 64 * 1024
