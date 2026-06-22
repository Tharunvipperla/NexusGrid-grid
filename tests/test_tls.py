"""TLS bootstrap and fingerprint helper tests (Wave 4 Step 6 / item 2.9)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.security import tls as tls_mod


@pytest.fixture
def isolated_cert_dir(tmp_path, monkeypatch):
    """Redirect cert/key files to a tmp dir and reset caches."""
    monkeypatch.setattr(tls_mod, "BASE_DIR", tmp_path)
    tls_mod._reset_for_testing()
    yield tmp_path
    tls_mod._reset_for_testing()


# ---------------------------------------------------------------------------
# ensure_local_cert
# ---------------------------------------------------------------------------

def test_ensure_local_cert_creates_files(isolated_cert_dir):
    cert, key = tls_mod.ensure_local_cert()
    assert cert.exists()
    assert key.exists()
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert key.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_ensure_local_cert_idempotent(isolated_cert_dir):
    cert1, key1 = tls_mod.ensure_local_cert()
    original_cert = cert1.read_bytes()
    original_key = key1.read_bytes()
    cert2, key2 = tls_mod.ensure_local_cert()
    assert cert2.read_bytes() == original_cert
    assert key2.read_bytes() == original_key


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------

def test_compute_fingerprint_matches_openssl_format(isolated_cert_dir):
    cert, _ = tls_mod.ensure_local_cert()
    pem = cert.read_bytes()
    fp = tls_mod.compute_fingerprint(pem)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    # Recomputing yields the same result.
    assert tls_mod.compute_fingerprint(pem) == fp


def test_compute_fingerprint_accepts_str(isolated_cert_dir):
    cert, _ = tls_mod.ensure_local_cert()
    pem_str = cert.read_text()
    pem_bytes = cert.read_bytes()
    assert tls_mod.compute_fingerprint(pem_str) == tls_mod.compute_fingerprint(pem_bytes)


def test_compute_fingerprint_changes_when_cert_rotated(isolated_cert_dir):
    cert, key = tls_mod.ensure_local_cert()
    fp1 = tls_mod.get_local_fingerprint()
    # Force a fresh cert.
    cert.unlink()
    key.unlink()
    tls_mod._reset_for_testing()
    tls_mod.ensure_local_cert()
    fp2 = tls_mod.get_local_fingerprint()
    assert fp1 != fp2


# ---------------------------------------------------------------------------
# get_local_fingerprint
# ---------------------------------------------------------------------------

def test_get_local_fingerprint_caches(isolated_cert_dir):
    fp1 = tls_mod.get_local_fingerprint()
    fp2 = tls_mod.get_local_fingerprint()
    assert fp1 == fp2
    # The cache means a second call shouldn't re-read the file.
    with patch.object(tls_mod, "ensure_local_cert") as m:
        tls_mod.get_local_fingerprint()
        assert m.call_count == 0


def test_get_local_fingerprint_generates_on_first_call(isolated_cert_dir):
    assert not tls_mod.cert_path().exists()
    fp = tls_mod.get_local_fingerprint()
    assert tls_mod.cert_path().exists()
    assert fp == tls_mod.compute_fingerprint(tls_mod.cert_path().read_bytes())
