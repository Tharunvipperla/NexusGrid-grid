"""P6 — disk-breakdown helpers: walker + foreign-storage hosted MB cache."""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.api import local as local_api


def test_walk_dir_bytes_counts_recursive_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 2048)
    (sub / "c.bin").write_bytes(b"z" * 4096)
    assert local_api._walk_dir_bytes(tmp_path) == 1024 + 2048 + 4096


def test_walk_dir_bytes_returns_zero_for_missing_path(tmp_path):
    assert local_api._walk_dir_bytes(tmp_path / "does-not-exist") == 0


def test_walk_dir_bytes_skips_broken_entries(tmp_path):
    # Empty dirs and zero-byte files both must be handled.
    (tmp_path / "empty").mkdir()
    (tmp_path / "zero").touch()
    assert local_api._walk_dir_bytes(tmp_path) == 0


def test_disk_breakdown_uses_per_port_db_filename(tmp_path, monkeypatch):
    """The DB row must point at ``nexus_mod_<port>.db``, not the legacy ``nexus.db``."""
    cache_root = tmp_path / "nexus_cache_8000"
    cache_root.mkdir()
    # Write a sentinel-size DB at the *correct* per-port path.
    (tmp_path / "nexus_mod_8000.db").write_bytes(b"x" * 4096)
    # And a stale legacy-named file we must NOT pick up.
    (tmp_path / "nexus.db").write_bytes(b"y" * 1024)

    monkeypatch.setattr(local_api, "_DISK_CACHE", {"ts": 0.0, "data": {}})
    monkeypatch.setattr("nexus.core.cache_dir", lambda port: cache_root)
    monkeypatch.setattr("nexus.core.get_node_port", lambda: 8000)
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)

    result = asyncio.run(local_api.local_disk_breakdown())
    assert result["bytes_by_category"]["db"] == 4096


def test_foreign_storage_hosted_mb_uses_cache_window(tmp_path, monkeypatch):
    """Two calls within 15 s must not walk twice; the second is a cache hit."""
    fake_root = tmp_path / "nexus_cache_8000"
    (fake_root / "foreign_storage").mkdir(parents=True)
    (fake_root / "foreign_storage" / "chunk_0.enc").write_bytes(b"x" * (2 * 1024 * 1024))

    monkeypatch.setattr(local_api, "_DISK_CACHE", {"ts": 0.0, "data": {}})

    calls = {"n": 0}
    real_walk = local_api._walk_dir_bytes

    def counting_walk(path):
        calls["n"] += 1
        return real_walk(path)

    monkeypatch.setattr(local_api, "_walk_dir_bytes", counting_walk)
    monkeypatch.setattr(
        "nexus.core.cache_dir", lambda port: fake_root
    )
    monkeypatch.setattr("nexus.core.get_node_port", lambda: 8000)

    first = asyncio.run(local_api._foreign_storage_hosted_mb())
    second = asyncio.run(local_api._foreign_storage_hosted_mb())

    assert first == 2  # 2 MB
    assert second == 2
    assert calls["n"] == 1, "second call inside the 15 s window must hit cache"
