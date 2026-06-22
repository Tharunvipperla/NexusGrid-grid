"""#3 — saved dispatch-settings profiles: normalization (caps, junk-drop)."""

from __future__ import annotations

from nexus.core.config import _normalize_dispatch_templates, normalize_local_settings


def test_keeps_valid_profile():
    out = _normalize_dispatch_templates({
        "gpu-heavy": {"settings": {"gpu": True, "ram": 8192, "priority": "80"},
                      "description": "for ML", "created_at": "t0"},
    })
    assert set(out) == {"gpu-heavy"}
    assert out["gpu-heavy"]["settings"] == {"gpu": True, "ram": 8192, "priority": "80"}
    assert out["gpu-heavy"]["description"] == "for ML"
    assert out["gpu-heavy"]["created_at"] == "t0"


def test_non_dict_returns_empty():
    assert _normalize_dispatch_templates(None) == {}
    assert _normalize_dispatch_templates([1, 2]) == {}


def test_drops_junk_entries():
    out = _normalize_dispatch_templates({
        "": {"settings": {"ram": 1}},            # empty name
        "  ": {"settings": {"ram": 1}},          # whitespace name
        "nosettings": {"description": "x"},       # missing settings
        "badsettings": {"settings": "nope"},      # settings not a dict
        "notdict": "x",                           # value not a dict
        "good": {"settings": {"ram": 1024}},
    })
    assert set(out) == {"good"}


def test_caps_counts_and_sizes():
    many = {f"t{i}": {"settings": {"ram": 1}} for i in range(60)}
    assert len(_normalize_dispatch_templates(many)) == 50           # profile cap

    out = _normalize_dispatch_templates({"t": {
        "settings": {"ram": 1}, "description": "d" * 500,
    }})
    assert len(out["t"]["description"]) == 200                       # description cap

    longname = "n" * 200
    out = _normalize_dispatch_templates({longname: {"settings": {"ram": 1}}})
    assert len(next(iter(out))) == 80                                # name cap


def test_round_trips_through_normalize_local_settings():
    merged = normalize_local_settings({"dispatch_templates": {
        "p": {"settings": {"gpu": True}, "description": "x"},
    }})
    assert merged["dispatch_templates"]["p"]["settings"] == {"gpu": True}
    # absent key normalizes to an empty dict
    assert normalize_local_settings({})["dispatch_templates"] == {}
