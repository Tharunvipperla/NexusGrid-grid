"""Wave 36.H — relay pause/resume state machine.

Exercises the pause→resume cycle by stubbing out the heavy side effects
(local_relay.start/stop, relay_tunnel.is_running/stop, selfheal). The
state transitions + grace-window kill-task lifecycle are the real
behavior under test.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import relay_pause


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts with a clean module-level state dict. The
    kill_task reference is dropped — each test cancels + drains its own
    task inside its event loop before close()."""
    relay_pause._state["is_paused"] = False
    relay_pause._state["paused_at"] = 0.0
    relay_pause._state["cloudflared_killed"] = False
    relay_pause._state["kill_task"] = None
    yield
    relay_pause._state["kill_task"] = None


def _drain_kill_task(loop):
    kt = relay_pause._state.get("kill_task")
    if kt is None or kt.done():
        return
    kt.cancel()
    try:
        loop.run_until_complete(kt)
    except (asyncio.CancelledError, Exception):
        pass
    relay_pause._state["kill_task"] = None


def test_pause_rejected_when_relay_not_running(monkeypatch):
    monkeypatch.setattr("nexus.runtime.local_relay.is_running", lambda: False)
    monkeypatch.setattr("nexus.runtime.local_relay.stop", lambda: {})
    result = asyncio.new_event_loop().run_until_complete(relay_pause.pause())
    assert result["status"] == "not_running"
    assert result["is_paused"] is False


def test_pause_then_status_reports_grace_window(monkeypatch):
    monkeypatch.setattr("nexus.runtime.local_relay.is_running", lambda: True)
    monkeypatch.setattr("nexus.runtime.local_relay.stop", lambda: {})

    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(relay_pause.pause())
        assert res["status"] == "paused"
        assert res["is_paused"] is True
        assert res["grace_remaining_sec"] > 0
        assert res["cloudflared_killed"] is False
        # Calling pause again is idempotent.
        res2 = loop.run_until_complete(relay_pause.pause())
        assert res2["status"] == "already_paused"
        _drain_kill_task(loop)
    finally:
        loop.close()


def test_resume_cancels_kill_task_and_restarts_relay(monkeypatch):
    started_with = []

    def _fake_start(port, grid_key):
        started_with.append((port, grid_key))
        return {"running": True}

    monkeypatch.setattr("nexus.runtime.local_relay.is_running", lambda: True)
    monkeypatch.setattr("nexus.runtime.local_relay.stop", lambda: {})
    monkeypatch.setattr("nexus.runtime.local_relay.start", _fake_start)
    # No tunnel restart path is hit when cloudflared was not killed.
    monkeypatch.setattr(
        "nexus.runtime.relay_tunnel.is_running", lambda: True
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(relay_pause.pause())
        # Kill task should be scheduled.
        kt = relay_pause._state.get("kill_task")
        assert kt is not None and not kt.done()

        res = loop.run_until_complete(relay_pause.resume())
        assert res["status"] == "resumed"
        assert res["is_paused"] is False
        assert res["url_changed"] is False
        assert res["cloudflared_was_killed"] is False
        # Local relay was restarted.
        assert len(started_with) == 1
        # Kill task was cancelled by resume().
        kt = relay_pause._state.get("kill_task")
        assert kt is None
        _drain_kill_task(loop)
    finally:
        loop.close()


def test_resume_when_not_paused_is_a_noop(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(relay_pause.resume())
        assert res["status"] == "not_paused"
        assert res["is_paused"] is False
        _drain_kill_task(loop)
    finally:
        loop.close()


def test_status_when_idle_is_unpaused():
    snap = relay_pause.status()
    assert snap["is_paused"] is False
    assert snap["grace_remaining_sec"] == 0
    assert snap["cloudflared_killed"] is False
