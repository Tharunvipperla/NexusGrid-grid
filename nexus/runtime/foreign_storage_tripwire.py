"""Batch C: tripwire that detects out-of-workflow changes to a host's
encrypted chunk directory.

Threat model: the host stores someone else's ciphertext on disk. The
depositor wants to know if the host operator has poked at the bundle.
A perfect detector would need OS-level filesystem-event APIs that vary
per platform; this module ships a minimal, cross-platform alternative:

* When a deposit finishes landing, ``record_baseline`` stats every
  ``chunk_*.enc`` file and writes a sidecar ``.tripwire.json`` recording
  ``(size, mtime_ns)`` for each chunk.
* The scheduler's 2-second lifecycle pass calls ``check_deposit`` for
  every ``stored`` deposit. If any chunk's stat differs from the
  baseline (size changed, mtime moved forward, file gone, file added),
  the function emits ``storage.unauthorized_access_detected`` and
  returns the list of changed chunk names.
* Legit operations within the workflow (purge / eviction / cloud
  upload) call ``clear_baseline`` so the deposit drops out of the
  check set without firing.

This only catches *writes* to the directory — pure reads of ciphertext
do not change file metadata. Read detection would need true filesystem
events; see the "Aspirational" section of ``FUTURE_HARDENED_COMPUTE.md``
for the wider hardening path. The cheap write-tampering tripwire still
catches the most common attacker action (replacing or modifying
chunks), which is what the user asked us to surface.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("nexus.runtime.foreign_storage_tripwire")

_TRIPWIRE_FILENAME = ".tripwire.json"


def _baseline_path(deposit_dir: Path) -> Path:
    return deposit_dir / _TRIPWIRE_FILENAME


def _stat_chunks(deposit_dir: Path) -> dict[str, dict[str, int]]:
    """Return ``{chunk_name: {"size": int, "mtime_ns": int}}`` for every
    ``chunk_*.enc`` file under *deposit_dir*."""
    snapshot: dict[str, dict[str, int]] = {}
    if not deposit_dir.exists():
        return snapshot
    for chunk in sorted(deposit_dir.glob("chunk_*.enc")):
        try:
            st = chunk.stat()
            snapshot[chunk.name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        except OSError:
            continue
    return snapshot


def record_baseline(deposit_dir: Path | str) -> None:
    """Stat all chunks and write the sidecar baseline to disk.

    Safe to call multiple times — overwrites the previous baseline. The
    sidecar lives next to the chunks so it survives node restarts and
    the lifecycle pass can verify against it without keeping the data
    in RAM.
    """
    dpath = Path(deposit_dir)
    if not dpath.exists():
        return
    snapshot = _stat_chunks(dpath)
    try:
        _baseline_path(dpath).write_text(
            json.dumps({"chunks": snapshot}), encoding="utf-8"
        )
    except OSError as exc:
        _log.debug("tripwire baseline write failed for %s: %s", dpath, exc)


def clear_baseline(deposit_dir: Path | str) -> None:
    """Remove the tripwire sidecar — call after every legit purge /
    eviction so the lifecycle pass stops checking."""
    dpath = Path(deposit_dir)
    try:
        _baseline_path(dpath).unlink(missing_ok=True)
    except OSError:
        pass


def _load_baseline(deposit_dir: Path) -> dict[str, dict[str, int]] | None:
    p = _baseline_path(deposit_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    chunks = data.get("chunks") if isinstance(data, dict) else None
    return chunks if isinstance(chunks, dict) else None


def check_deposit(deposit_dir: Path | str) -> list[str]:
    """Compare the current chunk stats against the sidecar baseline.

    Returns a list of chunk names whose ``size`` or ``mtime_ns``
    differs, are missing, or are new. An empty list means the deposit
    is untouched (or has no baseline yet — caller decides whether to
    treat that as "no tripwire armed").
    """
    dpath = Path(deposit_dir)
    baseline = _load_baseline(dpath)
    if baseline is None:
        return []
    current = _stat_chunks(dpath)
    changes: list[str] = []
    for name, expected in baseline.items():
        cur = current.get(name)
        if cur is None:
            changes.append(name)
            continue
        if (
            int(cur.get("size", -1)) != int(expected.get("size", -2))
            or int(cur.get("mtime_ns", -1)) != int(expected.get("mtime_ns", -2))
        ):
            changes.append(name)
    for name in current:
        if name not in baseline:
            changes.append(name)
    return changes


def baseline_exists(deposit_dir: Path | str) -> bool:
    return _baseline_path(Path(deposit_dir)).exists()
