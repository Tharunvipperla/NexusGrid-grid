"""Wave 61 — pluggable relay module loader + per-module code fingerprint."""

from __future__ import annotations

import pytest

from nexus.runtime import local_relay
from nexus.runtime import relay_codeprint as cp


def test_fingerprint_for_path_distinguishes_code(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("app = 1\n", encoding="utf-8")
    b.write_text("app = 2\n", encoding="utf-8")
    fa, fb = cp.fingerprint_for_path(str(a)), cp.fingerprint_for_path(str(b))
    assert fa and fb and fa != fb           # different code → different print
    assert cp.fingerprint_for_path(str(a)) == fa  # deterministic
    assert cp.fingerprint_for_path(str(tmp_path / "missing.py")) == ""  # absent → ""


def test_available_modules_lists_default_and_plugins(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    monkeypatch.setattr(local_relay, "_module", "default")
    # No plugins yet → just the bundled default.
    mods = local_relay.available_relay_modules()
    assert mods[0]["name"] == "default" and mods[0]["builtin"] is True
    assert mods[0]["fingerprint"] == cp.CURRENT_FINGERPRINT
    assert [m["name"] for m in mods] == ["default"]

    # Drop a host-trusted plugin → it shows up with its own fingerprint.
    d = tmp_path / "nexus_relays"
    d.mkdir()
    (d / "echo.py").write_text("app = object()\n", encoding="utf-8")
    mods = local_relay.available_relay_modules()
    names = [m["name"] for m in mods]
    assert names == ["default", "echo"]
    echo = next(m for m in mods if m["name"] == "echo")
    assert echo["builtin"] is False
    assert echo["fingerprint"] and echo["fingerprint"] != cp.CURRENT_FINGERPRINT


def test_load_unknown_plugin_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    with pytest.raises(ValueError):
        local_relay._load_relay_module("does-not-exist")


def test_load_plugin_without_app_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    d = tmp_path / "nexus_relays"
    d.mkdir()
    (d / "broken.py").write_text("GRID_KEY = 'x'\n", encoding="utf-8")  # no app
    with pytest.raises(ValueError):
        local_relay._load_relay_module("broken")


def test_default_module_loads_bundled():
    mod = local_relay._load_relay_module("default")
    assert hasattr(mod, "app") and hasattr(mod, "GRID_KEY")


def test_status_reports_active_module(monkeypatch):
    monkeypatch.setattr(local_relay, "_module", "default")
    st = local_relay.status()
    assert st["module"] == "default"
    assert st["code_fingerprint"] == cp.CURRENT_FINGERPRINT
