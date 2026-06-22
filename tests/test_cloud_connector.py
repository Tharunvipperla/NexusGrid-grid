"""A2 — cloud connector: URI classification, http/rclone download + upload,
run-spec ``inputs`` normalization, and run_replica wiring."""

from __future__ import annotations

import asyncio
import shutil

import httpx
import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.core.config import _normalize_run_spec
from nexus.runtime import cloud_connector as C
from nexus.runtime import replica_runner as R


# ---- classify --------------------------------------------------------------

@pytest.mark.parametrize("uri,kind", [
    ("http://example.com/x", "http"),
    ("https://example.com/x", "http"),
    ("HTTPS://EXAMPLE.com/x", "http"),
    ("gdrive:datasets/train.csv", "rclone"),
    ("s3-backup:bucket/k.bin", "rclone"),
    ("C:\\Users\\x\\f.bin", ""),       # windows drive, not a remote
    ("/home/u/f.bin", ""),             # local path
    ("./rel", ""),
    ("", ""),
])
def test_classify(uri, kind):
    assert C.classify(uri) == kind


# ---- http download (mocked httpx) ------------------------------------------

class _FakeResp:
    def __init__(self, chunks, status=200):
        self._chunks, self.status = chunks, status
        self.is_redirect = False
        self.headers = {}

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return self._resp


def _patch_httpx(monkeypatch, resp):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(resp))
    # These exercise download *mechanics* with a fake host; bypass the SSRF
    # host check (covered separately in test_security_ssrf.py).
    monkeypatch.setattr(C, "_host_blocked", lambda host: False)


def test_http_download_writes_file(monkeypatch, tmp_path):
    _patch_httpx(monkeypatch, _FakeResp([b"hello ", b"world"]))
    dest = tmp_path / "sub" / "out.bin"
    ok, why = asyncio.run(C.download("https://x/y", str(dest)))
    assert ok and why == ""
    assert dest.read_bytes() == b"hello world"  # parent dir auto-created


def test_http_download_error_returns_reason(monkeypatch, tmp_path):
    _patch_httpx(monkeypatch, _FakeResp([b""], status=500))
    ok, why = asyncio.run(C.download("https://x/y", str(tmp_path / "o")))
    assert not ok and why.startswith("http:")


def test_download_bad_uri():
    ok, why = asyncio.run(C.download("/local/path", "/tmp/o"))
    assert not ok and why == "bad_uri"


# ---- rclone download/upload (mocked subprocess) ----------------------------

class _FakeProc:
    def __init__(self, rc):
        self.returncode = rc

    async def communicate(self):
        return (b"", b"")


def _patch_rclone(monkeypatch, rc, available=True):
    calls = []

    async def _exec(*argv, **kw):
        calls.append(list(argv))
        return _FakeProc(rc)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(shutil, "which",
                        lambda n: "/usr/bin/rclone" if (available and n == "rclone") else None)
    return calls


def test_rclone_download_success(monkeypatch, tmp_path):
    calls = _patch_rclone(monkeypatch, rc=0)
    ok, why = asyncio.run(C.download("gdrive:data/x.bin", str(tmp_path / "x.bin")))
    assert ok and why == ""
    assert calls[0][:3] == ["rclone", "copyto", "gdrive:data/x.bin"]


def test_rclone_download_failure(monkeypatch, tmp_path):
    _patch_rclone(monkeypatch, rc=3)
    ok, why = asyncio.run(C.download("gdrive:data/x.bin", str(tmp_path / "x.bin")))
    assert not ok and why == "rclone_rc_3"


def test_rclone_download_unavailable(monkeypatch, tmp_path):
    _patch_rclone(monkeypatch, rc=0, available=False)
    ok, why = asyncio.run(C.download("gdrive:data/x.bin", str(tmp_path / "x.bin")))
    assert not ok and why == "rclone_unavailable"


def test_upload_src_missing():
    ok, why = asyncio.run(C.upload("/nope/missing", "gdrive:out.bin"))
    assert not ok and why == "src_missing"


def test_upload_http_unsupported(tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    ok, why = asyncio.run(C.upload(str(src), "https://x/y"))
    assert not ok and why == "http_upload_unsupported"


def test_upload_rclone_success(monkeypatch, tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    calls = _patch_rclone(monkeypatch, rc=0)
    ok, why = asyncio.run(C.upload(str(src), "s3:bucket/f"))
    assert ok and why == ""
    assert calls[0][:2] == ["rclone", "copyto"] and calls[0][-1] == "s3:bucket/f"


# ---- run-spec inputs normalization -----------------------------------------

def test_normalize_keeps_inputs_and_sanitizes_dest():
    spec = _normalize_run_spec({"cmd": "python /app.py", "inputs": [
        {"uri": "https://x/m.bin", "dest": "model.bin"},
        {"uri": "gdrive:d/t.csv", "dest": "data/t.csv"},
        {"uri": "https://x/e", "dest": "../evil"},   # traversal → dropped
        {"uri": "", "dest": "blank"},                # no uri → dropped
        {"uri": "https://x/n", "dest": ""},          # no dest → dropped
    ]})
    assert spec["inputs"] == [
        {"uri": "https://x/m.bin", "dest": "model.bin"},
        {"uri": "gdrive:d/t.csv", "dest": "data/t.csv"},
    ]


def test_normalize_inputs_only_is_dropped():
    # Inputs with nothing to run (no image/cmd/build) is not a run-spec.
    assert _normalize_run_spec({"image": "", "cmd": "", "inputs": [
        {"uri": "https://x/m", "dest": "m"}]}) == {}


def test_normalize_inputs_capped():
    many = [{"uri": f"https://x/{i}", "dest": f"f{i}"} for i in range(40)]
    spec = _normalize_run_spec({"cmd": "x", "inputs": many})
    assert len(spec["inputs"]) == 20


# ---- runner wiring ---------------------------------------------------------

def test_container_argv_mounts_inputs_dir():
    R._ensure_builtins()
    ctx = {"spec": {"image": "python:3.11-slim", "cmd": "", "env": [], "ports": []},
           "host_ports": [], "allow_outbound": False, "mem_mb": 256, "cpus": "1.0",
           "inputs_dir": "/tmp/nexus_inputs_abc"}
    argv = R._container_argv("docker", ctx)
    assert "-v" in argv
    assert "/tmp/nexus_inputs_abc:/nexus/inputs:ro" in argv


def test_run_replica_aborts_on_input_download_failure(monkeypatch):
    R._ensure_builtins()
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/docker" if n == "docker" else None)

    async def fake_fetch(provider_uuid, service_name):
        return {"name": service_name, "replicable": True,
                "run": {"image": "python:3.11-slim", "cmd": "python /app.py",
                        "inputs": [{"uri": "https://x/m.bin", "dest": "m.bin"}]}}
    monkeypatch.setattr(R, "_fetch_public_service", fake_fetch)
    monkeypatch.setitem(LOCAL_SETTINGS, "allowed_images", ["python"])

    async def fake_dl(uri, dest):
        return False, "http:Timeout"
    monkeypatch.setattr("nexus.runtime.cloud_connector.download", fake_dl)

    # A real spawn must never be reached.
    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not spawn when an input download fails")
    monkeypatch.setattr("subprocess.check_output", _boom)

    res = asyncio.run(R.run_replica("prov", "svc", "docker", False, True))
    assert res["ok"] is False
    assert res["error"].startswith("input_download_failed:http:Timeout")
