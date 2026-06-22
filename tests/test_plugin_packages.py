"""D1 — plugin packages: build, validate, install, and the saved library."""

from __future__ import annotations

import json

import pytest

from nexus.runtime import plugin_files, plugin_packages


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Point BASE_DIR at a tmp dir and stub the node identity."""
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.core.get_node_identity", lambda: "node-test")
    return tmp_path


RUNNER_SRC = "def _build(spec):\n    return ['echo', 'hi']\n"
PUMP_SRC = "def _make():\n    return lambda d, c: c\n"


# --- build_package -----------------------------------------------------------


def test_build_package_reads_existing_modules(base):
    plugin_files.write_module("runners", "myrun", RUNNER_SRC)
    plugin_files.write_module("pumps", "mypump", PUMP_SRC)
    pkg = plugin_packages.build_package(
        [{"kind": "runners", "name": "myrun"}, {"kind": "pumps", "name": "mypump"}],
        name="My kit", description="two plugins",
    )
    assert pkg["format"] == plugin_packages.PACKAGE_FORMAT
    assert pkg["version"] == plugin_packages.PACKAGE_VERSION
    assert pkg["name"] == "My kit"
    assert pkg["node"] == "node-test"
    assert {(m["kind"], m["name"]) for m in pkg["modules"]} == {
        ("runners", "myrun"), ("pumps", "mypump")}


def test_build_package_empty_items_raises(base):
    with pytest.raises(ValueError):
        plugin_packages.build_package([])


def test_build_package_missing_module_raises(base):
    with pytest.raises(ValueError, match="no such module"):
        plugin_packages.build_package([{"kind": "runners", "name": "ghost"}])


def test_build_package_unknown_kind_raises(base):
    with pytest.raises(ValueError, match="unknown plugin kind"):
        plugin_packages.build_package([{"kind": "wat", "name": "x"}])


# --- validate_package --------------------------------------------------------


def _good_pkg():
    return {
        "format": plugin_packages.PACKAGE_FORMAT,
        "version": 1,
        "modules": [{"kind": "runners", "name": "r1", "source": RUNNER_SRC}],
    }


def test_validate_accepts_good_package():
    out = plugin_packages.validate_package(_good_pkg())
    assert out["modules"][0]["name"] == "r1"


@pytest.mark.parametrize("bad,msg", [
    ("notadict", "JSON object"),
    ({"format": "other", "version": 1, "modules": []}, "not a NexusGrid"),
    ({"format": plugin_packages.PACKAGE_FORMAT, "version": 1, "modules": []}, "no modules"),
])
def test_validate_rejects_bad(bad, msg):
    with pytest.raises(ValueError, match=msg):
        plugin_packages.validate_package(bad)


def test_validate_rejects_newer_version():
    pkg = _good_pkg()
    pkg["version"] = plugin_packages.PACKAGE_VERSION + 1
    with pytest.raises(ValueError, match="newer than this node"):
        plugin_packages.validate_package(pkg)


def test_validate_rejects_unknown_kind():
    pkg = _good_pkg()
    pkg["modules"][0]["kind"] = "nope"
    with pytest.raises(ValueError, match="unknown plugin kind"):
        plugin_packages.validate_package(pkg)


def test_validate_rejects_bad_syntax():
    pkg = _good_pkg()
    pkg["modules"][0]["source"] = "def broken(:\n"
    with pytest.raises(ValueError):
        plugin_packages.validate_package(pkg)


def test_validate_rejects_reserved_name():
    pkg = _good_pkg()
    pkg["modules"][0]["name"] = "default"  # reserved by plugin_files
    with pytest.raises(ValueError):
        plugin_packages.validate_package(pkg)


# --- install_package ---------------------------------------------------------


def test_install_writes_modules(base):
    res = plugin_packages.install_package(_good_pkg())
    assert res["installed"] == 1 and res["skipped"] == 0
    assert plugin_files.read_module("runners", "r1")["source"] == RUNNER_SRC


def test_install_skips_existing_without_overwrite(base):
    plugin_files.write_module("runners", "r1", "def _build(spec):\n    return ['old']\n")
    res = plugin_packages.install_package(_good_pkg())
    assert res["installed"] == 0 and res["skipped"] == 1
    assert res["results"][0]["status"] == "skipped"
    # untouched
    assert "old" in plugin_files.read_module("runners", "r1")["source"]


def test_install_overwrites_when_asked(base):
    plugin_files.write_module("runners", "r1", "def _build(spec):\n    return ['old']\n")
    res = plugin_packages.install_package(_good_pkg(), overwrite=True)
    assert res["installed"] == 1
    assert plugin_files.read_module("runners", "r1")["source"] == RUNNER_SRC


def test_build_then_install_round_trip(base, tmp_path, monkeypatch):
    plugin_files.write_module("runners", "rt", RUNNER_SRC)
    pkg = plugin_packages.build_package([{"kind": "runners", "name": "rt"}])
    # fresh node: new BASE_DIR with nothing installed
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", fresh)
    assert plugin_files.read_module("runners", "rt") == {}
    res = plugin_packages.install_package(pkg)
    assert res["installed"] == 1
    assert plugin_files.read_module("runners", "rt")["source"] == RUNNER_SRC


# --- saved library -----------------------------------------------------------


def test_save_list_read_delete(base):
    pkg = {**_good_pkg(), "name": "Kit One"}
    saved = plugin_packages.save_package(pkg)
    assert saved["filename"].endswith(".json")

    listed = plugin_packages.list_packages()
    assert len(listed) == 1
    assert listed[0]["name"] == "Kit One"
    assert listed[0]["modules"] == [{"kind": "runners", "name": "r1"}]

    full = plugin_packages.read_package(saved["filename"])
    assert full["modules"][0]["source"] == RUNNER_SRC

    plugin_packages.delete_package(saved["filename"])
    assert plugin_packages.list_packages() == []


def test_read_package_rejects_traversal(base):
    with pytest.raises(ValueError):
        plugin_packages.read_package("../secret.json")


def test_read_missing_package_raises(base):
    with pytest.raises(ValueError, match="no such package"):
        plugin_packages.read_package("nope.json")


def test_save_package_persists_valid_json(base):
    saved = plugin_packages.save_package({**_good_pkg(), "name": "j"})
    raw = (base / "nexus_packages" / saved["filename"]).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["format"] == plugin_packages.PACKAGE_FORMAT
