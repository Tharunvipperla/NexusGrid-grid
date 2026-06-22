"""Wave 60 — replication auto-run: run-spec, runner argv hardening, gating."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import (LOCAL_SETTINGS, normalize_hosted_services,
                               public_services)
from nexus.runtime import replica_runner as rr


@pytest.fixture(autouse=True)
def _clean():
    rr._replicas.clear()
    saved = list(LOCAL_SETTINGS.get("allowed_images") or [])
    yield
    rr._replicas.clear()
    LOCAL_SETTINGS["allowed_images"] = saved
    LOCAL_SETTINGS["hosted_services"] = []


def test_run_spec_normalized_and_public():
    svc = normalize_hosted_services([{
        "name": "LLM", "replicable": True,
        "run": {"image": "Ollama/Ollama:Latest", "cmd": "serve",
                "env": ["FOO=bar", "bad", "OK=1"], "ports": [11434, "x", 11434, 22]},
    }])[0]
    run = svc["run"]
    assert run["image"] == "Ollama/Ollama:Latest"
    assert run["cmd"] == "serve"
    assert run["env"] == ["FOO=bar", "OK=1"]   # "bad" (no =) dropped
    assert run["ports"] == [11434, 22]          # non-int + dupe dropped
    # The run-spec is PUBLIC — the consumer needs it to run the service.
    assert public_services([svc])[0]["run"]["image"] == "Ollama/Ollama:Latest"


def test_empty_run_spec_when_absent():
    svc = normalize_hosted_services([{"name": "X", "run": {"env": ["A=1"]}}])[0]
    assert svc["run"] == {}  # no image and no cmd → no auto-run


def test_docker_argv_is_hardened_and_loopback():
    rr._ensure_builtins()
    ctx = {"spec": {"image": "redis:7", "cmd": "", "env": ["A=1"], "ports": [6379]},
           "host_ports": [55001], "allow_outbound": False,
           "mem_mb": 512, "cpus": "1.0"}
    argv = rr._RUNNERS["docker"]["build"](ctx)
    s = " ".join(argv)
    assert "--cap-drop ALL" in s
    assert "--security-opt no-new-privileges" in s
    assert "--read-only" in s
    assert "--memory 512m" in s
    # Loopback-only publish + internal (no-egress) network.
    assert "-p 127.0.0.1:55001:6379" in s
    assert "--network nexus-isolated" in s
    assert "-e A=1" in s
    assert argv[-1] == "redis:7"


def test_docker_argv_outbound_uses_default_bridge():
    rr._ensure_builtins()
    ctx = {"spec": {"image": "redis:7", "cmd": "", "env": [], "ports": [6379]},
           "host_ports": [55002], "allow_outbound": True,
           "mem_mb": 512, "cpus": "1.0"}
    argv = rr._RUNNERS["docker"]["build"](ctx)
    assert "--network" not in argv  # outbound → default bridge, no override


def test_run_requires_consent(monkeypatch):
    async def _svc(uid, name):
        return {"name": name, "replicable": True,
                "run": {"image": "redis:7", "cmd": "", "env": [], "ports": []}}
    monkeypatch.setattr(rr, "_fetch_public_service", _svc)
    LOCAL_SETTINGS["allowed_images"] = ["redis:7"]
    res = asyncio.run(rr.run_replica("nexus_x", "S", "docker", False, agreed=False))
    assert res["ok"] is False and res["error"] == "consent_required"


def test_run_refuses_non_allowlisted_image(monkeypatch):
    async def _svc(uid, name):
        return {"name": name, "replicable": True,
                "run": {"image": "evil/miner:latest", "cmd": "", "env": [], "ports": []}}
    monkeypatch.setattr(rr, "_fetch_public_service", _svc)
    # Make the docker runner look available so we reach the allowlist gate.
    monkeypatch.setitem(rr._RUNNERS, "docker",
                        {**rr._RUNNERS.get("docker", {}), "available": lambda: True,
                         "kind": "container", "engine": "docker",
                         "build": lambda c: ["docker"], "sandboxed": True})
    LOCAL_SETTINGS["allowed_images"] = ["python:3.11-slim"]
    res = asyncio.run(rr.run_replica("nexus_x", "S", "docker", False, agreed=True))
    assert res["ok"] is False and res["error"] == "image_not_allowed"


def test_run_refuses_when_not_replicable(monkeypatch):
    async def _svc(uid, name):
        return {"name": name, "replicable": False,
                "run": {"image": "redis:7", "cmd": "", "env": [], "ports": []}}
    monkeypatch.setattr(rr, "_fetch_public_service", _svc)
    res = asyncio.run(rr.run_replica("nexus_x", "S", "raw", False, agreed=True))
    assert res["ok"] is False and res["error"] == "not_replicable"


def test_run_refuses_without_run_spec(monkeypatch):
    async def _svc(uid, name):
        return {"name": name, "replicable": True, "run": {}}
    monkeypatch.setattr(rr, "_fetch_public_service", _svc)
    res = asyncio.run(rr.run_replica("nexus_x", "S", "raw", False, agreed=True))
    assert res["ok"] is False and res["error"] == "no_run_spec"


def test_available_runners_lists_builtins():
    names = {r["name"] for r in rr.available_runners()}
    assert {"docker", "podman", "raw"} <= names
    raw = next(r for r in rr.available_runners() if r["name"] == "raw")
    assert raw["sandboxed"] is False and raw["available"] is True
