"""Security F-011 — foreign-storage receive_chunk bounds the index to the agreed
deposit size, so an accepted depositor can't write past it (disk-exhaustion)."""

from __future__ import annotations

import asyncio
import base64

import pytest

from nexus.core import STATE
from nexus.networking import storage_pump


@pytest.fixture
def host_pump(tmp_path, monkeypatch):
    """A host pump that agreed to host a 2-chunk deposit, with _send_to_peer
    stubbed to capture acks."""
    acks = []

    async def fake_send(peer_uuid, frame):
        acks.append(frame)

    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)
    dep = "dep-1"
    STATE.foreign_storage_pumps[dep] = {
        "role": "host", "dir": str(tmp_path), "chunk_count": 2,
    }
    yield dep, tmp_path, acks
    STATE.foreign_storage_pumps.pop(dep, None)


def _chunk_frame(dep, idx, data=b"x"):
    return {"deposit_id": dep, "chunk_idx": idx, "b64": base64.b64encode(data).decode()}


def test_in_range_chunk_written(host_pump):
    dep, tmp_path, acks = host_pump
    asyncio.run(storage_pump.receive_chunk("peer", _chunk_frame(dep, 0)))
    assert (tmp_path / "chunk_00000000.enc").exists()
    assert acks and acks[-1].get("ok") is True


def test_out_of_range_chunk_rejected_and_not_written(host_pump):
    dep, tmp_path, acks = host_pump
    # count is 2 → index 5 is beyond the agreed deposit; must be refused.
    asyncio.run(storage_pump.receive_chunk("peer", _chunk_frame(dep, 5)))
    assert not (tmp_path / "chunk_00000005.enc").exists()
    assert acks and acks[-1].get("ok") is False
    assert acks[-1].get("reason") == "out_of_range"


def test_index_equal_to_count_rejected(host_pump):
    dep, tmp_path, acks = host_pump
    # valid indices are 0..1; index == count (2) is out of range.
    asyncio.run(storage_pump.receive_chunk("peer", _chunk_frame(dep, 2)))
    assert not (tmp_path / "chunk_00000002.enc").exists()
    assert acks[-1].get("reason") == "out_of_range"
