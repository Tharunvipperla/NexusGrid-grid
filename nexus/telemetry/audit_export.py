"""E1 fast-follow — audit log export (CSV/JSON).

Pure helpers over a list of audit-event dicts (as returned by the ``/local/audit``
endpoint): filter by severity / since-timestamp, and render CSV. Kept free of DB
and HTTP so they're trivially testable; the endpoint wires them to the rows.
"""

from __future__ import annotations

import csv
import io

_COLS = ["ts", "action", "actor", "severity", "task_id", "details"]


def filter_events(
    events: list[dict], severity: str = "", since: float = 0.0
) -> list[dict]:
    """Subset *events* by exact severity and/or a minimum timestamp."""
    out = events
    if severity:
        out = [e for e in out if str(e.get("severity") or "info") == severity]
    if since:
        out = [e for e in out if float(e.get("ts") or 0) >= float(since)]
    return out


def events_to_csv(events: list[dict]) -> str:
    """Render events as CSV (header + rows). The ``csv`` module handles quoting
    of commas, quotes, and newlines in free-text ``details``."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS, extrasaction="ignore")
    w.writeheader()
    for e in events:
        w.writerow({c: e.get(c, "") for c in _COLS})
    return buf.getvalue()


__all__ = ["filter_events", "events_to_csv"]
