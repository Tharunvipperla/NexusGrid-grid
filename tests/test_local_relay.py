"""Wave 26 — in-process local relay manager.

Exercises :mod:`nexus.runtime.local_relay`: LAN-address detection,
idempotent start, and a real start → serve → stop round trip.
"""

from __future__ import annotations

import socket
import time

import httpx
import pytest

from nexus.runtime import local_relay


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_relay(port: int, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            res = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
            if res.status_code == 200:
                return res.json()
        except Exception as exc:  # not up yet
            last_err = exc
        time.sleep(0.2)
    raise AssertionError(f"relay did not come up on {port}: {last_err}")


@pytest.fixture(autouse=True)
def _always_stopped():
    """Guarantee the singleton relay is stopped before and after each test."""
    local_relay.stop()
    yield
    local_relay.stop()


def test_status_when_stopped():
    st = local_relay.status()
    assert st["running"] is False
    assert st["suggested_url"] == ""
    assert st["port"] == local_relay.DEFAULT_RELAY_PORT


def test_is_lan_only_detection():
    assert local_relay._is_lan_only("192.168.1.10") is True
    assert local_relay._is_lan_only("10.0.0.5") is True
    assert local_relay._is_lan_only("127.0.0.1") is True
    assert local_relay._is_lan_only("8.8.8.8") is False
    # A hostname can't be classified — treated as public-intent.
    assert local_relay._is_lan_only("relay.example.com") is False


def test_start_serves_then_stop():
    port = _free_port()
    st = local_relay.start(port, "test-grid-key")
    assert st["running"] is True
    assert st["port"] == port
    assert st["suggested_url"].endswith(f":{port}")

    body = _wait_for_relay(port)
    assert body.get("service") == "Nexus Relay Server"
    assert local_relay.is_running() is True

    local_relay.stop()
    assert local_relay.is_running() is False


def test_start_is_idempotent_while_running():
    first_port = _free_port()
    local_relay.start(first_port, "k")
    # A second start while already running is a no-op — same port.
    st = local_relay.start(_free_port(), "k")
    assert st["running"] is True
    assert st["port"] == first_port
