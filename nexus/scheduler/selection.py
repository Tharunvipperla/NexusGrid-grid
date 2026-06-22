"""Pick the best queued task for a worker asking for work.

Extracted from node_modified.py (lines 2647-2724).

This is the scheduler's only *decision* function: given a worker id + the
current queue + the list of all known workers + per-master concurrency
state, return the single best task for that worker (or ``None`` if nothing
fits).

Complexity is O(tasks * workers) per call. Acceptable at current scale
(<1000 tasks/workers). The manifest cache in :mod:`.manifest` keeps the
inner loop cheap.
"""

from __future__ import annotations

from nexus.core import LOCAL_SETTINGS, STATE, resolve_uuid_to_ip
from nexus.runtime import task_required_caps
from nexus.scheduler.fitness import worker_fit_score, worker_supports_task
from nexus.scheduler.manifest import read_task_manifest
from nexus.scheduler.reliability import reliability_bucket
from nexus.storage import TaskRecord
from nexus.tasks.metadata import (
    extract_task_metadata,
    parse_task_env,
    task_created_at,
)
from nexus.utils import now_epoch


def _allowed_targets(
    preferred_set: set[str],
    target_groups: list[str],
    group_pool: dict[str, set[str]] | None,
    metadata: dict,
) -> set[str] | None:
    """The eligible-worker set for a task, or ``None`` if unscoped.

    Union of manual node targets (``preferred_set``) and the chosen groups'
    ``task:run`` members, minus any per-dispatch blocked members (their node
    UUID and resolved ip). ``None`` = grid-wide; an empty set = nothing fits.
    """
    if not preferred_set and not target_groups:
        return None
    allowed = set(preferred_set)
    for g in target_groups:
        allowed |= (group_pool or {}).get(g, set())
    for u in metadata.get("blocked_members") or []:
        allowed.discard(u)
        ip = resolve_uuid_to_ip(u)
        if ip:
            allowed.discard(ip)
    return allowed


