"""A1 — custom build context for runners: normalize, validate, fingerprint, build."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.core.config import _normalize_run_spec
from nexus.runtime import replica_runner as R


@pytest.fixture(autouse=True)
def _allow_python(monkeypatch):
    monkeypatch.setitem(LOCAL_SETTINGS, "allowed_images", ["python", "node"])
    monkeypatch.setitem(LOCAL_SETTINGS, "build_max_bytes", 5 * 1024 * 1024)
    yield


DF = "FROM python:3.11-slim\nRUN pip install flask\nCOPY app.py /\nCMD python /app.py\n"


# ---- normalization ---------------------------------------------------------

def test_normalize_keeps_build_and_sanitizes_paths():
    spec = _normalize_run_spec({
        "cmd": "python /app.py",
        "build": {"dockerfile": DF, "files": {
            "app.py": "print(1)", "sub/x.txt": "y",
            "../evil": "no", "/abs": "no", "..": "no",
        }},
    })
    b = spec["build"]
    assert b["dockerfile"].startswith("FROM python")
    assert set(b["files"]) == {"app.py", "sub/x.txt", "abs"}  # traversal stripped


def test_safe_build_path_rejects_windows_drive_anchor():
    """SECURITY F-009: a Windows drive letter / colon must be rejected — on
    Windows `base / 'C:/x'` escapes the base dir."""
    from nexus.core.config import _safe_build_path
    for bad in ["C:/Windows/Temp/evil", r"C:\Windows\Temp\evil", "x:y", "a/b:c"]:
        assert _safe_build_path(bad) == "", bad
    # legit relative paths still pass
    assert _safe_build_path("a/b.txt") == "a/b.txt"
    assert _safe_build_path("/abs/x") == "abs/x"   # leading slash made relative


def test_normalize_drops_drive_letter_file_key():
    spec = _normalize_run_spec({
        "cmd": "python /app.py",
        "build": {"dockerfile": DF, "files": {"ok.py": "1", "C:/evil": "no"}},
    })
    assert set(spec["build"]["files"]) == {"ok.py"}


def test_normalize_empty_dockerfile_drops_build():
    spec = _normalize_run_spec({"cmd": "x", "build": {"dockerfile": "  ", "files": {"a": "b"}}})
    assert "build" not in spec


def test_normalize_build_only_is_valid_run_spec():
    # A build context alone (no prebuilt image, no cmd) must NOT be dropped.
    spec = _normalize_run_spec({"image": "", "cmd": "", "ports": [8000],
                                "env": ["K=V"], "build": {"dockerfile": DF}})
    assert spec.get("build", {}).get("dockerfile", "").startswith("FROM python")
    assert spec["ports"] == [8000] and spec["env"] == ["K=V"]


def test_normalize_hard_caps_oversized_pieces():
    spec = _normalize_run_spec({
        "cmd": "x",
        "build": {"dockerfile": "FROM python\n" + "x" * 100000,
                  "files": {"big": "z" * 500000}},
    })
    assert len(spec["build"]["dockerfile"]) <= 65536
    assert len(spec["build"]["files"]["big"]) <= 262144


# ---- FROM parsing ----------------------------------------------------------

def test_from_bases_multistage_and_scratch():
    df = (
        "FROM --platform=linux/amd64 golang:1.22 AS builder\n"
        "RUN go build\n"
        "FROM scratch\n"
        "COPY --from=builder /app /app\n"
        "FROM builder\n"          # internal stage ref, not external
    )
    assert R._from_bases(df) == ["golang:1.22"]


def test_from_bases_detects_tab_and_odd_whitespace():
    """SECURITY F-008: Docker accepts a tab after FROM; the allowlist parser must
    too, or a multi-stage Dockerfile can hide its real (final) base image."""
    # Tab between FROM and the base.
    assert R._from_bases("FROM\tevil:latest\n") == ["evil:latest"]
    # Multi-stage where the FINAL output image is tab-hidden.
    df = "FROM python:3.11-slim AS build\nFROM\tevil/malicious:latest\n"
    assert R._from_bases(df) == ["python:3.11-slim", "evil/malicious:latest"]


def test_validate_rejects_tab_hidden_base(monkeypatch):
    """The tab-hidden disallowed base must fail validation, not pass it."""
    monkeypatch.setitem(LOCAL_SETTINGS, "allowed_images", ["python"])
    df = "FROM python:3.11-slim AS build\nFROM\tevil/malicious:latest\n"
    ok, why = R.validate_build({"dockerfile": df})
    assert not ok and why == "base_not_allowed:evil/malicious:latest"


# ---- validation ------------------------------------------------------------

def test_validate_ok():
    assert R.validate_build({"dockerfile": DF, "files": {}}) == (True, "")


def test_validate_no_dockerfile():
    ok, why = R.validate_build({"dockerfile": "", "files": {}})
    assert not ok and why == "no_dockerfile"


def test_validate_base_not_allowed(monkeypatch):
    monkeypatch.setitem(LOCAL_SETTINGS, "allowed_images", ["node"])
    ok, why = R.validate_build({"dockerfile": DF})
    assert not ok and why.startswith("base_not_allowed")


def test_validate_too_large(monkeypatch):
    monkeypatch.setitem(LOCAL_SETTINGS, "build_max_bytes", 100)
    ok, why = R.validate_build({"dockerfile": DF, "files": {"a": "x" * 500}})
    assert not ok and why.startswith("build_too_large")


def test_validate_no_from():
    ok, why = R.validate_build({"dockerfile": "RUN echo hi\n"})
    assert not ok and why == "no_from"


# ---- fingerprint -----------------------------------------------------------

def test_fingerprint_deterministic_and_content_sensitive():
    a = {"dockerfile": DF, "files": {"app.py": "print(1)"}}
    b = {"dockerfile": DF, "files": {"app.py": "print(2)"}}
    assert R.build_fingerprint(a) == R.build_fingerprint(dict(a))
    assert R.build_fingerprint(a) != R.build_fingerprint(b)


# ---- write build dir -------------------------------------------------------

def test_write_build_dir(tmp_path):
    R._write_build_dir({"dockerfile": DF, "files": {"app.py": "print(1)", "sub/c.txt": "z"}}, tmp_path)
    assert (tmp_path / "Dockerfile").read_text().startswith("FROM python")
    assert (tmp_path / "app.py").read_text() == "print(1)"
    assert (tmp_path / "sub" / "c.txt").read_text() == "z"


# ---- ensure_built_image (mocked engine) ------------------------------------

class _FakeProc:
    def __init__(self, rc): self._rc = rc; self.returncode = rc
    async def communicate(self): return (b"", b"")


def _spawn(rc_for):
    calls = []

    async def _exec(*argv, **kw):
        calls.append(list(argv))
        sub = argv[1]  # "image" (inspect) or "build"
        return _FakeProc(rc_for.get(sub, 0))
    return _exec, calls


def test_build_cache_hit_skips_build(monkeypatch):
    # `image inspect` returns 0 → already built → no `build` call.
    spawn, calls = _spawn({"image": 0})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tag, err = asyncio.run(R.ensure_built_image("docker", {"dockerfile": DF}))
    assert err == "" and tag.startswith("nexus_built_")
    assert not any(c[1] == "build" for c in calls)


def test_build_runs_when_not_cached(monkeypatch):
    # inspect → 1 (miss), build → 0 (success).
    spawn, calls = _spawn({"image": 1, "build": 0})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tag, err = asyncio.run(R.ensure_built_image("docker", {"dockerfile": DF}))
    assert err == "" and tag.startswith("nexus_built_")
    assert any(c[1] == "build" for c in calls)


def test_build_failure_returns_error(monkeypatch):
    spawn, _ = _spawn({"image": 1, "build": 1})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    tag, err = asyncio.run(R.ensure_built_image("docker", {"dockerfile": DF}))
    assert tag == "" and err == "build_failed"


def test_build_validation_failure_never_spawns(monkeypatch):
    monkeypatch.setitem(LOCAL_SETTINGS, "allowed_images", ["node"])

    async def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("must not spawn when validation fails")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    tag, err = asyncio.run(R.ensure_built_image("docker", {"dockerfile": DF}))
    assert tag == "" and err.startswith("base_not_allowed")


# ---- run_replica build wiring ----------------------------------------------

def test_run_replica_takes_build_path_and_surfaces_failure(monkeypatch):
    # A run-spec with a build context routes through ensure_built_image; a
    # build failure surfaces as build_failed BEFORE anything is spawned.
    R._ensure_builtins()
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/docker" if n == "docker" else None)

    async def fake_fetch(provider_uuid, service_name):
        return {"name": service_name, "replicable": True,
                "run": {"image": "", "cmd": "python /app.py", "build": {"dockerfile": DF}}}
    monkeypatch.setattr(R, "_fetch_public_service", fake_fetch)

    seen = {}

    async def fake_build(engine, build):
        seen["engine"] = engine
        return "", "base_not_allowed:python:3.11-slim"
    monkeypatch.setattr(R, "ensure_built_image", fake_build)

    res = asyncio.run(R.run_replica("prov", "svc", "docker", False, True))
    assert res["ok"] is False
    assert res["error"].startswith("build_failed:base_not_allowed")
    assert seen["engine"] == "docker"


def test_run_replica_requires_consent():
    res = asyncio.run(R.run_replica("prov", "svc", "docker", False, False))
    assert res == {"ok": False, "error": "consent_required"}
