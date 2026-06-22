"""Self-benchmark tests (Wave 4 Step 7 / item 6.7)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.scheduler import benchmark
from nexus.scheduler.fitness import worker_fit_score


# ---------------------------------------------------------------------------
# CPU + IO + combined scoring
# ---------------------------------------------------------------------------

def test_cpu_score_returns_positive_float():
    score = benchmark.cpu_score()
    assert isinstance(score, float)
    assert score > 0.0


def test_io_score_returns_positive_float():
    # Use a smaller payload to keep the test fast.
    score = benchmark.io_score(payload_bytes=1 * 1024 * 1024)
    assert isinstance(score, float)
    assert score > 0.0


def test_combined_score_weights():
    # 50 MFLOPS reference, 200 MB/s reference -> normalised to 100 each.
    # 0.7*100 + 0.3*100 = 100.0
    assert benchmark.combined_score(50.0, 200.0) == 100.0


def test_combined_score_zero_floors_at_zero():
    assert benchmark.combined_score(0.0, 0.0) == 0.0


def test_run_benchmark_dict_shape():
    result = benchmark.run_benchmark()
    assert set(result.keys()) == {"score", "cpu_mflops", "io_mb_s", "ran_at"}
    assert result["score"] >= 0.0
    assert result["cpu_mflops"] >= 0.0
    assert result["io_mb_s"] >= 0.0
    assert result["ran_at"] > 0


def test_run_benchmark_swallows_cpu_failure():
    with patch.object(benchmark, "cpu_score", side_effect=RuntimeError("boom")):
        result = benchmark.run_benchmark()
    assert result["cpu_mflops"] == 0.0
    assert result["io_mb_s"] >= 0.0


# ---------------------------------------------------------------------------
# Fitness consumes bench
# ---------------------------------------------------------------------------

def _worker(bench: float = 0.0, free_ram: int = 4096) -> dict:
    import time

    return {
        "last_seen": time.time(),
        "stats": {
            "free_ram": free_ram,
            "dispatch_ram_cap_mb": free_ram,
            "cpu": 30.0,
            "active_task_count": 0,
            "capabilities": {"gpu": False},
            "connection_type": "lan",
            "bench": bench,
        },
    }


def test_higher_bench_wins_when_resources_equal():
    fast = worker_fit_score(_worker(bench=120.0), req_ram=512, req_cpu=0)
    slow = worker_fit_score(_worker(bench=10.0), req_ram=512, req_cpu=0)
    assert fast is not None
    assert slow is not None
    assert fast > slow  # tuple comparison: fast.bench_tier > slow.bench_tier


def test_zero_bench_does_not_break_score():
    score = worker_fit_score(_worker(bench=0.0), req_ram=512, req_cpu=0)
    assert score is not None


def test_bench_tier_buckets_5_points():
    """Two workers within the same 5-point bucket tie on the bench dimension."""
    a = worker_fit_score(_worker(bench=12.0), req_ram=512, req_cpu=0)
    b = worker_fit_score(_worker(bench=14.5), req_ram=512, req_cpu=0)
    assert a is not None and b is not None
    # bench_tier is the third element of the tuple
    assert a[2] == b[2]
