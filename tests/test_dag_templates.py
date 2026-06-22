"""DAG #4 — saved DAG templates: normalization (caps, junk-drop, field sizes)."""

from __future__ import annotations

from nexus.core.config import _normalize_dag_templates, normalize_local_settings


def test_keeps_valid_template():
    out = _normalize_dag_templates({
        "pipeline": {"steps": [{"id": "a"}, {"id": "b", "depends_on": ["a"]}],
                     "description": "two-step", "created_at": "t0"},
    })
    assert set(out) == {"pipeline"}
    assert out["pipeline"]["steps"] == [{"id": "a"}, {"id": "b", "depends_on": ["a"]}]
    assert out["pipeline"]["description"] == "two-step"
    assert out["pipeline"]["created_at"] == "t0"


def test_non_dict_returns_empty():
    assert _normalize_dag_templates(None) == {}
    assert _normalize_dag_templates([1, 2]) == {}


def test_drops_junk_entries():
    out = _normalize_dag_templates({
        "": {"steps": [{"id": "a"}]},          # empty name
        "  ": {"steps": [{"id": "a"}]},        # whitespace name
        "nosteps": {"description": "x"},        # missing steps
        "badsteps": {"steps": "notalist"},      # steps not a list
        "notdict": "x",                         # value not a dict
        "good": {"steps": [{"id": "ok"}]},
    })
    assert set(out) == {"good"}


def test_non_dict_steps_filtered():
    out = _normalize_dag_templates({"t": {"steps": [{"id": "a"}, "junk", 5, {"id": "b"}]}})
    assert out["t"]["steps"] == [{"id": "a"}, {"id": "b"}]


def test_caps_counts_and_sizes():
    many = {f"t{i}": {"steps": [{"id": "a"}]} for i in range(60)}
    assert len(_normalize_dag_templates(many)) == 50               # template cap

    big = _normalize_dag_templates({"t": {
        "steps": [{"id": str(i)} for i in range(150)],
        "description": "d" * 500,
    }})
    assert len(big["t"]["steps"]) == 100                            # step cap
    assert len(big["t"]["description"]) == 200                      # description cap

    longname = "n" * 200
    out = _normalize_dag_templates({longname: {"steps": [{"id": "a"}]}})
    assert len(next(iter(out))) == 80                               # name cap


def test_round_trips_through_normalize_local_settings():
    merged = normalize_local_settings({"dag_templates": {
        "p": {"steps": [{"id": "a"}], "description": "x"},
    }})
    assert merged["dag_templates"]["p"]["steps"] == [{"id": "a"}]
    # absent key normalizes to an empty dict
    assert normalize_local_settings({})["dag_templates"] == {}
