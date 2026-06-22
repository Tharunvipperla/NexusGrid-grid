"""Auto-rescue cloud-overflow: stream ciphertext into rclone when disk full."""

from __future__ import annotations

import asyncio
import base64

import pytest
from sqlalchemy import select

from nexus.core import STATE
from nexus.runtime import foreign_storage_rclone as rcl
from nexus.runtime.foreign_storage_workflow import _handle_retrieve_chunk
from nexus.security import tokens
from nexus.storage import ForeignStorageDeposit, database, get_session


DEP_ID = "dep-overflow-1"
DEPOSITOR = "10.0.0.1:9000"
HOST = "10.0.0.2:9000"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()


@pytest.fixture(autouse=True)
def _reset():
    STATE.foreign_storage_stream_queues.clear()
    STATE.foreign_storage_auto_rescue_seen.clear()
    yield
    STATE.foreign_storage_stream_queues.clear()
    STATE.foreign_storage_auto_rescue_seen.clear()


def _seed(status="eviction_requested"):
    async def _go():
        async with get_session() as db:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEP_ID, role="depositor", depositor_uuid=DEPOSITOR,
                    host_uuid=HOST, status=status, total_bytes=30, chunk_count=3,
                    transport="stream", salt=b"\x00" * 16, ttl_days=30,
                    created_at="2026-06-15T00:00:00+00:00", depositor_signature="s",
                    filename="big.bin",
                )
            )
            await db.commit()
    asyncio.run(_go())


def _status():
    async def _go():
        async with get_session() as db:
            row = (await db.execute(select(ForeignStorageDeposit).filter(
                ForeignStorageDeposit.deposit_id == DEP_ID))).scalar_one()
            return row.status, row.cloud_dest
    return asyncio.run(_go())


# --- handler hands the chunk to the stream queue, not to disk ---------------

def test_retrieve_chunk_routes_to_stream_queue():
    q: asyncio.Queue = asyncio.Queue()
    STATE.foreign_storage_stream_queues[DEP_ID] = q
    frame = {"deposit_id": DEP_ID, "chunk_idx": 2,
             "b64": base64.b64encode(b"ciphertext").decode()}
    asyncio.run(_handle_retrieve_chunk(HOST, frame))
    assert q.get_nowait() == (2, b"ciphertext")


# --- a fake rclone proc: stdin collects bytes, wait() -> rc ----------------

class _FakeStdin:
    def __init__(self):
        self.buf = bytearray()
    def write(self, data):
        self.buf.extend(data)
    async def drain(self):
        pass
    def close(self):
        pass


class _FakeProc:
    def __init__(self, rc=0):
        self.stdin = _FakeStdin()
        self._rc = rc
    async def wait(self):
        return self._rc
    def kill(self):
        pass


def _fake_spawn(rc, captured):
    async def _spawn(*args, **kwargs):
        p = _FakeProc(rc)
        captured.append((args, p))
        return p
    return _spawn


def test_stream_one_uploads_in_order(isolated_db, monkeypatch):
    chunks = [b"AAA", b"BBBB", b"CC"]
    captured: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn(0, captured))

    sent = []
    async def fake_send(target, frame):
        sent.append(frame); return True
    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    async def _drive():
        task = asyncio.create_task(rcl._stream_one(DEP_ID, HOST, len(chunks), "gdrive:nx"))
        # Wait for the streaming task to register its queue.
        for _ in range(100):
            q = STATE.foreign_storage_stream_queues.get(DEP_ID)
            if q is not None:
                break
            await asyncio.sleep(0.005)
        assert q is not None
        # Feed chunks OUT of order to exercise the reorder buffer.
        q.put_nowait((2, chunks[2]))
        q.put_nowait((0, chunks[0]))
        q.put_nowait((1, chunks[1]))
        return await task

    ok = asyncio.run(_drive())
    assert ok is True
    assert sent and sent[0]["type"] == "storage_retrieve_open"
    _args, proc = captured[0]
    assert bytes(proc.stdin.buf) == b"".join(chunks)   # reassembled in order
    assert _args[:2] == ("rclone", "rcat")
    assert _args[2] == "gdrive:nx/" + DEP_ID + ".enc"


def test_stream_one_returns_false_on_nonzero_exit(isolated_db, monkeypatch):
    captured: list = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn(1, captured))

    async def fake_send(target, frame):
        return True
    monkeypatch.setattr("nexus.networking.tunnel._send_to_peer", fake_send)

    async def _drive():
        task = asyncio.create_task(rcl._stream_one(DEP_ID, HOST, 1, "bad:remote"))
        for _ in range(100):
            q = STATE.foreign_storage_stream_queues.get(DEP_ID)
            if q is not None:
                break
            await asyncio.sleep(0.005)
        q.put_nowait((0, b"x"))
        return await task

    assert asyncio.run(_drive()) is False


# --- orchestration: try targets in order, mark row done on first success ---

def test_overflow_rescue_falls_back_to_second_target(isolated_db, monkeypatch):
    _seed()
    calls = []
    async def fake_stream(deposit_id, host_uuid, total_chunks, target):
        calls.append(target)
        return target == "good:remote"      # first target fails, second works
    monkeypatch.setattr(rcl, "_stream_one", fake_stream)

    asyncio.run(rcl.overflow_rescue(
        DEP_ID, HOST, "big.bin", 3, ["bad:remote", "good:remote"]))

    assert calls == ["bad:remote", "good:remote"]
    status, dest = _status()
    assert status == "completed"
    assert dest == "good:remote"


def test_overflow_rescue_all_fail_marks_cloud_failed(isolated_db, monkeypatch):
    _seed()
    async def fake_stream(*a, **k):
        return False
    monkeypatch.setattr(rcl, "_stream_one", fake_stream)

    asyncio.run(rcl.overflow_rescue(DEP_ID, HOST, "big.bin", 3, ["a:b", "c:d"]))

    assert STATE.foreign_storage_auto_rescue_seen[DEP_ID] == "cloud_failed"
    status, _ = _status()
    assert status == "eviction_requested"   # unchanged — nothing was rescued
