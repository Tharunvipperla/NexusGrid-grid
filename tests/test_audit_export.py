"""E1 fast-follow — audit export: severity/since filters + CSV rendering/escaping."""

from __future__ import annotations

import csv
import io

from nexus.telemetry import audit_export as AE


EVENTS = [
    {"ts": 100.0, "action": "secret.set", "actor": "n1", "severity": "info", "task_id": "", "details": "name=KEY"},
    {"ts": 200.0, "action": "storage.auto_rescue_failed", "actor": "n1", "severity": "warning", "task_id": "d1", "details": "files_may_be_lost"},
    {"ts": 300.0, "action": "peer.blocked", "actor": "n1", "severity": "info", "task_id": "p2", "details": ""},
]


def test_filter_by_severity():
    out = AE.filter_events(EVENTS, severity="warning")
    assert [e["action"] for e in out] == ["storage.auto_rescue_failed"]


def test_filter_by_since():
    out = AE.filter_events(EVENTS, since=200.0)
    assert {e["ts"] for e in out} == {200.0, 300.0}


def test_filter_noop_returns_all():
    assert AE.filter_events(EVENTS) == EVENTS


def test_csv_has_header_and_rows():
    text = AE.events_to_csv(EVENTS)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3
    assert rows[0]["action"] == "secret.set"
    assert rows[1]["severity"] == "warning"


def test_csv_escapes_commas_and_quotes():
    tricky = [{"ts": 1.0, "action": "x", "actor": "a", "severity": "info",
               "task_id": "t", "details": 'has, comma and "quotes"\nand newline'}]
    text = AE.events_to_csv(tricky)
    # Round-trips cleanly despite commas/quotes/newlines in the free-text field.
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["details"] == 'has, comma and "quotes"\nand newline'


def test_csv_missing_fields_blank():
    text = AE.events_to_csv([{"action": "only_action"}])
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["action"] == "only_action"
    assert rows[0]["details"] == ""
