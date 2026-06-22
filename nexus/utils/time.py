"""Timestamp and duration helpers.

Extracted from node_modified.py (lines 669, 928).
"""

from __future__ import annotations

import time
from datetime import datetime


def timestamp() -> str:
    """Return the current wall-clock time as ``HH:MM:SS``.

    Used by audit / log formatters that want a compact readable stamp.
    """
    return datetime.now().strftime("%H:%M:%S")


def iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Use this for any datetime persisted to the DB and later parsed for
    arithmetic (foreign-storage eviction lifecycle, ttl_at, etc.).
    ``timestamp()`` is for short log labels and cannot be parsed back
    into a date.
    """
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()


def now_epoch() -> float:
    """Return the current wall-clock time as a POSIX epoch float."""
    return time.time()


def format_elapsed(seconds: float) -> str:
    """Format a duration in seconds as a short human string (e.g. ``3m 12s``)."""
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