def select_task_for_worker(
    worker_id: str,
    queued_tasks: list[TaskRecord],
    all_workers: dict,
    processing_by_master: dict[str, int],
    group_pool: dict[str, set[str]] | None = None,
    workflow_busy: dict[str, set[str]] | None = None,
) -> TaskRecord | None:
    """Return the best task in *queued_tasks* for *worker_id*, or ``None``.

    ``group_pool`` maps ``group_id -> set(eligible worker_ids)`` for
    any group referenced by a queued task's ``target_groups``. A task scoped
    to one or more groups is only offered to workers in the union of those
    groups' eligible sets. Tasks with no ``target_groups`` are grid-wide as
    before.
    """
    if not queued_tasks:
        return None
    if worker_id not in all_workers or now_epoch() < float(
        STATE.worker_cooldown_until.get(worker_id, 0) or 0
    ):
        return queued_tasks[0] if worker_id not in all_workers else None

    available_workers = {
        cid: info
        for cid, info in all_workers.items()
        if now_epoch() >= float(STATE.worker_cooldown_until.get(cid, 0) or 0)
        and worker_fit_score(info, 128, 10) is not None
    }
    if not available_workers:
        return None

    best_candidate: tuple | None = None
    for task in queued_tasks:
        metadata = extract_task_metadata(task)
        if processing_by_master.get(
            metadata.get("requested_by") or "unknown", 0
        ) >= int(LOCAL_SETTINGS["master_quota_per_origin"]):
            continue

        preferred_set = set(metadata["preferred_workers"])
        target_groups = metadata.get("target_groups") or []
        # Union of manual node targets and the chosen groups'
        # eligible workers, minus any per-dispatch blocked members. ``None``
        # means unscoped (grid-wide); an empty set means nothing qualifies.
        allowed = _allowed_targets(preferred_set, target_groups, group_pool, metadata)
        if allowed is not None and worker_id not in allowed:
            continue

        # DAG #2 anti-affinity: when this workflow asks for "one step per node",
        # any worker already running a sibling step (same parent_id) is excluded
        # so the remaining steps spread across other nodes.
        busy: set[str] = set()
        if metadata.get("one_step_per_node") and getattr(task, "parent_id", ""):
            busy = (workflow_busy or {}).get(task.parent_id) or set()
            if worker_id in busy:
                continue

        manifest = read_task_manifest(cache_key=task.id)
        req_ram = int(manifest.get("ram_limit_mb", 512) or 512)
        req_cpu = int(manifest.get("cpu_limit_pct", 100) or 100)
        req = task_required_caps(task)
        task_needs_gpu = req["require_gpu"]

        env = parse_task_env(task)
        fw_raw = env.get("NEXUS_META_FAILED_WORKERS", [])
        task_failed_workers = set(fw_raw) if isinstance(fw_raw, list) else set()
        # Reliability-aware ranking: per-task override (None = inherit) over the
        # node default. When on, each worker's finished-to-fail bucket is the
        # top dimension of its score so a more reliable node wins ties on fit.
        pref = metadata.get("prefer_reliable_workers")
        prefer_reliable = (
            bool(LOCAL_SETTINGS.get("prefer_reliable_workers", False))
            if pref is None else bool(pref)
        )
        scored_workers: list[tuple[str, tuple]] = []
        for cid, info in available_workers.items():
            if allowed is not None and cid not in allowed:
                continue
            if cid in busy:  # anti-affinity: already running a sibling step
                continue
            if cid in task_failed_workers:
                continue
            if not worker_supports_task(info, task):
                continue
            score = worker_fit_score(info, req_ram, req_cpu, require_gpu=task_needs_gpu)
            if score is not None:
                if prefer_reliable:
                    score = (reliability_bucket(cid),) + score
                scored_workers.append((cid, score))

        if not scored_workers:
            continue
        scored_workers.sort(key=lambda item: item[1], reverse=True)
        if scored_workers[0][0] != worker_id:
            continue

        top_score = scored_workers[0][1]
        candidate_key = (
            metadata["priority"],
            1 if worker_id in preferred_set else 0,
            top_score,
            now_epoch() - (task_created_at(task) or now_epoch()),
            req_ram,
        )
        if not best_candidate or candidate_key > best_candidate[0]:
            best_candidate = (candidate_key, task)

    return best_candidate[1] if best_candidate else None


def select_top_n_workers(
    task: TaskRecord,
    n: int,
    all_workers: dict,
    *,
    exclude: set[str] | None = None,
    group_pool: dict[str, set[str]] | None = None,
) -> list[str]:
    """Return the top *n* worker_ids ranked by ``worker_fit_score`` for *task*.

    Used by replication: the first entry is the primary, the
    rest are standbys. Workers in *exclude* (already-failed, already-chosen)
    are skipped. Returns fewer than *n* if not enough workers qualify; the
    caller logs that as a warning and proceeds with what's available.
    """
    if n <= 0 or not all_workers:
        return []
    exclude = exclude or set()
    manifest = read_task_manifest(
        task_payload=getattr(task, "payload", None), cache_key=task.id
    )
    req_ram = int(manifest.get("ram_limit_mb", 512) or 512)
    req_cpu = int(manifest.get("cpu_limit_pct", 100) or 100)
    req = task_required_caps(task)

    # Restrict to the union of group-eligible workers (replication
    # has no manual node targets, so preferred is empty here), minus blocked.
    meta = extract_task_metadata(task)
    target_groups = meta.get("target_groups") or []
    allowed = _allowed_targets(set(), target_groups, group_pool, meta)

    scored: list[tuple[str, tuple]] = []
    for cid, info in all_workers.items():
        if cid in exclude:
            continue
        if allowed is not None and cid not in allowed:
            continue
        if now_epoch() < float(STATE.worker_cooldown_until.get(cid, 0) or 0):
            continue
        if not worker_supports_task(info, task):
            continue
        score = worker_fit_score(info, req_ram, req_cpu, require_gpu=req["require_gpu"])
        if score is None:
            continue
        scored.append((cid, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return [cid for cid, _ in scored[:n]]


__all__ = ["select_task_for_worker", "select_top_n_workers"]
