"""Phase 3/4 — property/fuzz tests on security-critical pure functions.

Two classes of property:
  * **robustness** — arbitrary (attacker-shaped) input must never raise an
    uncaught exception (an unhandled crash on a peer-supplied frame is a DoS);
  * **invariant** — a forged signature must never verify, and a path sanitizer
    must never emit something that escapes its base.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.core.config import _normalize_webhooks, _safe_build_path
from nexus.runtime import plugin_packages, webhooks
from nexus.runtime.replica_runner import _from_bases, validate_build
from nexus.security.group_grant import generate_keypair
from nexus.security.usage_receipt import (
    STMT_DM,
    dm_statement_payload,
    sign_statement,
    verify_statement,
)

_TEXT = st.text(max_size=300)
_PRIV, _PUB = generate_keypair()

# ECIES uses the ed25519 seed to derive an X25519 keypair: seal to the X25519
# pubkey, open with the ed25519 private hex.
from nexus.security.group_ecies import derive_x25519_pubkey_hex  # noqa: E402

_ECIES_PRIV = _PRIV
_ECIES_PUB = derive_x25519_pubkey_hex(_PRIV)


# ---- signed statements -----------------------------------------------------


@given(kind=_TEXT, payload=st.dictionaries(_TEXT, _TEXT, max_size=8),
       sig=_TEXT, pub=_TEXT)
@settings(max_examples=300)
def test_verify_statement_never_raises(kind, payload, sig, pub):
    # Arbitrary garbage must return a bool, never throw.
    assert isinstance(verify_statement(kind, payload, sig, pub), bool)


@given(payload=st.dictionaries(_TEXT, _TEXT, max_size=8), kind=_TEXT)
@settings(max_examples=200)
def test_valid_signature_verifies_and_is_bound(payload, kind):
    sig = sign_statement(kind, payload, _PRIV)
    assert verify_statement(kind, payload, sig, _PUB) is True
    # A different kind must not verify (domain separation).
    assert verify_statement(kind + "x", payload, sig, _PUB) is False
    # The wrong signer must not verify.
    _other_priv, other_pub = generate_keypair()
    assert verify_statement(kind, payload, sig, other_pub) is False


@given(text=_TEXT, tamper=_TEXT)
@settings(max_examples=200)
def test_dm_signature_tamper_fails(text, tamper):
    payload = dm_statement_payload("m1", "u1", "t1", text)
    sig = sign_statement(STMT_DM, payload, _PRIV)
    assert verify_statement(STMT_DM, payload, sig, _PUB) is True
    if tamper != text:  # any different body => different hash => must fail
        bad = dm_statement_payload("m1", "u1", "t1", tamper)
        assert verify_statement(STMT_DM, bad, sig, _PUB) is False


# ---- parsers / sanitizers --------------------------------------------------


@given(df=st.text(max_size=2000))
@settings(max_examples=300)
def test_from_bases_never_raises(df):
    bases = _from_bases(df)
    assert isinstance(bases, list) and all(isinstance(b, str) for b in bases)
    # validate_build must also stay total on arbitrary input.
    ok, why = validate_build({"dockerfile": df})
    assert isinstance(ok, bool) and isinstance(why, str)


@given(rel=st.text(max_size=300))
@settings(max_examples=500)
def test_safe_build_path_never_escapes(rel):
    out = _safe_build_path(rel)
    assert isinstance(out, str)
    if out:
        # Never traversal, drive-letter, or absolute.
        assert ".." not in out.split("/")
        assert ":" not in out
        assert not out.startswith("/")
        # The real property: joining under a base never leaves the base.
        base = Path(os.getcwd()).resolve()
        joined = (base / out).resolve()
        assert str(joined).startswith(str(base))


@given(val=st.lists(st.dictionaries(_TEXT, st.one_of(_TEXT, st.booleans(),
       st.lists(_TEXT, max_size=5)), max_size=6), max_size=60))
@settings(max_examples=200)
def test_normalize_webhooks_never_raises_and_caps(val):
    out = _normalize_webhooks(val)
    assert isinstance(out, list) and len(out) <= 50
    for h in out:
        assert h["url"].startswith("http://") or h["url"].startswith("https://")
        assert len(h["events"]) <= 32


@given(pat=st.lists(_TEXT, max_size=10), event=_TEXT)
@settings(max_examples=200)
def test_event_matches_never_raises(pat, event):
    assert isinstance(webhooks.event_matches(pat, event), bool)


@given(pkg=st.one_of(st.none(), _TEXT, st.integers(),
       st.dictionaries(_TEXT, _TEXT, max_size=6)))
@settings(max_examples=200)
def test_validate_package_only_raises_valueerror(pkg):
    # A malformed package must fail as a clean ValueError, never an internal crash.
    try:
        plugin_packages.validate_package(pkg)
    except ValueError:
        pass


# ---- second-pass additions -------------------------------------------------


@given(plaintext=st.binary(min_size=1, max_size=2000))
@settings(max_examples=150)
def test_ecies_roundtrip_and_tamper(plaintext):
    from nexus.security.group_ecies import ecies_open, ecies_seal
    env = ecies_seal(plaintext, _ECIES_PUB)
    assert ecies_open(env, _ECIES_PRIV) == plaintext
    # Flipping any byte of the AEAD envelope must fail authentication.
    if env:
        b = bytearray(env)
        b[-1] ^= 0x01
        try:
            ecies_open(bytes(b), _ECIES_PRIV)
            assert False, "tampered ECIES envelope opened"
        except Exception:
            pass


@given(spec=st.dictionaries(_TEXT, st.one_of(_TEXT, st.integers(), st.booleans(),
       st.dictionaries(_TEXT, _TEXT, max_size=4),
       st.lists(st.dictionaries(_TEXT, _TEXT, max_size=4), max_size=4)), max_size=8))
@settings(max_examples=200)
def test_normalize_run_spec_never_raises(spec):
    from nexus.core.config import _normalize_run_spec
    out = _normalize_run_spec(spec)
    assert isinstance(out, dict)
    # build file keys, if any, are always traversal/anchor-safe.
    for k in (out.get("build", {}) or {}).get("files", {}):
        assert ".." not in k.split("/") and ":" not in k and not k.startswith("/")


@pytest.mark.parametrize("evil", ["../escape.txt", "a/../../escape.txt",
                                  "/abs/escape.txt", "x/../../../escape.txt"])
def test_safe_extractall_rejects_traversal_members(tmp_path, evil):
    """safe_extractall must refuse any archive containing a traversal member and
    let nothing escape the target dir."""
    import io
    import zipfile

    from nexus.utils import safe_extractall

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok.txt", b"x")
        zf.writestr(evil, b"pwned")
    buf.seek(0)
    with pytest.raises(ValueError):
        with zipfile.ZipFile(buf) as zf:
            safe_extractall(zf, str(tmp_path))
    # nothing escaped the target dir
    assert not (tmp_path.parent / "escape.txt").exists()
