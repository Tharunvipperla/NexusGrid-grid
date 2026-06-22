"""E5 — node backup (export): consistent DB snapshot + identity files + manifest."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from nexus.runtime import backup as B


@pytest.fixture
def fake_node(tmp_path, monkeypatch):
    """A BASE_DIR with a real sqlite DB + .nexus_* files for port 8090."""
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.core.get_node_port", lambda: 8090)

    db = tmp_path / "nexus_mod_8090.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (k TEXT, v TEXT)")
    con.execute("INSERT INTO t VALUES ('hello', 'world')")
    con.commit()
    con.close()

    (tmp_path / ".nexus_secret").write_text("SECRETKEY")
    (tmp_path / ".nexus_group_key").write_text("GROUPKEY")
    (tmp_path / ".nexus_local_token").write_text("TOKEN")
    (tmp_path / "unrelated.txt").write_text("not in backup")  # must be excluded
    return tmp_path


def test_backup_contains_db_identity_and_manifest(fake_node, tmp_path):
    dest = tmp_path / "out" / "backup.zip"
    summary = B.build_backup(dest)

    assert dest.is_file() and summary["bytes"] > 0
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert "nexus.db" in names
        assert {".nexus_secret", ".nexus_group_key", ".nexus_local_token"} <= names
        assert "manifest.json" in names
        assert "unrelated.txt" not in names  # only .nexus_* dotfiles + db
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["kind"] == "nexus-node-backup"
    assert "schema_version" in manifest
    assert "nexus.db" in manifest["members"]


def test_backup_db_snapshot_is_readable_and_consistent(fake_node, tmp_path):
    """The snapshot is a real, queryable copy of the live DB (WAL-safe)."""
    dest = tmp_path / "backup.zip"
    B.build_backup(dest)
    extracted = tmp_path / "nexus.db"
    with zipfile.ZipFile(dest) as z:
        extracted.write_bytes(z.read("nexus.db"))
    con = sqlite3.connect(str(extracted))
    row = con.execute("SELECT v FROM t WHERE k='hello'").fetchone()
    con.close()
    assert row[0] == "world"


def test_backup_survives_missing_db(tmp_path, monkeypatch):
    """No DB yet (fresh node) → still produces a zip with the manifest."""
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.core.get_node_port", lambda: 9999)
    (tmp_path / ".nexus_secret").write_text("S")
    dest = tmp_path / "backup.zip"
    summary = B.build_backup(dest)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
    assert "manifest.json" in names and ".nexus_secret" in names
    assert "nexus.db" not in names
    assert "nexus.db" not in summary["members"]


# ---- restore ---------------------------------------------------------------

def test_validate_backup_zip(fake_node, tmp_path):
    good = tmp_path / "good.zip"
    B.build_backup(good)
    assert B.validate_backup_zip(good) == (True, "")

    nomani = tmp_path / "nomani.zip"
    with zipfile.ZipFile(nomani, "w") as z:
        z.writestr("foo.txt", "bar")
    ok, why = B.validate_backup_zip(nomani)
    assert not ok and why == "no_manifest"

    wrong = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": "something-else"}))
    ok, why = B.validate_backup_zip(wrong)
    assert not ok and why == "wrong_kind"


def test_apply_pending_restore_swaps_db_and_files(fake_node, tmp_path, monkeypatch):
    # Build a backup of the SOURCE node (db has hello=world, .nexus_secret=SECRETKEY).
    src_backup = tmp_path / "src-backup.zip"
    B.build_backup(src_backup)

    # A different TARGET node: old db content + old secret, with the backup staged.
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", target)
    db = target / "nexus_mod_8090.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (k TEXT, v TEXT)")
    con.execute("INSERT INTO t VALUES ('hello', 'OLD')")
    con.commit()
    con.close()
    (target / ".nexus_secret").write_text("OLDSECRET")
    (target / B.PENDING_NAME).write_bytes(src_backup.read_bytes())

    res = B.apply_pending_restore(8090)
    assert res and res["applied"] is True

    con = sqlite3.connect(str(db))
    val = con.execute("SELECT v FROM t WHERE k='hello'").fetchone()[0]
    con.close()
    assert val == "world"                                   # restored content
    assert (target / ".nexus_secret").read_text() == "SECRETKEY"  # restored secret
    assert (target / "nexus_mod_8090.db.pre_restore").is_file()   # old db kept
    assert not (target / B.PENDING_NAME).exists()                 # consumed


def test_apply_pending_restore_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    assert B.apply_pending_restore(8090) is None


def test_apply_invalid_pending_is_moved_aside(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    bad = tmp_path / B.PENDING_NAME
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("foo.txt", "not a backup")
    res = B.apply_pending_restore(8090)
    assert res and res["applied"] is False
    assert not bad.exists()
    assert (tmp_path / (B.PENDING_NAME + ".invalid")).is_file()


# ---- full backup -----------------------------------------------------------

def _seed_data_dirs(base: Path):
    """On-disk node data a *full* backup must capture, plus junk it must skip."""
    (base / "nexus_relays").mkdir()
    (base / "nexus_relays" / "my_relay.py").write_text("# custom relay")
    (base / "nexus_relays" / "__pycache__").mkdir()
    (base / "nexus_relays" / "__pycache__" / "my_relay.pyc").write_bytes(b"junk")
    (base / "completed_tasks" / "job1").mkdir(parents=True)
    (base / "completed_tasks" / "job1" / "out.txt").write_text("result")
    (base / "nexus_cache_8090").mkdir()
    (base / "nexus_cache_8090" / "blob.bin").write_bytes(b"hosted")
    # regenerable — must NOT be in a full backup
    (base / "nexus_venv_cache").mkdir()
    (base / "nexus_venv_cache" / "x").write_text("cache")


def test_normal_backup_excludes_on_disk_data(fake_node):
    _seed_data_dirs(fake_node)
    dest = fake_node / "normal.zip"
    summary = B.build_backup(dest, full=False)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert json.loads(z.read("manifest.json"))["full"] is False
    assert not any(n.startswith("nexus_relays/") for n in names)
    assert not any(n.startswith("completed_tasks/") for n in names)
    assert summary["data_files"] == 0


def test_full_backup_includes_data_dirs_but_not_caches(fake_node):
    _seed_data_dirs(fake_node)
    dest = fake_node / "full.zip"
    summary = B.build_backup(dest, full=True)
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert json.loads(z.read("manifest.json"))["full"] is True
    assert "nexus.db" in names                            # still the normal contents
    assert "nexus_relays/my_relay.py" in names            # plugin code
    assert "completed_tasks/job1/out.txt" in names        # artifacts
    assert "nexus_cache_8090/blob.bin" in names           # hosted deposit bytes
    assert not any("nexus_venv_cache" in n for n in names)  # regenerable junk skipped
    assert not any("__pycache__" in n for n in names)        # compiled junk skipped
    assert summary["data_files"] == 3


def test_apply_full_restore_writes_data_files(fake_node, tmp_path, monkeypatch):
    _seed_data_dirs(fake_node)
    src = tmp_path / "src-full.zip"
    B.build_backup(src, full=True)

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", target)
    (target / B.PENDING_NAME).write_bytes(src.read_bytes())

    res = B.apply_pending_restore(8090)
    assert res and res["applied"] is True and res["data_files"] == 3
    assert (target / "nexus_relays" / "my_relay.py").read_text() == "# custom relay"
    assert (target / "completed_tasks" / "job1" / "out.txt").read_text() == "result"
    assert (target / "nexus_cache_8090" / "blob.bin").read_bytes() == b"hosted"


def test_restore_refuses_newer_schema(fake_node, tmp_path, monkeypatch):
    """A backup from a newer schema must NOT be applied onto an older node."""
    from nexus.storage import models

    src = tmp_path / "future.zip"
    monkeypatch.setattr(models, "SCHEMA_VERSION", 9999)  # backup "from the future"
    B.build_backup(src)
    monkeypatch.setattr(models, "SCHEMA_VERSION", 13)    # this (older) node

    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", target)
    db = target / "nexus_mod_8090.db"
    db.write_bytes(b"original-db")  # must be left untouched
    (target / B.PENDING_NAME).write_bytes(src.read_bytes())

    res = B.apply_pending_restore(8090)
    assert res and res["applied"] is False and res["reason"] == "newer_version"
    assert res["backup_schema"] == 9999
    assert db.read_bytes() == b"original-db"                 # DB not swapped
    assert (target / (B.PENDING_NAME + ".invalid")).is_file()  # parked, won't loop


def test_restore_allows_older_or_equal_schema(fake_node, tmp_path, monkeypatch):
    src = tmp_path / "old.zip"
    from nexus.storage import models
    monkeypatch.setattr(models, "SCHEMA_VERSION", 5)  # older backup
    B.build_backup(src)
    monkeypatch.setattr(models, "SCHEMA_VERSION", 13)  # newer node accepts it
    assert B.restore_too_new(src) is None


def test_restore_refuses_zipslip(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", base)
    pending = base / B.PENDING_NAME
    with zipfile.ZipFile(pending, "w") as z:
        z.writestr("manifest.json", json.dumps({"kind": "nexus-node-backup", "full": True}))
        z.writestr("../evil.txt", "pwned")  # path traversal attempt
    res = B.apply_pending_restore(8090)
    assert res and res["applied"] is True
    assert not (tmp_path / "evil.txt").exists()  # never written outside BASE_DIR
