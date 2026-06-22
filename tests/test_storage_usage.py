"""Diagnostics storage-usage breakdown: scan categories + clear deletable ones."""

from __future__ import annotations

import pytest

from nexus.runtime import storage_usage as su


def _seed(base):
    (base / "nexus_mod_5000.db").write_bytes(b"x" * 100)
    (base / "nexus_mod_5000.db-wal").write_bytes(b"x" * 50)
    (base / ".nexus_secret").write_bytes(b"s" * 10)
    (base / "completed_tasks").mkdir()
    (base / "completed_tasks" / "out.txt").write_bytes(b"a" * 200)
    (base / "nexus_venv_cache").mkdir()
    (base / "nexus_venv_cache" / "lib").write_bytes(b"c" * 300)
    (base / "nexus_cache_5000").mkdir()
    (base / "nexus_cache_5000" / "dep").write_bytes(b"h" * 70)   # hosted deposit
    (base / "nexus_mod_9999.db").write_bytes(b"o" * 80)          # stale (other port)
    (base / "nexus_mod_5000.db.pre_restore").write_bytes(b"b" * 40)


def _cat(scan, key):
    return next(c for c in scan["categories"] if c["key"] == key)


def test_scan_sizes_and_deletable_flags(tmp_path):
    _seed(tmp_path)
    s = su.scan(base=tmp_path, port=5000)
    assert _cat(s, "database")["bytes"] == 150 and _cat(s, "database")["deletable"] is False
    assert _cat(s, "identity")["bytes"] == 10 and _cat(s, "identity")["deletable"] is False
    assert _cat(s, "artifacts")["bytes"] == 200 and _cat(s, "artifacts")["deletable"] is True
    assert _cat(s, "hosted")["bytes"] == 70 and _cat(s, "hosted")["deletable"] is False
    assert _cat(s, "caches")["bytes"] == 300 and _cat(s, "caches")["deletable"] is True
    assert _cat(s, "backups")["bytes"] == 40 and _cat(s, "backups")["deletable"] is True
    assert _cat(s, "stale_db")["bytes"] == 80 and _cat(s, "stale_db")["deletable"] is True
    assert s["total_bytes"] == 150 + 10 + 200 + 70 + 300 + 40 + 80


def test_clear_artifacts_removes_dir_only(tmp_path):
    _seed(tmp_path)
    res = su.clear("artifacts", base=tmp_path, port=5000)
    assert res["removed_bytes"] == 200
    assert not (tmp_path / "completed_tasks").exists()
    assert (tmp_path / "nexus_mod_5000.db").exists()        # live data untouched


def test_clear_stale_db_keeps_current_port(tmp_path):
    _seed(tmp_path)
    res = su.clear("stale_db", base=tmp_path, port=5000)
    assert res["removed_bytes"] == 80
    assert not (tmp_path / "nexus_mod_9999.db").exists()
    assert (tmp_path / "nexus_mod_5000.db").exists()


def test_clear_backups(tmp_path):
    _seed(tmp_path)
    res = su.clear("backups", base=tmp_path, port=5000)
    assert res["removed_bytes"] == 40
    assert not (tmp_path / "nexus_mod_5000.db.pre_restore").exists()


def test_clear_refuses_protected_categories(tmp_path):
    _seed(tmp_path)
    for key in ("database", "identity", "hosted", "bogus"):
        with pytest.raises(ValueError):
            su.clear(key, base=tmp_path, port=5000)
    assert (tmp_path / ".nexus_secret").exists()
    assert (tmp_path / "nexus_cache_5000").exists()


def test_list_files_returns_relative_paths(tmp_path):
    _seed(tmp_path)
    files = su.list_files("artifacts", base=tmp_path, port=5000)
    assert files == [{"path": "completed_tasks/out.txt", "bytes": 200}]
    caches = su.list_files("caches", base=tmp_path, port=5000)
    assert {f["path"] for f in caches} == {"nexus_venv_cache/lib"}


def test_delete_file_removes_one_and_validates(tmp_path):
    _seed(tmp_path)
    res = su.delete_file("artifacts", "completed_tasks/out.txt", base=tmp_path, port=5000)
    assert res["removed_bytes"] == 200
    assert not (tmp_path / "completed_tasks" / "out.txt").exists()
    # traversal / wrong-category / protected all refuse
    with pytest.raises(ValueError):
        su.delete_file("artifacts", "../.nexus_secret", base=tmp_path, port=5000)
    with pytest.raises(ValueError):
        su.delete_file("caches", "completed_tasks/out.txt", base=tmp_path, port=5000)  # not in caches
    with pytest.raises(ValueError):
        su.delete_file("identity", ".nexus_secret", base=tmp_path, port=5000)  # protected
    assert (tmp_path / ".nexus_secret").exists()
