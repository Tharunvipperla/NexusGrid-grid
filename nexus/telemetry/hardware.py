"""CPU / RAM / GPU / network sampling.

Extracted from node_modified.py (lines 289-354, plus GPU query
helpers at 357-…). Kept narrow for Step 5: only the pieces that everything
upstream of runtime needs (bandwidth, GPU detection). Runtime-specific GPU
scheduling queries stay in ``runtime/`` where they belong.

GPU detection probes ``nvidia-smi`` / ``rocm-smi`` **once** at first call
and caches the result. The cache is process-wide so repeated scheduling
decisions don't re-shell-out.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import TypedDict

from nexus.core import LOCAL_SETTINGS

_log = logging.getLogger("nexus.telemetry.hardware")


class NetIoRate(TypedDict):
    sent_per_sec: float
    recv_per_sec: float
    total_sent: int
    total_recv: int


_gpu_detected: bool | None = None
_gpu_vendor: str | None = None
_net_io_last = {"t": 0.0, "sent": 0, "recv": 0}
_net_io_rate = {"sent_per_sec": 0.0, "recv_per_sec": 0.0}


def detect_gpu() -> bool:
    """Return ``True`` if any usable GPU is present.

    Checks NVIDIA (``nvidia-smi``) then AMD (``rocm-smi``). Vendor is
    cached in :func:`gpu_vendor` for later consumers.
    """
    global _gpu_detected, _gpu_vendor
    if _gpu_detected is not None:
        return _gpu_detected
    for vendor, argv in (
        ("nvidia", ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]),
        ("amd", ["rocm-smi", "--showproductname"]),
    ):
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            _gpu_detected = True
            _gpu_vendor = vendor
            _log.info("GPU detected (%s)", vendor)
            return True
    _gpu_detected = False
    _gpu_vendor = None
    return False


def gpu_vendor() -> str | None:
    """Return the detected GPU vendor (``"nvidia"``/``"amd"``) or ``None``."""
    if _gpu_detected is None:
        detect_gpu()
    return _gpu_vendor


def get_gpu_stats() -> dict:
    """Return live GPU utilization + memory stats, or ``{}`` if unavailable.

    NVIDIA uses ``nvidia-smi --query-gpu=...``; AMD uses ``rocm-smi --json``.
    Caller-visible keys: ``gpu_util``, ``gpu_mem_used_mb``,
    ``gpu_mem_free_mb``, ``gpu_mem_total_mb``, ``gpu_name``,
    ``dispatch_gpu_cap_mb`` (clamped by ``max_gpu_pct`` setting).
    """
    if not detect_gpu():
        return {}
    max_gpu_pct = int(LOCAL_SETTINGS.get("max_gpu_pct", 80) or 80)

    if _gpu_vendor == "nvidia":
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.free,memory.total,name",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}
            line = result.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                return {}
            gpu_util, mem_used, mem_free, mem_total = (
                int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]),
            )
            gpu_name = parts[4]
            gpu_cap_mb = int(mem_total * (max_gpu_pct / 100.0))
            dispatch_gpu_mb = min(gpu_cap_mb, max(0, mem_free - 256))
            return {
                "gpu_util": gpu_util,
                "gpu_mem_used_mb": mem_used,
                "gpu_mem_free_mb": mem_free,
                "gpu_mem_total_mb": mem_total,
                "gpu_name": gpu_name,
                "dispatch_gpu_cap_mb": dispatch_gpu_mb,
            }
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
            return {}

    if _gpu_vendor == "amd":
        try:
            result = subprocess.run(
                ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout)
            card_key = next((k for k in data if k.startswith("card")), None)
            if not card_key:
                return {}
            card = data[card_key]
            gpu_util = 0
            for key in ("GPU use (%)", "GPU Activity", "GPU Use (%)", "GPU activity"):
                if key in card:
                    gpu_util = int(
                        str(card[key]).replace("%", "").replace(" ", "").strip()
                    )
                    break
            mem_total = int(
                card.get("VRAM Total Memory (B)", card.get("vram Total Memory (B)", 0))
            ) // (1024 * 1024)
            mem_used = int(
                card.get(
                    "VRAM Total Used Memory (B)",
                    card.get("vram Total Used Memory (B)", 0),
                )
            ) // (1024 * 1024)
            mem_free = max(0, mem_total - mem_used)
            gpu_name = str(
                card.get(
                    "Card Series",
                    card.get("Card series", card.get("card_series", "AMD GPU")),
                )
            )
            if not mem_total:
                return {}
            gpu_cap_mb = int(mem_total * (max_gpu_pct / 100.0))
            dispatch_gpu_mb = min(gpu_cap_mb, max(0, mem_free - 256))
            return {
                "gpu_util": gpu_util,
                "gpu_mem_used_mb": mem_used,
                "gpu_mem_free_mb": mem_free,
                "gpu_mem_total_mb": mem_total,
                "gpu_name": gpu_name,
                "dispatch_gpu_cap_mb": dispatch_gpu_mb,
            }
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            ValueError,
            IndexError,
            KeyError,
            json.JSONDecodeError,
        ):
            return {}

    return {}


def sample_net_bandwidth() -> NetIoRate:
    """Return bytes/sec (sent+recv) since the previous call.

    First call seeds the baseline and returns zeros — that's the the original implementation
    behaviour callers already expect.
    """
    try:
        import psutil  # local import: psutil is optional in some tests

        counters = psutil.net_io_counters()
        now = time.time()
        dt = now - _net_io_last["t"]
        if _net_io_last["t"] > 0 and dt > 0:
            _net_io_rate["sent_per_sec"] = max(
                0.0, (counters.bytes_sent - _net_io_last["sent"]) / dt
            )
            _net_io_rate["recv_per_sec"] = max(
                0.0, (counters.bytes_recv - _net_io_last["recv"]) / dt
            )
        _net_io_last["t"] = now
        _net_io_last["sent"] = counters.bytes_sent
        _net_io_last["recv"] = counters.bytes_recv
        return {
            "sent_per_sec": _net_io_rate["sent_per_sec"],
            "recv_per_sec": _net_io_rate["recv_per_sec"],
            "total_sent": counters.bytes_sent,
            "total_recv": counters.bytes_recv,
        }
    except Exception:
        return {
            "sent_per_sec": 0.0,
            "recv_per_sec": 0.0,
            "total_sent": 0,
            "total_recv": 0,
        }
