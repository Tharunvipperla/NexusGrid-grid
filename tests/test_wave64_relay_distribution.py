"""Wave 64 — relay-code distribution (cookbook-style export / import)."""

from __future__ import annotations

import pytest

from nexus.runtime import local_relay
from nexus.runtime import relay_codeprint as cp

_SRC = ("from fastapi import FastAPI\n"
        "GRID_KEY = ''\n"
        "app = FastAPI()\n")


@pytest.fixture(autouse=True)
def _base(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    yield


def test_export_bundled_default_source():
    out = local_relay.get_module_source("default")
    assert out["builtin"] is True
    assert "app" in out["source"]                      # real relay source
    assert out["fingerprint"] == cp.CURRENT_FINGERPRINT


def test_import_then_export_roundtrip_and_listed():
    res = local_relay.import_module_source("myrelay", _SRC)
    assert res["name"] == "myrelay" and res["fingerprint"]
    # The saved module's fingerprint is deterministic from its bytes.
    assert res["fingerprint"] == cp.fingerprint_for_path(res["path"])
    # It now appears among available modules with that fingerprint.
    mods = {m["name"]: m for m in local_relay.available_relay_modules()}
    assert "myrelay" in mods and mods["myrelay"]["fingerprint"] == res["fingerprint"]
    # ...and its source round-trips.
    got = local_relay.get_module_source("myrelay")
    assert got["source"] == _SRC and got["builtin"] is False


@pytest.mark.parametrize("name", ["default", "", "../evil", "a/b", "has space", "x" * 41])
def test_import_rejects_bad_names(name):
    with pytest.raises(ValueError):
        local_relay.import_module_source(name, _SRC)


def test_import_rejects_empty_and_oversized():
    with pytest.raises(ValueError):
        local_relay.import_module_source("ok", "   ")
    with pytest.raises(ValueError):
        local_relay.import_module_source("big", "x" * (local_relay._MAX_RELAY_SOURCE + 1))


def test_export_unknown_returns_empty():
    assert local_relay.get_module_source("nope") == {}


def test_delete_plugin_and_guards():
    local_relay.import_module_source("tmp", _SRC)
    assert any(m["name"] == "tmp" for m in local_relay.available_relay_modules())
    assert local_relay.delete_module("tmp")["ok"] is True
    assert all(m["name"] != "tmp" for m in local_relay.available_relay_modules())
    # Can't delete the bundled default, an unknown, or a running module.
    assert local_relay.delete_module("default")["ok"] is False
    assert local_relay.delete_module("ghost")["ok"] is False

    class _Alive:
        def is_alive(self): return True
    local_relay.import_module_source("live", _SRC)
    local_relay._instances[59998] = {"server": None, "thread": _Alive(), "module": "live"}
    try:
        assert local_relay.delete_module("live")["error"] == "module is running — stop it first"
    finally:
        local_relay._instances.pop(59998, None)
