"""Wave 62 — multiple opt-in relay instances per host."""

from __future__ import annotations

import socket
import time

import httpx
import pytest

from nexus.runtime import local_relay
from nexus.runtime import relay_codeprint as cp


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(port: int, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
            if r.status_code == 200:
                return r.json()
        except Exception as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(f"relay not up on {port}: {last}")


@pytest.fixture(autouse=True)
def _clean():
    local_relay.stop()
    for p in list(local_relay._instances):
        local_relay.stop_instance(p)
    yield
    for p in list(local_relay._instances):
        local_relay.stop_instance(p)
    local_relay.stop()


def _seed_plugin(tmp_path, monkeypatch, name="echo"):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    d = tmp_path / "nexus_relays"
    d.mkdir(exist_ok=True)
    (d / f"{name}.py").write_text(
        "from fastapi import FastAPI\n"
        "GRID_KEY = ''\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        f"def root(): return {{'relay': '{name}'}}\n",
        encoding="utf-8",
    )


class _FakeThread:
    def is_alive(self):
        return True


def test_module_already_running_guard(monkeypatch):
    # A module already running as an instance can't be started again.
    local_relay._instances[59999] = {"server": None, "thread": _FakeThread(),
                                     "module": "echo"}
    with pytest.raises(ValueError):
        local_relay.start_instance(_free_port(), "k", "echo")
    local_relay._instances.pop(59999, None)


def test_fingerprint_per_module(tmp_path, monkeypatch):
    assert local_relay._fingerprint_for_module("default") == cp.CURRENT_FINGERPRINT
    _seed_plugin(tmp_path, monkeypatch, "echo")
    fp = local_relay._fingerprint_for_module("echo")
    assert fp and fp != cp.CURRENT_FINGERPRINT


def test_start_list_stop_instance_real(tmp_path, monkeypatch):
    _seed_plugin(tmp_path, monkeypatch, "echo")
    port = _free_port()
    st = local_relay.start_instance(port, "k", "echo")
    assert st["running"] is True and st["module"] == "echo"

    assert _wait(port).get("relay") == "echo"   # the custom relay actually serves
    listed = local_relay.list_instances()
    assert any(i["port"] == port and i["module"] == "echo" and i["running"]
               for i in listed)

    assert local_relay.stop_instance(port)["ok"] is True
    assert all(i["port"] != port for i in local_relay.list_instances())


def test_stop_unknown_instance():
    assert local_relay.stop_instance(_free_port())["ok"] is False
