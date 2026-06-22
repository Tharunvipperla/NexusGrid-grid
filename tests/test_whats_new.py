"""B2 — What's-new changelog parser + the bundled CHANGELOG.md."""

from __future__ import annotations

from nexus.runtime.whats_new import load_entries, parse_changelog


def test_parses_headers_bullets_and_dates():
    entries = parse_changelog(
        "# Changelog\n\n"
        "## [1.0.0] - 2026-06-20\n"
        "- first thing\n"
        "- second thing\n\n"
        "## [0.9.0] - 2026-05-30\n"
        "* star bullet\n"
    )
    assert len(entries) == 2
    assert entries[0] == {"version": "1.0.0", "date": "2026-06-20",
                          "highlights": ["first thing", "second thing"]}
    assert entries[1]["version"] == "0.9.0"
    assert entries[1]["highlights"] == ["star bullet"]   # '*' bullets too


def test_newest_first_order_preserved():
    entries = parse_changelog("## [2.0.0] - a\n- x\n## [1.0.0] - b\n- y\n")
    assert [e["version"] for e in entries] == ["2.0.0", "1.0.0"]


def test_header_without_date_is_ok():
    entries = parse_changelog("## [1.2.3]\n- only a version\n")
    assert entries == [{"version": "1.2.3", "date": "", "highlights": ["only a version"]}]


def test_title_and_prose_ignored():
    # The leading "# Changelog" title and any non-bullet prose must not leak in.
    entries = parse_changelog("# Changelog\nsome intro prose\n## [1.0.0] - x\nintro line\n- real bullet\n")
    assert entries[0]["highlights"] == ["real bullet"]


def test_empty_or_garbage_yields_no_entries():
    assert parse_changelog("") == []
    assert parse_changelog("no headers here\njust text") == []


def test_bundled_changelog_loads_and_is_wellformed():
    entries = load_entries()
    assert entries, "bundled nexus/CHANGELOG.md should parse to >=1 entry"
    for e in entries:
        assert e["version"] and e["highlights"], f"entry missing fields: {e}"
