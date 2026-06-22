"""Tests for the signed central auto-update path — root + per-release delegated
keys. Covers the chain of trust (root → cert → release key → binary hash),
expiry, revocation, version comparison, and the check() flow.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import nexus.security.app_update as au
from nexus import __version__
from nexus.api.diagnostics import health_check
from nexus.runtime import updater


def _key():
    return Ed25519PrivateKey.generate()


def _pub_b64(sk):
    return base64.b64encode(
        sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_manifest(root, version="1.1.0", *, rel=None, days=90, **over):
    """Build a fully chained, signed manifest (rel key certified by root)."""
    rel = rel or _key()
    now = datetime.now(timezone.utc)
    rel_pub = _pub_b64(rel)
    cert = {
        "signing_pubkey": rel_pub,
        "key_id": hashlib.sha256(base64.b64decode(rel_pub)).hexdigest()[:16],
        "not_after": _iso(now + timedelta(days=days)),
        "created": _iso(now),
    }
    cert_sig = base64.b64encode(root.sign(au.cert_bytes(cert))).decode()
    m = {
        "version": version, "url": "file://x", "sha256": "abc",
        "min_version": "0.0.0", "notes_url": "http://notes",
        "cert": cert, "cert_sig": cert_sig,
    }
    m.update(over)
    m["sig"] = base64.b64encode(rel.sign(au.canonical_bytes(m))).decode()
    return m


def _use_root(monkeypatch):
    root = _key()
    monkeypatch.setattr(au, "ROOT_PUBKEY_B64", _pub_b64(root))
    return root


# --- the chain of trust ----------------------------------------------------

def test_valid_chain_verifies(monkeypatch):
    root = _use_root(monkeypatch)
    ok, reason = au.verify_release(make_manifest(root))
    assert ok and reason == ""


def test_tampered_manifest_rejected(monkeypatch):
    root = _use_root(monkeypatch)
    m = make_manifest(root)
    m["version"] = "9.9.9"           # change a fact after signing
    ok, reason = au.verify_release(m)
    assert not ok and "release key" in reason


def test_wrong_root_rejected(monkeypatch):
    real_root = _use_root(monkeypatch)
    other_root = _key()
    m = make_manifest(other_root)    # certified by a root the app doesn't trust
    ok, reason = au.verify_release(m)
    assert not ok and "root" in reason


def test_expired_cert_rejected(monkeypatch):
    root = _use_root(monkeypatch)
    m = make_manifest(root, days=-1)  # cert already expired
    ok, reason = au.verify_release(m)
    assert not ok and "expired" in reason


def test_revoked_key_rejected(monkeypatch):
    root = _use_root(monkeypatch)
    m = make_manifest(root)
    monkeypatch.setattr(au, "REVOKED_KEY_IDS", frozenset({m["cert"]["key_id"]}))
    ok, reason = au.verify_release(m)
    assert not ok and "revoked" in reason


def test_uncertified_key_cannot_sign(monkeypatch):
    root = _use_root(monkeypatch)
    rel = _key()
    m = make_manifest(root, rel=rel)
    # attacker re-signs the manifest with a different key but keeps the old cert
    evil = _key()
    m["sig"] = base64.b64encode(evil.sign(au.canonical_bytes(m))).decode()
    ok, reason = au.verify_release(m)
    assert not ok


# --- version comparison ----------------------------------------------------

def test_ver_tuple_ordering():
    assert updater._ver_tuple("1.2.3") == (1, 2, 3)
    assert updater._ver_tuple("0.3.0") > updater._ver_tuple("0.2.9")
    assert updater._ver_tuple("1.0.0") > updater._ver_tuple("0.9.9")


# --- check() flow ----------------------------------------------------------

def test_manifest_url_default_and_override(monkeypatch):
    monkeypatch.delenv("NEXUS_UPDATE_MANIFEST_URL", raising=False)
    assert updater.manifest_url() == updater.DEFAULT_MANIFEST_URL  # unset → baked default
    monkeypatch.setenv("NEXUS_UPDATE_MANIFEST_URL", "http://x/m.json")
    assert updater.manifest_url() == "http://x/m.json"            # env overrides
    monkeypatch.setenv("NEXUS_UPDATE_MANIFEST_URL", "")
    assert updater.manifest_url() == ""                            # empty → disabled


def test_check_no_manifest(monkeypatch):
    monkeypatch.setenv("NEXUS_UPDATE_MANIFEST_URL", "")  # empty = disabled
    r = asyncio.run(updater.check())
    assert r["available"] is False and r["current"] == r["latest"]


def test_check_detects_signed_update(monkeypatch, tmp_path):
    root = _use_root(monkeypatch)
    monkeypatch.setattr(updater, "__version__", "1.0.0")
    m = make_manifest(root, version="1.1.0")
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps(m))
    monkeypatch.setenv("NEXUS_UPDATE_MANIFEST_URL", str(mf))
    r = asyncio.run(updater.check())
    assert r["available"] is True
    assert r["latest"] == "1.1.0"
    assert r["notes_url"] == "http://notes"


def test_check_rejects_tampered_or_wrong_root(monkeypatch, tmp_path):
    root = _use_root(monkeypatch)
    monkeypatch.setattr(updater, "__version__", "1.0.0")
    m = make_manifest(_key(), version="1.1.0")   # wrong root
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps(m))
    monkeypatch.setenv("NEXUS_UPDATE_MANIFEST_URL", str(mf))
    assert asyncio.run(updater.check())["available"] is False


# --- /health exposes the version ------------------------------------------

def test_health_reports_version():
    r = asyncio.run(health_check())
    assert r["status"] == "ok" and r["version"] == __version__
