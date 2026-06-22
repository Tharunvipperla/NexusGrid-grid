"""B3 — result/artifact browser: bundle listing, file listing, safe resolve."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.runtime import result_browser as RB


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    """Point BASE_DIR at a temp tree so completed_tasks/ is isolated."""
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    root = tmp_path / "completed_tasks"
    root.mkdir()
    return root


def _bundle(root: Path, tid: str, files: dict[str, str]) -> Path:
    d = root / tid
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


# ---- list_bundles ----------------------------------------------------------

def test_list_bundles_empty_when_no_root(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path / "nope")
    assert RB.list_bundles() == []


def test_list_bundles_counts_and_sizes(_root):
    _bundle(_root, "task-a", {"out.txt": "hello", "sub/log.txt": "world!"})
    _bundle(_root, "task-b", {"only.json": "{}"})
    (_root / "loose_file.txt").write_text("ignored")  # non-dir entries skipped

    bundles = {b["task_id"]: b for b in RB.list_bundles()}
    assert set(bundles) == {"task-a", "task-b"}
    assert bundles["task-a"]["file_count"] == 2
    assert bundles["task-a"]["total_bytes"] == len("hello") + len("world!")
    assert bundles["task-b"]["file_count"] == 1


def test_list_bundles_sorted_newest_first(_root):
    import os
    import time
    a = _bundle(_root, "older", {"f": "x"})
    b = _bundle(_root, "newer", {"f": "x"})
    old = time.time() - 1000
    for p in a.rglob("*"):
        os.utime(p, (old, old))
    ids = [x["task_id"] for x in RB.list_bundles()]
    assert ids.index("newer") < ids.index("older")


# ---- list_files ------------------------------------------------------------

def test_list_files_recursive_posix(_root):
    _bundle(_root, "t", {"a.txt": "1", "logs/run.log": "22", "logs/deep/x": "333"})
    files = {f["path"]: f["bytes"] for f in RB.list_files("t")}
    assert files == {"a.txt": 1, "logs/run.log": 2, "logs/deep/x": 3}


def test_list_files_missing_bundle_returns_none(_root):
    assert RB.list_files("ghost") is None


def test_list_files_rejects_traversal_id(_root):
    assert RB.list_files("../secrets") is None
    assert RB.list_files("a/b") is None


# ---- resolve_file ----------------------------------------------------------

def test_resolve_file_ok(_root):
    _bundle(_root, "t", {"logs/run.log": "data"})
    p = RB.resolve_file("t", "logs/run.log")
    assert p is not None and p.read_text() == "data"


def test_resolve_file_rejects_traversal(_root):
    _bundle(_root, "t", {"a.txt": "x"})
    (_root / "secret.txt").write_text("TOP SECRET")
    assert RB.resolve_file("t", "../secret.txt") is None
    assert RB.resolve_file("t", "..\\secret.txt") is None
    assert RB.resolve_file("t", "/etc/passwd") is None


def test_resolve_file_missing_or_dir(_root):
    _bundle(_root, "t", {"sub/a.txt": "x"})
    assert RB.resolve_file("t", "nope.txt") is None
    assert RB.resolve_file("t", "sub") is None  # directory, not a file


# ---- delete_bundle / delete_all_bundles ------------------------------------

def test_delete_bundle_removes_one(_root):
    _bundle(_root, "keep", {"a": "1"})
    _bundle(_root, "gone", {"b": "2", "sub/c": "3"})
    assert RB.delete_bundle("gone") is True
    assert not (_root / "gone").exists()
    assert (_root / "keep").exists()  # other bundles untouched


def test_delete_bundle_missing_returns_false(_root):
    assert RB.delete_bundle("ghost") is False


def test_delete_bundle_rejects_traversal(_root):
    (_root.parent / "secret_dir").mkdir()
    assert RB.delete_bundle("../secret_dir") is False
    assert (_root.parent / "secret_dir").exists()


def test_delete_all_bundles_clears_root(_root):
    _bundle(_root, "a", {"f": "x"})
    _bundle(_root, "b", {"f": "y"})
    (_root / "loose.txt").write_text("kept")  # non-dir entries are left alone
    assert RB.delete_all_bundles() == 2
    assert RB.list_bundles() == []
    assert (_root / "loose.txt").exists()


# ---- write_log_artifact (B4) -----------------------------------------------

def test_write_log_artifact_creates_bundle_and_file(_root):
    rel = RB.write_log_artifact("svc-1", ["line one", "line two"])
    assert rel.startswith("logs/live-log-") and rel.endswith(".log")
    # The artifact is now browsable as a normal bundle file.
    files = {f["path"]: f["bytes"] for f in RB.list_files("svc-1")}
    assert rel in files
    saved = (_root / "svc-1" / rel).read_text()
    assert saved == "line one\nline two\n"


def test_write_log_artifact_rejects_traversal(_root):
    import pytest as _pt
    with _pt.raises(ValueError):
        RB.write_log_artifact("../escape", ["x"])
    with _pt.raises(ValueError):
        RB.write_log_artifact("a/b", ["x"])
