"""Wave 72 / A8 — in-app plugin-module editor backend."""

from __future__ import annotations

import pytest

from nexus.runtime import local_relay
from nexus.runtime import plugin_files as pf


@pytest.fixture(autouse=True)
def _base(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    yield


_SRC = "def create(a, b, c, d):\n    pass\n"


# --- validation -------------------------------------------------------------


def test_validate_ok():
    assert pf.validate_source("x = 1\n") == {"ok": True}


def test_validate_empty():
    assert pf.validate_source("   ")["ok"] is False


def test_validate_syntax_error_reports_line():
    res = pf.validate_source("def f(:\n  pass\n")
    assert res["ok"] is False
    assert res["line"] == 1
    assert res["error"]


def test_validate_too_large():
    assert pf.validate_source("x=1\n" + "#" * (1024 * 1024 + 1))["ok"] is False


# --- CRUD -------------------------------------------------------------------


def test_write_read_list_roundtrip():
    res = pf.write_module("pumps", "mypump", _SRC)
    assert res["name"] == "mypump"
    got = pf.read_module("pumps", "mypump")
    assert got["source"] == _SRC
    names = [m["name"] for m in pf.list_modules("pumps")]
    assert "mypump" in names


def test_write_rejects_syntax_error():
    with pytest.raises(ValueError):
        pf.write_module("runners", "bad", "def x(:\n")


@pytest.mark.parametrize("name", ["default", "", "../evil", "a/b", "has space", "x" * 41])
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        pf.write_module("pumps", name, _SRC)


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        pf.list_modules("nope")


def test_write_normalizes_crlf_to_lf(tmp_path):
    pf.write_module("dbproviders", "crlf", "a = 1\r\nb = 2\r\n")
    raw = (tmp_path / "nexus_dbproviders" / "crlf.py").read_bytes()
    assert b"\r\n" not in raw and raw == b"a = 1\nb = 2\n"


def test_relays_get_a_fingerprint_others_dont():
    pf.write_module("relays", "r1", "from fastapi import FastAPI\napp = FastAPI()\n")
    pf.write_module("pumps", "p1", _SRC)
    r = next(m for m in pf.list_modules("relays") if m["name"] == "r1")
    p = next(m for m in pf.list_modules("pumps") if m["name"] == "p1")
    assert r["fingerprint"]          # relays are fingerprinted
    assert p["fingerprint"] == ""    # pumps are not


def test_delete_generic_kind():
    pf.write_module("runners", "tmp", _SRC)
    assert pf.delete_module("runners", "tmp")["ok"] is True
    assert pf.delete_module("runners", "tmp")["ok"] is False  # gone -> not_found


def test_delete_relay_delegates_guard():
    # Unknown relay -> local_relay guard returns not_found, not a crash.
    assert pf.delete_module("relays", "ghost")["ok"] is False


def test_overview_lists_all_kinds():
    kinds = {k["kind"] for k in pf.overview()}
    assert kinds == {"relays", "pumps", "runners", "dbproviders"}


# --- A8: view + copy the built-in default relay -----------------------------


def test_default_relay_source_is_viewable():
    # The built-in default is read-only but its source can be inspected/copied.
    got = local_relay.get_module_source("default")
    assert got["builtin"] is True
    assert "app" in got["source"] and len(got["source"]) > 100


def test_copy_of_default_becomes_an_editable_module():
    src = local_relay.get_module_source("default")["source"]
    pf.write_module("relays", "default-copy", src)        # "Make a copy"
    back = pf.read_module("relays", "default-copy")["source"]
    assert back == src.replace("\r\n", "\n")              # LF-normalized on write
    # the copy is a real, deletable plugin (unlike the protected default)
    assert pf.delete_module("relays", "default-copy")["ok"] is True


# --- A8: built-in reference implementations are visible (read-only) ----------


def test_builtins_listed_for_every_kind():
    assert [b["name"] for b in pf.builtins("relays")] == ["default"]
    assert [b["name"] for b in pf.builtins("pumps")] == ["default"]
    assert {b["name"] for b in pf.builtins("runners")} == {"docker", "podman", "raw"}
    assert [b["name"] for b in pf.builtins("dbproviders")] == ["postgres"]


@pytest.mark.parametrize("kind,name", [
    ("relays", "default"), ("pumps", "default"),
    ("runners", "raw"), ("runners", "docker"), ("dbproviders", "postgres"),
])
def test_builtin_source_returns_readonly_reference(kind, name):
    got = pf.builtin_source(kind, name)
    assert got and got["readonly"] is True and got["builtin"] is True
    assert got["source"].strip()


def test_builtin_source_unknown_returns_empty():
    assert pf.builtin_source("pumps", "nope") == {}
    assert pf.builtin_source("relays", "myfile") == {}      # a file name is not a built-in
