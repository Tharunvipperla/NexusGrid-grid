"""Pre-launch hardening — archive extraction safety.

F-003: a peer-supplied service snapshot zip is extracted on the standby. Even
though CPython's zipfile strips traversal components, we defend in depth with
the repo's safe_extractall, which *rejects* a traversal entry outright.
"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from nexus.runtime import service_replication


def _zip_with(entry_name: str, body: bytes = b"pwned") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(entry_name, body)
    return buf.getvalue()


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr(service_replication, "cache_dir", lambda _p: tmp_path)
    monkeypatch.setattr(service_replication, "get_node_port", lambda: 8000)
    return tmp_path


@pytest.mark.parametrize("evil", ["../../evil.txt", "../escape.txt"])
def test_extract_snapshot_rejects_traversal(staged, evil):
    base = staged / "services" / "svc-evil"
    base.mkdir(parents=True)
    (base / "snapshot.zip").write_bytes(_zip_with(evil))

    with pytest.raises(ValueError, match="traversal"):
        asyncio.run(service_replication.extract_snapshot("svc-evil"))

    # nothing escaped the staging dir at any level above it
    assert not (staged / "services" / "evil.txt").exists()
    assert not (staged / "services" / "escape.txt").exists()
    assert not (staged / "evil.txt").exists()


def test_extract_snapshot_accepts_clean_zip(staged):
    base = staged / "services" / "svc-ok"
    base.mkdir(parents=True)
    (base / "snapshot.zip").write_bytes(_zip_with("app/main.py", b"print(1)"))

    out = asyncio.run(service_replication.extract_snapshot("svc-ok"))
    assert (out / "app" / "main.py").read_bytes() == b"print(1)"
