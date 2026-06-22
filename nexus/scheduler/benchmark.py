"""Self-benchmark for fitness ranking.

Two cheap measurements run in a worker thread:

* ``cpu_score()`` — fixed-size pure-Python multiply-accumulate loop. Wall
  time → MFLOPS approximation. Pure Python on purpose: NumPy is not in
  the base requirements, and we want every node (including PyInstaller
  bundles) to produce a comparable number.

* ``io_score()`` — write+read a 64 MB scratch file in tmp, report MB/s.

Combined into a single weighted scalar that the scheduler blends into
:func:`worker_fit_score`. Higher is better; 0.0 means "not yet
benchmarked" and is treated as last-place.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import time
from typing import Any

_log = logging.getLogger("nexus.scheduler.benchmark")

_CPU_OUTER = 200_000
_IO_PAYLOAD_BYTES = 64 * 1024 * 1024

# Score normalization — tuned so a mid-2020s laptop lands near 100.
_CPU_REFERENCE_MFLOPS = 50.0
_IO_REFERENCE_MB_S = 200.0
_CPU_WEIGHT = 0.7
_IO_WEIGHT = 0.3


def cpu_score() -> float:
    """Return a CPU score (MFLOPS-like)."""
    a, b, c = 1.000_001, 0.999_999, 0.0
    iters = _CPU_OUTER
    start = time.perf_counter()
    for _ in range(iters):
        c = a * b + c
        a = c - b
        b = a + 1.0
    elapsed = max(1e-6, time.perf_counter() - start)
    # 3 fp ops per iter
    return (iters * 3.0) / elapsed / 1_000_000.0


def io_score(payload_bytes: int = _IO_PAYLOAD_BYTES) -> float:
    """Return scratch-disk throughput in MB/s (write+read average)."""
    fd, path = tempfile.mkstemp(prefix="nexus_bench_", suffix=".bin")
    os.close(fd)
    try:
        chunk = secrets.token_bytes(64 * 1024)
        chunks_needed = max(1, payload_bytes // len(chunk))
        # Write
        start = time.perf_counter()
        with open(path, "wb") as f:
            for _ in range(chunks_needed):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        write_secs = max(1e-6, time.perf_counter() - start)
        # Read
        start = time.perf_counter()
        with open(path, "rb") as f:
            while f.read(1024 * 1024):
                pass
        read_secs = max(1e-6, time.perf_counter() - start)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    written = chunks_needed * len(chunk)
    write_mb_s = (written / write_secs) / (1024 * 1024)
    read_mb_s = (written / read_secs) / (1024 * 1024)
    return (write_mb_s + read_mb_s) / 2.0


def combined_score(cpu_mflops: float, io_mb_s: float) -> float:
    """Blend CPU + IO into a single comparable scalar."""
    cpu_norm = (cpu_mflops / _CPU_REFERENCE_MFLOPS) * 100.0
    io_norm = (io_mb_s / _IO_REFERENCE_MB_S) * 100.0
    return round(_CPU_WEIGHT * cpu_norm + _IO_WEIGHT * io_norm, 2)


def run_benchmark() -> dict[str, Any]:
    """Run all benches synchronously. Designed for ``asyncio.to_thread``."""
    started = time.time()
    try:
        cpu = cpu_score()
    except Exception as exc:
        _log.warning("cpu_score failed: %s", exc)
        cpu = 0.0
    try:
        io = io_score()
    except Exception as exc:
        _log.warning("io_score failed: %s", exc)
        io = 0.0
    return {
        "score": combined_score(cpu, io),
        "cpu_mflops": round(cpu, 2),
        "io_mb_s": round(io, 2),
        "ran_at": int(started),
    }


__all__ = ["cpu_score", "io_score", "combined_score", "run_benchmark"]
