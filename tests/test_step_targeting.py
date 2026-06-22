"""DAG #1 — per-step targeting: merge resolution + flow into task metadata env."""

from __future__ import annotations

from nexus.tasks.metadata import build_task_metadata
from nexus.tasks.step_targeting import resolve_step_targeting


DEFAULTS = {
    "preferred_workers": ["9.9.9.9"],
    "target_groups": ["default_grp"],
    "blocked_members": [],
    "required_tags": ["base"],
    "require_gpu": False,
    "preferred_region": "us-east",
    "priority": 50,
    "retry_max": 2,
    "retry_backoff_base": None,
    "lease_seconds": None,
    "queue_timeout_sec": 0,
    "orphan_policy": "retry",
}


# ---- resolve_step_targeting ------------------------------------------------

def test_absent_keys_inherit_defaults():
    assert resolve_step_targeting({}, DEFAULTS) == DEFAULTS


def test_list_override_json_and_csv():
    eff = resolve_step_targeting(
        {"preferred_workers": ["1.1.1.1", "2.2.2.2"], "target_groups": "g1, g2"},
        DEFAULTS)
    assert eff["preferred_workers"] == ["1.1.1.1", "2.2.2.2"]
    assert eff["target_groups"] == ["g1", "g2"]      # CSV split + trimmed
    assert eff["required_tags"] == ["base"]           # untouched key inherits


def test_scalar_overrides():
    eff = resolve_step_targeting(
        {"priority": 90, "preferred_region": "eu-west", "retry_max": 5}, DEFAULTS)
    assert eff["priority"] == 90
    assert eff["preferred_region"] == "eu-west"
    assert eff["retry_max"] == 5


def test_require_gpu_true_and_explicit_false():
    assert resolve_step_targeting({"require_gpu": True}, DEFAULTS)["require_gpu"] is True
    gpu_default_true = {**DEFAULTS, "require_gpu": True}
    assert resolve_step_targeting({"require_gpu": False}, gpu_default_true)["require_gpu"] is False


def test_explicit_empty_list_clears_but_empty_string_inherits():
    # An explicit [] means "grid-wide for this step"; "" is treated as absent.
    assert resolve_step_targeting({"preferred_workers": []}, DEFAULTS)["preferred_workers"] == []
    assert resolve_step_targeting({"target_groups": ""}, DEFAULTS)["target_groups"] == ["default_grp"]


def test_defaults_not_mutated():
    snapshot = dict(DEFAULTS)
    resolve_step_targeting({"priority": 99, "preferred_workers": ["x"]}, DEFAULTS)
    assert DEFAULTS == snapshot


# ---- flows into the task metadata env --------------------------------------

def test_resolved_targeting_lands_in_task_env():
    step = {"priority": 90, "required_tags": ["gpu"], "require_gpu": True,
            "preferred_workers": ["10.0.0.5"], "target_groups": ["groupA"]}
    eff = resolve_step_targeting(step, DEFAULTS)
    env = build_task_metadata(
        {},
        preferred_workers=eff.get("preferred_workers") or None,
        target_groups=eff.get("target_groups") or None,
        blocked_members=eff.get("blocked_members") or None,
        priority=eff.get("priority", 50),
        retry_max=eff.get("retry_max", 2),
        required_tags=eff.get("required_tags"),
        require_gpu=eff.get("require_gpu", False),
        preferred_region=eff.get("preferred_region", ""),
    )
    assert env["NEXUS_META_PRIORITY"] == 90
    assert env["NEXUS_META_REQUIRE_GPU"] is True
    assert "gpu" in env["NEXUS_META_REQUIRED_TAGS"]
    assert env["NEXUS_META_PREFERRED_WORKERS"] == ["10.0.0.5"]
    assert env["NEXUS_META_TARGET_GROUPS"] == ["groupA"]
    assert env["NEXUS_META_PREFERRED_REGION"] == "us-east"  # inherited default
