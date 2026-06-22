"""B4 — incremental live-tail of the Docker entrypoint's growing output file.

`unstreamed_tail` is the pure core of the executor's Docker live-streaming: each
poll it re-reads the whole capture file and emits only the bytes not yet shown,
so a Docker task streams like a native one without ever duplicating output.
"""

from __future__ import annotations

from nexus.telemetry.logs import unstreamed_tail


def test_first_read_emits_everything():
    new, hwm = unstreamed_tail(b"hello\n", 0)
    assert new == b"hello\n" and hwm == 6


def test_only_new_bytes_after_offset():
    new, hwm = unstreamed_tail(b"hello\nworld\n", 6)
    assert new == b"world\n" and hwm == 12


def test_no_growth_emits_nothing():
    new, hwm = unstreamed_tail(b"hello\n", 6)
    assert new == b"" and hwm == 6


def test_truncation_resyncs_from_zero():
    # File shrank (rotated/truncated) below the high-water mark — resync, never
    # return a negative/garbage slice.
    new, hwm = unstreamed_tail(b"abc", 99)
    assert new == b"abc" and hwm == 3


def test_streaming_loop_matches_full_once():
    """Simulate the executor loop: a file grown in chunks, polled repeatedly,
    must reconstruct the full output exactly once (no drops, no duplicates)."""
    chunks = [b"line1\n", b"line2\n", b"", b"line3\nline4\n", b"line5\n"]
    full = b""
    emitted = b""
    streamed = 0
    for c in chunks:
        full += c
        new, streamed = unstreamed_tail(full, streamed)
        emitted += new
    assert emitted == full == b"line1\nline2\nline3\nline4\nline5\n"
