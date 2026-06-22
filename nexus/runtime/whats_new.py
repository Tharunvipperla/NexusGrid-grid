"""B2 — in-app "What's new" changelog source.

Parses the bundled ``nexus/CHANGELOG.md`` (Keep-a-Changelog-ish: one
``## [version] - date`` header per release followed by ``-`` bullets) into a
structured list the UI renders and the notification bell flags when the newest
version hasn't been seen yet. Pure ``parse_changelog`` so it's unit-testable;
``load_entries`` just reads the bundled file.
"""

from __future__ import annotations

import re

from nexus.core.paths import get_resource_dir

# "## [1.0.0] - 2026-06-20"  (brackets and the date are both optional)
_HEADER = re.compile(r"^##\s+\[?([^\]\s]+)\]?\s*(?:[-–—]\s*(.+))?$")


def parse_changelog(text: str) -> list[dict]:
    """Return ``[{version, date, highlights:[...]}, ...]`` in file order
    (newest first by convention). Bullets between a header and the next header
    become that release's highlights; anything else is ignored."""
    entries: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADER.match(line)
        if m:
            cur = {"version": m.group(1).strip(), "date": (m.group(2) or "").strip(), "highlights": []}
            entries.append(cur)
            continue
        if cur is not None and (line.startswith("- ") or line.startswith("* ")):
            cur["highlights"].append(line[2:].strip())
    return entries


def load_entries() -> list[dict]:
    """Parse the bundled changelog; empty list if it's missing/unreadable.

    Resolved via :func:`get_resource_dir` so it works both from source and from
    a PyInstaller bundle (where it's extracted to ``<_MEIPASS>/nexus``)."""
    try:
        path = get_resource_dir() / "nexus" / "CHANGELOG.md"
        return parse_changelog(path.read_text(encoding="utf-8"))
    except Exception:
        return []


__all__ = ["parse_changelog", "load_entries"]
