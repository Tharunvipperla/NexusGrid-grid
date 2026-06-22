"""Per-step targeting/override resolution for DAG workflows.

A workflow step may carry its own scheduling targets (nodes, groups, tags, …)
and overrides, falling back to the dispatch-level defaults when it doesn't.
This lets one DAG send step A to group X, step B to specific nodes, and step C
wherever — instead of one set of targets for the whole dispatch.

Pure function (no DB/HTTP) so it's unit-testable; ``local_add_workflow`` builds
the ``defaults`` dict from its form fields and merges each step over it.
"""

from __future__ import annotations

# Keys whose values are lists; a step value may be a JSON list or a CSV string.
_LIST_KEYS = ("preferred_workers", "target_groups", "blocked_members", "required_tags")
# Keys whose values are scalars; a present, non-empty step value overrides.
_SCALAR_KEYS = (
    "require_gpu", "preferred_region", "priority", "retry_max",
    "retry_backoff_base", "lease_seconds", "queue_timeout_sec", "orphan_policy",
)


def _as_list(value) -> list[str] | None:
    """Coerce a JSON list or CSV string to a clean list; ``None`` if absent/empty."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    s = str(value).strip()
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def resolve_step_targeting(step: dict, defaults: dict) -> dict:
    """Merge a step's per-step targeting over the dispatch *defaults*.

    Only keys the step actually specifies override the default; everything else
    inherits. ``require_gpu`` is coerced to bool. Returns a new dict.
    """
    out = dict(defaults)
    for k in _LIST_KEYS:
        if k in step:
            lst = _as_list(step.get(k))
            if lst is not None:
                out[k] = lst
    for k in _SCALAR_KEYS:
        if k in step and step.get(k) not in (None, ""):
            out[k] = bool(step[k]) if k == "require_gpu" else step[k]
    return out


__all__ = ["resolve_step_targeting"]
