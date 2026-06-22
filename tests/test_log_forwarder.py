"""Batch D1 — worker→master live log forwarding."""

from __future__ import annotations

import asyncio

import pytest

from nexus.networking import log_forwarder


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_forwarder():
    log_forwarder.reset_for_testing()
    yield
    log_forwarder.reset_for_testing()


def test_enqueue_without_target_is_noop():
    log_forwarder.enqueue_chunk("nope", "hello\n")
    assert "nope" not in log_forwarder._TARGETS


def test_enqueue_drops_empty_chunk():
    log_forwarder.register_target("t1", "192.168.1.10", "tok-1")
    log_forwarder.enqueue_chunk("t1", "")
    assert log_forwarder._TARGETS["t1"].pending == []


def test_register_then_unregister_clears_state():
    log_forwarder.register_target("t2", "192.168.1.11", "tok-2")
    assert "t2" in log_forwarder._TARGETS
    log_forwarder.unregister_target("t2")
    assert "t2" not in log_forwarder._TARGETS


def test_register_rejects_missing_fields():
    log_forwarder.register_target("", "192.168.1.10", "tok")
    log_forwarder.register_target("t3", "", "tok")
    log_forwarder.register_target("t4", "192.168.1.10", "")
    assert log_forwarder._TARGETS == {}


def test_enqueued_chunks_post_to_master(monkeypatch):
    posted: list[tuple[str, str, str, str]] = []

    async def fake_post(master_ip, token, task_id, chunk):
        posted.append((master_ip, token, task_id, chunk))

    monkeypatch.setattr(log_forwarder, "_post_chunk", fake_post)
    monkeypatch.setattr(log_forwarder, "_FLUSH_INTERVAL_S", 0.01)

    async def scenario():
        log_forwarder.register_target("task-A", "10.0.0.1", "secret")
        log_forwarder.enqueue_chunk("task-A", "line 1\n")
        log_forwarder.enqueue_chunk("task-A", "line 2\n")
        await asyncio.sleep(0.05)

    _run(scenario())

    assert len(posted) == 1
    master_ip, token, task_id, chunk = posted[0]
    assert master_ip == "10.0.0.1"
    assert token == "secret"
    assert task_id == "task-A"
    assert chunk == "line 1\nline 2\n"


def test_large_chunk_triggers_early_flush(monkeypatch):
    posted: list[str] = []

    async def fake_post(master_ip, token, task_id, chunk):
        posted.append(chunk)

    monkeypatch.setattr(log_forwarder, "_post_chunk", fake_post)
    monkeypatch.setattr(log_forwarder, "_FLUSH_INTERVAL_S", 5.0)
    monkeypatch.setattr(log_forwarder, "_EARLY_FLUSH_BYTES", 16)

    async def scenario():
        log_forwarder.register_target("task-B", "10.0.0.2", "tok")
        log_forwarder.enqueue_chunk("task-B", "0123456789ABCDEF__overflow")
        await asyncio.sleep(0.05)

    _run(scenario())
    assert posted == ["0123456789ABCDEF__overflow"]


def test_unregister_cancels_pending_flush(monkeypatch):
    posted: list[str] = []

    async def fake_post(master_ip, token, task_id, chunk):
        posted.append(chunk)

    monkeypatch.setattr(log_forwarder, "_post_chunk", fake_post)
    monkeypatch.setattr(log_forwarder, "_FLUSH_INTERVAL_S", 0.5)

    async def scenario():
        log_forwarder.register_target("task-C", "10.0.0.3", "tok")
        log_forwarder.enqueue_chunk("task-C", "buffered text\n")
        log_forwarder.unregister_target("task-C")
        await asyncio.sleep(0.1)

    _run(scenario())
    assert posted == []


def test_post_failure_does_not_crash_loop(monkeypatch):
    calls: list[str] = []

    async def fake_post(master_ip, token, task_id, chunk):
        calls.append(chunk)
        raise RuntimeError("network down")

    monkeypatch.setattr(log_forwarder, "_post_chunk", fake_post)
    monkeypatch.setattr(log_forwarder, "_FLUSH_INTERVAL_S", 0.01)

    async def scenario():
        log_forwarder.register_target("task-D", "10.0.0.4", "tok")
        log_forwarder.enqueue_chunk("task-D", "a\n")
        await asyncio.sleep(0.05)

    _run(scenario())
    assert calls == ["a\n"]
