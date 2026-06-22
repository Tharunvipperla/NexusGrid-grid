"""Pre-execution regex scan for known-bad patterns in user workspaces.

Extracted from Phase-1/node_modified.py (lines 1371-1455) and hardened in
with three additions:

1. **Profile-aware reads.** Risky text extensions (``.py``, ``.sh``,
   ``.ps1``, ``.js``, ``.ipynb``) are scanned up to 8 MB; everything else
   stays at the original 1 MB cap.

2. **Base64 second pass.** Long base64-looking runs are decoded and the
   regex scan is re-applied to the decoded text. This catches
   ``echo <base64> | base64 -d | sh`` style payloads where the script
   body itself is encoded.

3. **Entropy heuristic.** When ``profile == "maximum"`` we Shannon-score
   the first 256 KB of each non-text file ≥ 64 KB. Anything above 7.5
   bits/byte is flagged as a high-entropy binary — packed executables,
   encrypted payloads, etc.

The scan stays cheap: regex over textual files, a single 256 KB byte
sample for entropy, and a length-bounded base64 second pass. Findings are
still advisory; the executor decides whether to fail the task.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import re
from collections import Counter
from typing import TypedDict


class Finding(TypedDict):
    file: str
    threat: str
    sample: str


_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(bash\s+-i\s+>&|/dev/tcp/|nc\s+-[elp]|ncat\s+-e|"
            r"python.*socket.*connect)",
            re.I,
        ),
        "reverse_shell",
    ),
    (
        re.compile(r"(xmrig|cryptonight|stratum\+tcp|minerd|cgminer|ethminer)", re.I),
        "crypto_miner",
    ),
    (
        re.compile(
            r"(curl|wget|Invoke-WebRequest).*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", re.I
        ),
        "suspicious_network",
    ),
    (
        re.compile(
            # ``\.env`` only fires when it looks like a filesystem path —
            # i.e. preceded by whitespace, quote, slash, or start-of-line and
            # NOT preceded by an identifier char (so ``process.env``,
            # ``obj.env``, ``data.env`` are skipped). This kills the JS
            # false-positive while still catching ``cat .env``, ``open(".env")``
            # and similar.
            r"(/etc/shadow|/etc/passwd|~/\.ssh|\.aws/credentials|"
            r"(?:^|[\s'\"`/])\.env(?:\b|$))",
            re.I,
        ),
        "sensitive_path_access",
    ),
    (
        re.compile(r"base64\s+(-d|--decode).*\|\s*(sh|bash|python|exec)", re.I),
        "encoded_payload_exec",
    ),
    (
        re.compile(r"(rm\s+-rf\s+/|:\(\)\{\s*:\|:&\s*\};:|fork\s*bomb)", re.I),
        "destructive_command",
    ),
)
"""Each entry is ``(compiled_pattern, threat_label)``. Ordering is not
significant."""


_SCAN_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".bash", ".js", ".rb", ".pl", ".ps1", ".bat", ".cmd",
    ".php", ".lua", ".r", ".ipynb",
})
"""File extensions worth regex-scanning. Binaries and archives are scanned
via the entropy heuristic instead."""


_RISKY_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sh", ".ps1", ".js", ".ipynb",
})
"""Extensions whose 1 MB read cap is lifted to 8 MB — ``.ipynb`` files
routinely exceed 1 MB because of embedded outputs."""


_MAX_BYTES_PER_FILE = 1024 * 1024              # 1 MB default
_MAX_BYTES_RISKY = 8 * 1024 * 1024             # 8 MB for risky extensions
_ENTROPY_SAMPLE_BYTES = 256 * 1024             # bytes read for entropy score
_ENTROPY_MIN_FILE_SIZE = 64 * 1024             # files smaller than this are ignored
_ENTROPY_THRESHOLD = 7.5                       # bits/byte; random ≈ 8.0
_BASE64_MIN_LEN = 80                           # ignore short base64-looking runs
_BASE64_MAX_DECODED = 1024 * 1024              # cap each decoded segment at 1 MB

_BASE64_RUN_RE = re.compile(rb"[A-Za-z0-9+/]{80,}={0,2}")


def is_scan_required(profile: str, enable_task_scanning: bool) -> bool:
    """Decide whether the scan must run for the current task.

    Maximum-profile workers cannot opt out of scanning — the
    ``enable_task_scanning`` toggle is ignored in that case. Other
    profiles honour the toggle.
    """
    if profile == "maximum":
        return True
    return bool(enable_task_scanning)


async def scan_workspace_for_threats(
    workspace_dir: str, profile: str = "standard"
) -> list[Finding]:
    """Walk *workspace_dir* and return a list of findings.

    Empty list means nothing suspicious was found. ``profile`` controls
    the entropy heuristic — high-entropy binaries are only flagged under
    ``maximum``.
    """
    findings: list[Finding] = []
    for root, _dirs, files in os.walk(workspace_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SCAN_EXTENSIONS:
                findings.extend(_scan_text_file(fpath, workspace_dir))
            elif not ext and os.access(fpath, os.X_OK):
                findings.append(
                    {
                        "file": os.path.relpath(fpath, workspace_dir),
                        "threat": "unexpected_binary",
                        "sample": fname,
                    }
                )
            elif profile == "maximum":
                hit = _scan_entropy(fpath, workspace_dir)
                if hit is not None:
                    findings.append(hit)
    return findings


def _scan_text_file(fpath: str, workspace_dir: str) -> list[Finding]:
    ext = os.path.splitext(fpath)[1].lower()
    max_bytes = _MAX_BYTES_RISKY if ext in _RISKY_EXTENSIONS else _MAX_BYTES_PER_FILE
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(max_bytes)
    except Exception:
        return []

    rel = os.path.relpath(fpath, workspace_dir)
    out: list[Finding] = []
    seen: set[str] = set()

    for pattern, threat_type in _THREAT_PATTERNS:
        matches = pattern.findall(content)
        if matches and threat_type not in seen:
            out.append(
                {
                    "file": rel,
                    "threat": threat_type,
                    "sample": str(matches[0])[:100],
                }
            )
            seen.add(threat_type)

    for decoded_text in _decode_base64_runs(content):
        for pattern, threat_type in _THREAT_PATTERNS:
            label = f"encoded_{threat_type}"
            if label in seen:
                continue
            match = pattern.search(decoded_text)
            if match:
                out.append(
                    {
                        "file": rel,
                        "threat": label,
                        "sample": match.group(0)[:100],
                    }
                )
                seen.add(label)

    return out


def _decode_base64_runs(content: str) -> list[str]:
    """Yield decoded text for every long base64 run in *content*.

    Garbage that doesn't decode to mostly-printable text is dropped — it
    would only feed false positives.
    """
    decoded_chunks: list[str] = []
    for match in _BASE64_RUN_RE.finditer(content.encode("ascii", "ignore")):
        chunk = match.group(0)
        if len(chunk) < _BASE64_MIN_LEN:
            continue
        try:
            raw = base64.b64decode(chunk, validate=False)
        except (binascii.Error, ValueError):
            continue
        if not raw:
            continue
        try:
            text = raw[:_BASE64_MAX_DECODED].decode("utf-8", errors="ignore")
        except Exception:
            continue
        if not text:
            continue
        printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
        if printable / max(1, len(text)) < 0.85:
            continue
        decoded_chunks.append(text)
    return decoded_chunks


def _scan_entropy(fpath: str, workspace_dir: str) -> Finding | None:
    try:
        size = os.path.getsize(fpath)
    except OSError:
        return None
    if size < _ENTROPY_MIN_FILE_SIZE:
        return None
    try:
        with open(fpath, "rb") as f:
            buf = f.read(_ENTROPY_SAMPLE_BYTES)
    except OSError:
        return None
    if not buf:
        return None
    score = _shannon_entropy(buf)
    if score < _ENTROPY_THRESHOLD:
        return None
    return {
        "file": os.path.relpath(fpath, workspace_dir),
        "threat": "high_entropy_binary",
        "sample": f"entropy={score:.2f} size={size}",
    }


def _shannon_entropy(buf: bytes) -> float:
    if not buf:
        return 0.0
    counts = Counter(buf)
    total = len(buf)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


__all__ = ["Finding", "is_scan_required", "scan_workspace_for_threats"]
