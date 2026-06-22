"""Per-worker reliability tracking for reliability-aware scheduling.

The master tallies how many tasks each worker *finished* vs *failed* (kept in
``STATE.worker_outcomes``) and turns that into a small comparable bucket the
selector folds into its ranking when a task opts into "prefer reliable
workers". Opt-in is per-task (with a node-wide default); see
:func:`nexus.scheduler.selection.select_task_for_worker`.

Design: in-memory and approximate. A Laplace prior keeps brand-new workers at a
neutral score instead of a misleading 0 or 1, and the score is bucketed to
tenths so a one-off failure doesn't swing scheduling more than real fitness
(RAM / GPU / network) differences.
"""

from __future__ import annotations

from nexus.core.state import STATE


def record_worker_outcome(worker_id: str | None, ok: bool) -> None:
    """Tally one finished (*ok=True*) or failed (*ok=False*) task for a worker."""
    if not worker_id:
        return
    tally = STATE.worker_outcomes.setdefault(str(worker_id), {"ok": 0, "fail": 0})
    tally["ok" if ok else "fail"] += 1


def reliability_ratio(worker_id: str | None) -> float:
    """Finished-to-total ratio for *worker_id*, Laplace-smoothed to ``[0, 1]``.

    A worker with no history scores 0.5 (neutral). ``(ok + 1) / (ok + fail + 2)``.
    """
    tally = STATE.worker_outcomes.get(str(worker_id or "")) or {}
    ok = int(tally.get("ok", 0))
    fail = int(tally.get("fail", 0))
    return (ok + 1) / (ok + fail + 2)


def reliability_bucket(worker_id: str | None) -> int:
    """The reliability score as a 0-10 bucket (tenths), for tuple comparison."""
    return int(reliability_ratio(worker_id) * 10)


__all__ = ["record_worker_outcome", "reliability_ratio", "reliability_bucket"]
