"""Wave 65 — sandboxed (out-of-process) execution of a relay module."""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import local_relay
from nexus.runtime import relay_sandbox as rs

_SRC = ("from fastapi import FastAPI\n"
        "GRID_KEY = ''\n"
        "app = FastAPI()\n")


class _FakeProc:
    def __init__(self, *a, **k):
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False


@pytest.fixture(autouse=True)
def _base(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    rs._sandboxed.clear()
    # A real python so the raw runner is "available"; spawn is faked.
    monkeypatch.setattr(rs, "_find_python", lambda: "python")
    monkeypatch.setattr(rs.subprocess, "Popen", _FakeProc)
    yield
    rs._sandboxed.clear()


def _run(module="default", port=9700, runner="raw", grid_key="k",
         agreed=True, image="", allow_outbound=False):
    return asyncio.run(rs.run_sandboxed_relay(
        module, port, runner, grid_key, agreed, image, allow_outbound))


def test_consent_required():
    assert _run(agreed=False) == {"ok": False, "error": "consent_required"}


def test_unknown_module():
    assert _run(module="ghost")["error"] == "no_such_module"


def test_unknown_runner():
    assert _run(runner="nope")["error"] == "unknown_runner"


def test_raw_run_lists_and_stops():
    res = _run()
    assert res["ok"] is True
    rec = res["relay"]
    assert rec["runner"] == "raw" and rec["sandboxed"] is False
    assert rec["kind"] == "process" and rec["port"] == 9700
    assert rec["fingerprint"]  # default's bundled fingerprint

    listed = rs.list_sandboxed_relays()["relays"]
    assert len(listed) == 1 and listed[0]["running"] is True

    # Bind-time validation can resolve a sandboxed relay's fingerprint by port.
    assert local_relay.fingerprint_for_url("ws://127.0.0.1:9700") == rec["fingerprint"]

    out = asyncio.run(rs.stop_sandboxed_relay(rec["sandbox_id"]))
    assert out["ok"] is True
    assert rs.list_sandboxed_relays()["relays"] == []


def test_imported_plugin_runs_sandboxed():
    local_relay.import_module_source("shared1", _SRC)
    res = _run(module="shared1")
    assert res["ok"] is True
    assert res["relay"]["module"] == "shared1"
    assert res["relay"]["fingerprint"]


def test_container_requires_allowlisted_image(monkeypatch):
    # Pretend docker exists so the container runner is available.
    monkeypatch.setattr(rs.rr.shutil, "which", lambda n: "/usr/bin/docker")
    monkeypatch.setattr(rs.rr, "_image_allowed", lambda img: False)
    assert _run(runner="docker", image="evil:latest")["error"] == "image_not_allowed"
    assert _run(runner="docker", image="")["error"] == "image_required"


def test_too_many_guard():
    for i in range(rs._MAX_SANDBOXED):
        rs._sandboxed[f"x{i}"] = {"port": 1, "proc": _FakeProc()}
    assert _run()["error"] == "too_many"
