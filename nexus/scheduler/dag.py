"""Resolve workflow dependencies and release ready tasks into the queue.

Extracted from Phase-1/node_modified.py (lines 5727-5766).

A task in the ``waiting`` state declares its prerequisites in
``TaskRecord.depends_on`` (comma-separated task ids). This loop polls
those tasks and, when every prerequisite has reached ``completed``,
transitions the task to ``queued`` and enqueues its id.

Tasks with no ``depends_on`` move straight to ``queued``.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE, events
from nexus.storage import TaskRecord, get_session
from nexus.tasks.lifecycle import set_task_status
from nexus.tasks.metadata import extract_task_metadata
from nexus.tasks.queue import enqueue_task
from nexus.telemetry.metrics import incr_metric


def _gate_on(task: TaskRecord) -> bool:
    """Effective step-gate for *task*: the per-dispatch override if set, else the
    node default. When on, a step is held at ``awaiting_approval`` once its deps
    complete so the user can verify the level before the next one is assigned."""
    g = extract_task_metadata(task).get("step_gate")
    return bool(LOCAL_SETTINGS.get("step_gate", False)) if g is None else bool(g)

PRIMARY_OFFLINE_AFTER_SEC = 12
IMAGE_REFRESH_INTERVAL_SEC = 60.0
_LAST_IMAGE_REFRESH_AT: dict[str, float] = {}


async def release_ready_tasks() -> tuple[int, int]:
    """One DAG-resolution pass: queue every ``waiting`` task whose deps are met
    (or hold it at ``awaiting_approval`` when its step gate is on). Returns
    ``(queued, gated)`` counts."""
    queued_now = 0
    gated_now = 0
    async with get_session() as db:
        waiting_tasks = (
            (
                await db.execute(
                    select(TaskRecord).filter(TaskRecord.status == "waiting")
                )
            )
            .scalars()
            .all()
        )
        for task in waiting_tasks:
            deps = [d.strip() for d in (task.depends_on or "").split(",") if d.strip()]
            if not deps:
                if set_task_status(task, "queued", "No dependencies remaining."):
                    await enqueue_task(task.id)
                    incr_metric("tasks_dispatched")
                    queued_now += 1
                continue
            dep_records = (
                (
                    await db.execute(
                        select(TaskRecord).filter(TaskRecord.id.in_(deps))
                    )
                )
                .scalars()
                .all()
            )
            if len(dep_records) == len(deps) and all(
                d.status == "completed" for d in dep_records
            ):
                # Step gate: hold the step for manual approval instead of
                # queuing it, so the user can verify the finished level before
                # the next one is assigned. The first level (no deps) is never
                # gated — it runs so there's something to verify.
                if _gate_on(task):
                    if set_task_status(
                        task,
                        "awaiting_approval",
                        f"Dependencies ({task.depends_on}) met — awaiting approval.",
                    ):
                        gated_now += 1
                    continue
                if set_task_status(
                    task, "queued", f"Dependencies ({task.depends_on}) met."
                ):
                    await enqueue_task(task.id)
                    incr_metric("tasks_dispatched")
                    queued_now += 1
        await db.commit()
    return queued_now, gated_now


async def dag_scheduler_loop(poll_seconds: float = 2.0) -> None:
    """Forever: resolve DAG deps and release ready tasks."""
    while True:
        queued_now, gated_now = await release_ready_tasks()
        if queued_now:
            events.publish("scheduler.dag_released", {"count": queued_now})
        if gated_now:
            events.publish("scheduler.dag_gated", {"count": gated_now})

        # Failover + traffic switch.
        try:
            await service_health_pass()
        except Exception:
            # Health pass must not break the DAG loop.
            pass

        # Standby image refresh on a slower cadence.
        try:
            await service_image_refresh_pass()
        except Exception:
            pass

        # Foreign-storage lifecycle pass (eviction → grace → purge).
        try:
            await foreign_storage_lifecycle_pass()
        except Exception:
            pass

        # Drop idle session keys past their TTL.
        try:
            await foreign_storage_key_gc_pass()
        except Exception:
            pass

        await asyncio.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Service health 
# ---------------------------------------------------------------------------

def _worker_online(worker_id: str) -> bool:
    info = STATE.active_workers.get(worker_id)
    if not info:
        return False
    last_seen = float(info.get("last_seen", 0) or 0)
    return (time.time() - last_seen) < PRIMARY_OFFLINE_AFTER_SEC


async def service_health_pass() -> None:
    """Detect dead service primaries and promote a standby for each (Step 9e).

    Runs on the master. For every service we know about, if the recorded
    primary worker hasn't been seen in :data:`PRIMARY_OFFLINE_AFTER_SEC`
    seconds, pick the first online standby, send it
    ``service_promote_with_snapshot``, repoint the local tunnel listener,
    and audit the transition. If every standby is also gone, mark the
    service ``failed``.
    """
    from nexus.networking.tunnel import _send_to_peer, reroute_tunnel
    from nexus.telemetry.audit import record_audit_event

    candidates: list[tuple[str, dict]] = []
    async with STATE.service_lock:
        for tid, rec in list(STATE.service_records.items()):
            if rec.get("status") in {"failed", "stopped"}:
                continue
            primary = rec.get("worker_id") or ""
            if primary and _worker_online(primary):
                continue
            candidates.append((tid, dict(rec)))

    for task_id, rec in candidates:
        old_primary = rec.get("worker_id") or ""
        await record_audit_event(
            "service_primary_lost",
            actor=task_id,
            task_id=task_id,
            severity="warning",
            details=f"primary={old_primary}",
        )

        standbys = [s for s in (rec.get("standbys") or []) if _worker_online(s)]
        if not standbys:
            async with STATE.service_lock:
                live = STATE.service_records.get(task_id)
                if live is not None:
                    live["status"] = "failed"
                    live["worker_id"] = ""
            await record_audit_event(
                "service_no_replicas_left",
                actor=task_id,
                task_id=task_id,
                severity="error",
            )
            continue

        new_primary = standbys[0]
        strategy = str(rec.get("replica_strategy", "snapshot") or "snapshot").lower()
        if strategy == "snapshot":
            frame = {
                "type": "service_promote_with_snapshot",
                "task_id": task_id,
            }
            try:
                await _send_to_peer(new_primary, frame)
            except Exception:
                pass

        async with STATE.service_lock:
            live = STATE.service_records.get(task_id)
            if live is not None:
                live["worker_id"] = new_primary
                live["standbys"] = [s for s in (live.get("standbys") or []) if s != new_primary]
                live["promoted_at"] = time.time()
                live["status"] = "running"

        try:
            await reroute_tunnel(task_id, new_primary)
        except Exception:
            pass

        # Notify dependents and push grants to the new primary.
        await _notify_dependents_after_promotion(task_id, new_primary, rec)

        await record_audit_event(
            "service_primary_promoted",
            actor=task_id,
            task_id=task_id,
            severity="info",
            details=f"old={old_primary} new={new_primary} strategy={strategy}",
        )


async def service_image_refresh_pass() -> None:
    """Periodically push ``service_image_refresh`` to every standby.

    Standbys re-pull the manifest image so promotion uses whatever tag the
    registry currently serves. Runs once per ``IMAGE_REFRESH_INTERVAL_SEC``
    per service (the DAG loop ticks every 2 s; we throttle here).
    """
    from nexus.networking.tunnel import _send_to_peer

    now = time.time()
    targets: list[tuple[str, str, list[str]]] = []
    async with STATE.service_lock:
        for tid, rec in list(STATE.service_records.items()):
            if rec.get("status") in {"failed", "stopped"}:
                continue
            standbys = list(rec.get("standbys") or [])
            if not standbys:
                continue
            image = str(rec.get("image", "") or "").strip()
            if not image:
                continue
            last = float(_LAST_IMAGE_REFRESH_AT.get(tid, 0) or 0)
            if now - last < IMAGE_REFRESH_INTERVAL_SEC:
                continue
            _LAST_IMAGE_REFRESH_AT[tid] = now
            targets.append((tid, image, standbys))

    for task_id, image, standbys in targets:
        frame = {
            "type": "service_image_refresh",
            "task_id": task_id,
            "image": image,
        }
        for worker_id in standbys:
            try:
                await _send_to_peer(worker_id, frame)
            except Exception:
                pass


async def foreign_storage_lifecycle_pass() -> None:
    """Drive deposit lifecycle on each DAG tick.

    Three transitions:
      1. ``eviction_requested`` + 1 day window elapsed
         → move bytes into ``ForeignStorageDBGrace`` and flip to
         ``in_db_grace``.
      2. ``in_db_grace`` + 2 days elapsed → purge.
      3. ``stored`` past natural ``ttl_at`` → start eviction
         (reason='ttl_expired').
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_

    from nexus.networking.storage_pump import build_storage_eviction_request
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import (
        ForeignStorageDBGrace,
        ForeignStorageDeposit,
        get_session,
    )
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import iso_now

    now = datetime.now(timezone.utc)

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "host",
                        or_(
                            ForeignStorageDeposit.status == "eviction_requested",
                            ForeignStorageDeposit.status == "in_db_grace",
                            ForeignStorageDeposit.status == "stored",
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                # Host-configured total countdown (Evict click → purge).
                # Stamped on the row at Evict click time so changing the
                # setting doesn't reshuffle an active eviction. Falls back
                # to the legacy 1+2=3 day default for rows that pre-date
                # schema 9.
                total_days = max(1, int(row.eviction_total_days or 3))
                response_window = 1 if total_days >= 2 else 0
                grace_days = max(0, total_days - response_window)
                if (
                    row.status == "eviction_requested"
                    and row.eviction_requested_at
                ):
                    cutoff = _parse_iso(row.eviction_requested_at) + timedelta(days=response_window)
                    if now >= cutoff:
                        await _move_to_db_grace(db, row, now)
                elif row.status == "in_db_grace" and row.db_grace_at:
                    cutoff = _parse_iso(row.db_grace_at) + timedelta(days=grace_days)
                    if now >= cutoff:
                        await _purge_deposit(db, row, now)
                elif row.status == "stored" and row.ttl_at:
                    cutoff = _parse_iso(row.ttl_at)
                    if now >= cutoff:
                        row.status = "eviction_requested"
                        row.eviction_requested_at = iso_now()
                        ttl_total_days = max(
                            1, int(LOCAL_SETTINGS.get("evict_total_days", 3) or 3)
                        )
                        row.eviction_total_days = ttl_total_days
                        await _send_to_peer(
                            row.depositor_uuid,
                            build_storage_eviction_request(
                                row.deposit_id,
                                response_window_days=1,
                                urgency="ttl",
                                total_days=ttl_total_days,
                            ),
                        )
                        await record_audit_event(
                            "storage.eviction_requested",
                            actor=row.deposit_id,
                            task_id=row.deposit_id,
                            details=f"reason=ttl_expired total_days={ttl_total_days}",
                        )
            except Exception:
                # Lifecycle errors must not break the DAG loop.
                pass
        await db.commit()

    # Batch C: tripwire — for every still-stored deposit on this host,
    # re-stat chunks and fire an audit + bus event if anything changed
    # since the baseline was armed. The baseline is recorded when the
    # deposit transitions to "stored" and cleared on purge / DB-grace
    # migration, so only un-tampered, in-flight deposits are checked.
    try:
        await _foreign_storage_tripwire_pass()
    except Exception:
        pass

    # P1: retry deposits that were queued because the target peer was
    # offline at deposit time. Drops rows past their 24 h TTL or whose
    # derived key was lost across a process restart.
    try:
        await _foreign_storage_queue_retry_pass()
    except Exception:
        pass

    # P2: enforce the user-configured timeout on auto-mode fan-out
    # offers. When elapsed, broadcast cancels to remaining candidates,
    # flip the row to withdrawn, and ask the UI to prompt a redo. We
    # deliberately do not auto-retry — per user, that would spam the
    # network.
    try:
        await _foreign_storage_auto_offer_timeout_pass()
    except Exception:
        pass

    # P8: depositor-side resume retry for paused_* rows (host blip,
    # send failure, etc.). Bounded by fs_transit_max_retries; after the
    # last attempt the row flips to failed_in_transit and the user is
    # asked to redo.
    try:
        await _foreign_storage_transit_retry_pass()
    except Exception:
        pass

    # P8: host-side abandoned-chunk purge for deposits whose depositor
    # never came back to resume. TTL is operator-configurable, max 24 h.
    try:
        await _foreign_storage_abandoned_chunk_purge_pass()
    except Exception:
        pass

    # Auto-rescue: depositor-side salvage of our own deposits when a host
    # starts evicting (or, optionally, as TTL nears). Default-on; user
    # disables in Settings.
    try:
        await _foreign_storage_auto_rescue_pass()
    except Exception:
        pass


async def _foreign_storage_tripwire_pass() -> None:
    from nexus.core import events as _events
    from nexus.networking.storage_pump import deposit_dir as _ddir
    from nexus.runtime import foreign_storage_tripwire
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "host",
                        ForeignStorageDeposit.status == "stored",
                    )
                )
            )
            .scalars()
            .all()
        )
    for r in rows:
        try:
            dpath = _ddir(r.deposit_id, r.depositor_uuid)
            if not foreign_storage_tripwire.baseline_exists(dpath):
                continue
            changed = foreign_storage_tripwire.check_deposit(dpath)
            if not changed:
                continue
            already = bool(STATE.foreign_storage_tripwire_fired.get(r.deposit_id))
            if already:
                continue
            STATE.foreign_storage_tripwire_fired[r.deposit_id] = True
            await record_audit_event(
                "storage.unauthorized_access_detected",
                actor=LOCAL_SETTINGS.get("node_uuid", ""),
                task_id=r.deposit_id,
                severity="warning",
                details=f"changed={','.join(changed[:8])}",
            )
            _events.publish(
                "storage.unauthorized_access_detected",
                {
                    "deposit_id": r.deposit_id,
                    "depositor_uuid": r.depositor_uuid,
                    "changed_chunks": changed[:8],
                },
            )
            # Tell the depositor too so the toast lands on the side that cares.
            try:
                from nexus.networking.tunnel import _send_to_peer
                await _send_to_peer(
                    r.depositor_uuid,
                    {
                        "type": "storage_tripwire_fired",
                        "deposit_id": r.deposit_id,
                        "changed_chunks": changed[:8],
                    },
                )
            except Exception:
                pass
        except Exception:
            continue


_QUEUED_OFFLINE_TTL_HOURS = 24


async def _foreign_storage_queue_retry_pass() -> None:
    """Retry deposit offers that were queued because the target was offline.

    Three transitions per row:

    1. **TTL expired** (>24 h old): flip to ``withdrawn``, audit, drop the
       cached key + staged temp dir.
    2. **Key lost across restart**: same teardown — without the derived
       key we cannot fulfil the offer if accepted.
    3. **Target reachable**: re-send the offer; on success flip to
       ``offered``, audit. On failure leave queued for the next pass.
    """
    from datetime import datetime, timedelta, timezone

    from nexus.networking.storage_pump import build_storage_offer
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.security.foreign_storage_terms import DEFAULT_DEPOSITOR_TERMS
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event
    from nexus.telemetry.presence import is_peer_offline

    now = datetime.now(timezone.utc)
    ttl_cutoff = now - timedelta(hours=_QUEUED_OFFLINE_TTL_HOURS)

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "depositor",
                        ForeignStorageDeposit.status == "queued_offline",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                created = _parse_iso(row.created_at) if row.created_at else now
                expired = created < ttl_cutoff
                key_missing = foreign_storage_keys.get(row.deposit_id) is None
                if expired or key_missing:
                    reason = "ttl" if expired else "key_lost"
                    row.status = "withdrawn"
                    foreign_storage_keys.drop(row.deposit_id)
                    staged_dir = STATE.upload_temp_dirs_by_deposit.pop(
                        row.deposit_id, None
                    )
                    if staged_dir:
                        import shutil

                        try:
                            shutil.rmtree(staged_dir, ignore_errors=True)
                        except Exception:
                            pass
                    await record_audit_event(
                        "storage.deposit_retry_expired",
                        actor=LOCAL_SETTINGS.get("node_uuid", ""),
                        task_id=row.deposit_id,
                        details=f"reason={reason}",
                    )
                    continue
                if is_peer_offline(row.host_uuid):
                    continue
                # Target may be reachable now — re-send the same offer.
                # Re-sign the terms with the pair key; the stored
                # row signature is self-keyed and never verifies remotely.
                depositor_tc_bytes = DEFAULT_DEPOSITOR_TERMS.encode("utf-8")
                from hashlib import sha256 as _sha

                from nexus.runtime.foreign_storage_workflow import (
                    peer_signing_key,
                )
                from nexus.security.crypto import sign_bytes as _sign_bytes
                depositor_tc_sha = _sha(depositor_tc_bytes).hexdigest()
                sent = await _send_to_peer(
                    row.host_uuid,
                    build_storage_offer(
                        deposit_id=row.deposit_id,
                        total_bytes=int(row.total_bytes or 0),
                        chunk_count=int(row.chunk_count or 0),
                        salt=row.salt or b"",
                        password_hint=row.password_hint or "",
                        ttl_days=int(row.ttl_days or 30),
                        transport=row.transport or "stream",
                        cloud_url=row.cloud_url or "",
                        depositor_tc=depositor_tc_sha,
                        depositor_signature=_sign_bytes(
                            "foreign_storage_terms",
                            row.deposit_id,
                            depositor_tc_bytes,
                            key=await peer_signing_key(row.host_uuid),
                        ),
                        filename=row.filename or "",
                    ),
                )
                if sent:
                    row.status = "offered"
                    await record_audit_event(
                        "storage.deposit_retry_succeeded",
                        actor=LOCAL_SETTINGS.get("node_uuid", ""),
                        task_id=row.deposit_id,
                        details=f"target={row.host_uuid}",
                    )
            except Exception:
                continue
        await db.commit()


async def _foreign_storage_auto_offer_timeout_pass() -> None:
    """P2: time out auto-mode offers that nobody accepted in time.

    For every row in ``offering_multi``: if ``time.time() - started_at``
    has exceeded the user-configured ``fs_auto_offer_timeout_sec``, send
    ``storage_offer_cancelled`` to every remaining candidate, flip the
    row to ``withdrawn``, drop the cached key, and publish a UI event so
    the user is asked to redo the deposit. We do **not** auto-retry.
    """
    import time as _time

    from nexus.core import events as _events
    from nexus.networking.storage_pump import build_storage_offer_cancelled
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    timeout_sec = int(
        LOCAL_SETTINGS.get("fs_auto_offer_timeout_sec", 300) or 300
    )
    now = _time.time()

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "depositor",
                        ForeignStorageDeposit.status == "offering_multi",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                started = STATE.foreign_storage_auto_started_at.get(
                    row.deposit_id
                )
                if started is None:
                    # Lost STATE across restart — the offer can't be
                    # arbitrated anymore. Drop it so the user can redo.
                    row.status = "withdrawn"
                    foreign_storage_keys.drop(row.deposit_id)
                    STATE.foreign_storage_auto_candidates.pop(row.deposit_id, None)
                    await record_audit_event(
                        "storage.auto_offer_lost_state",
                        actor=LOCAL_SETTINGS.get("node_uuid", ""),
                        task_id=row.deposit_id,
                    )
                    _events.publish(
                        "storage.auto_offer_failed",
                        {"deposit_id": row.deposit_id, "reason": "lost_state"},
                    )
                    continue
                # Per-deposit override beats the node default.
                row_timeout = int(getattr(row, "offer_timeout_sec", 0) or 0)
                if (now - started) < (row_timeout or timeout_sec):
                    continue
                # Timed out — broadcast cancels, then bail.
                candidates = STATE.foreign_storage_auto_candidates.pop(
                    row.deposit_id, []
                )
                STATE.foreign_storage_auto_started_at.pop(row.deposit_id, None)
                for cand in candidates:
                    try:
                        await _send_to_peer(
                            cand,
                            build_storage_offer_cancelled(
                                row.deposit_id, reason="timeout"
                            ),
                        )
                    except Exception:
                        continue
                row.status = "withdrawn"
                foreign_storage_keys.drop(row.deposit_id)
                # Upload-temp parity with the queue-retry pass.
                staged_dir = STATE.upload_temp_dirs_by_deposit.pop(
                    row.deposit_id, None
                )
                if staged_dir:
                    import shutil
                    try:
                        shutil.rmtree(staged_dir, ignore_errors=True)
                    except Exception:
                        pass
                await record_audit_event(
                    "storage.auto_offer_timeout",
                    actor=LOCAL_SETTINGS.get("node_uuid", ""),
                    task_id=row.deposit_id,
                    details=f"timeout_sec={timeout_sec}",
                )
                _events.publish(
                    "storage.auto_offer_failed",
                    {
                        "deposit_id": row.deposit_id,
                        "reason": "timeout",
                        "timeout_sec": timeout_sec,
                    },
                )
            except Exception:
                continue
        await db.commit()


# P8: exponential backoff schedule for transit resumes — 30/60/120/240/480 s.
# After fs_transit_max_retries attempts fail, the row is flipped to
# failed_in_transit (no more auto-retries; the user must redo manually).
_TRANSIT_BACKOFF = (30, 60, 120, 240, 480, 960, 1920, 3600, 7200, 14400)


def _backoff_for_retry(retry_count: int) -> int:
    idx = min(retry_count, len(_TRANSIT_BACKOFF) - 1)
    return _TRANSIT_BACKOFF[idx]


async def _foreign_storage_transit_retry_pass() -> None:
    """P8: drive paused depositor rows back to transferring.

    Three transitions per row:

    1. ``retry_count >= max_retries`` → ``failed_in_transit`` + delete_now
       to wipe the host's partial chunks. UI is notified so the user can
       redo the deposit consciously.
    2. ``foreign_storage_keys`` cache empty (depositor restarted, no key)
       → leave the row as-is. Surfaced in the UI for the user to type
       their password and resume manually (see P8.6).
    3. Host reachable + backoff elapsed → send ``storage_resume_request``,
       increment ``retry_count``. Host's reply (``storage_resume_reply``)
       restarts the chunk pump with the missing-chunk list.
    """
    from datetime import datetime, timezone

    from sqlalchemy import or_
    from nexus.core import events as _events
    from nexus.networking.storage_pump import (
        build_storage_delete_now,
        build_storage_resume_request,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.security.crypto import sign_bytes
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event
    from nexus.telemetry.presence import is_peer_offline

    max_retries = int(
        LOCAL_SETTINGS.get("fs_transit_max_retries", 5) or 5
    )
    now = datetime.now(timezone.utc)

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "depositor",
                        or_(
                            ForeignStorageDeposit.status == "paused_send_failed",
                            ForeignStorageDeposit.status == "paused_silent",
                            ForeignStorageDeposit.status == "paused_host_shutdown",
                            ForeignStorageDeposit.status == "paused_host_down",
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                retries = int(row.retry_count or 0)
                # Per-deposit override beats the node default.
                row_max = int(getattr(row, "transit_retries", 0) or 0)
                if retries >= (row_max or max_retries):
                    # Game over — purge host-side bytes and tell the user.
                    sig = sign_bytes(
                        "foreign_storage_delete", row.deposit_id, b""
                    )
                    try:
                        await _send_to_peer(
                            row.host_uuid,
                            build_storage_delete_now(row.deposit_id, sig),
                        )
                    except Exception:
                        pass
                    row.status = "failed_in_transit"
                    foreign_storage_keys.drop(row.deposit_id)
                    await record_audit_event(
                        "storage.transit_failed_max_retries",
                        actor=LOCAL_SETTINGS.get("node_uuid", ""),
                        task_id=row.deposit_id,
                        details=f"retries={retries}",
                    )
                    _events.publish(
                        "storage.transit_failed",
                        {
                            "deposit_id": row.deposit_id,
                            "reason": "max_retries",
                            "retries": retries,
                        },
                    )
                    continue
                # Need the AES key cached to encrypt missing chunks.
                if foreign_storage_keys.get(row.deposit_id) is None:
                    # Depositor restarted; no key in RAM. Wait for the
                    # user to hit Resume + type the password (P8.6).
                    continue
                # Don't probe an offline host; respect the existing
                # presence layer the user already trusts.
                if is_peer_offline(row.host_uuid):
                    continue
                # Backoff: only retry once last_progress_at + backoff < now.
                last_at = _parse_iso(row.last_progress_at) if row.last_progress_at else _parse_iso(row.created_at)
                from datetime import timedelta
                if (now - last_at) < timedelta(seconds=_backoff_for_retry(retries)):
                    continue
                # Fire the resume request — the reply handler restarts the pump.
                sent = await _send_to_peer(
                    row.host_uuid,
                    build_storage_resume_request(row.deposit_id),
                )
                if not sent:
                    continue
                row.retry_count = retries + 1
                await record_audit_event(
                    "storage.transit_resume_requested",
                    actor=LOCAL_SETTINGS.get("node_uuid", ""),
                    task_id=row.deposit_id,
                    details=f"attempt={retries + 1}/{max_retries}",
                )
            except Exception:
                continue
        await db.commit()


async def _foreign_storage_abandoned_chunk_purge_pass() -> None:
    """P8: host-side — purge chunks for deposits that never resumed.

    Walks every ``role="host"`` row whose last activity was older than
    ``fs_transit_abandoned_chunk_ttl_hours``. Removes the chunk dir,
    flips the row to ``withdrawn`` and audits. Mirrors the existing
    delete-now teardown so we don't grow a second cleanup code path.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_
    from nexus.runtime import foreign_storage_tripwire
    from nexus.networking.storage_pump import deposit_dir
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    ttl_hours = int(
        LOCAL_SETTINGS.get("fs_transit_abandoned_chunk_ttl_hours", 24) or 24
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ttl_hours)

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "host",
                        or_(
                            ForeignStorageDeposit.status == "transferring",
                            ForeignStorageDeposit.status == "paused_send_failed",
                            ForeignStorageDeposit.status == "paused_silent",
                            ForeignStorageDeposit.status == "paused_depositor_shutdown",
                            ForeignStorageDeposit.status == "paused_depositor_down",
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                # Use last_progress_at if we have it, else fall back to created_at.
                anchor = row.last_progress_at or row.created_at
                if not anchor:
                    continue
                if _parse_iso(anchor) >= cutoff:
                    continue
                # Past TTL — purge chunks and mark withdrawn.
                from pathlib import Path
                dpath = deposit_dir(row.deposit_id, row.depositor_uuid)
                if dpath.exists():
                    try:
                        foreign_storage_tripwire.clear_baseline(dpath)
                    except Exception:
                        pass
                    for f in dpath.glob("chunk_*.enc"):
                        try:
                            f.unlink(missing_ok=True)
                        except Exception:
                            pass
                row.status = "withdrawn"
                await record_audit_event(
                    "storage.host_abandoned_chunks_purged",
                    actor=LOCAL_SETTINGS.get("node_uuid", ""),
                    task_id=row.deposit_id,
                    details=f"ttl_hours={ttl_hours}",
                )
            except Exception:
                continue
        await db.commit()


async def _foreign_storage_auto_rescue_pass() -> None:
    """Depositor-side: salvage our own deposits before a host drops them.

    A deposit is *at risk* when the host has started evicting it
    (``eviction_requested`` / ``in_db_grace``), or — when the trigger is
    ``days`` — while it is still ``stored`` but ``ttl_at`` is within the
    configured window. For each at-risk row we act once (tracked in
    ``STATE.foreign_storage_auto_rescue_seen`` so the 2 s tick doesn't
    repeat):

    * **cloud** — if ``fs_auto_rescue_cloud_cred`` is set, ask the host to
      stream the ciphertext to our bucket. Needs no deposit password.
    * **local** — download to ``fs_auto_rescue_dir``. Only works while the
      deposit key is unlocked this session (we never persist the
      password). If it isn't unlocked, the rescue dir lacks free space, or
      the host is offline, we leave the row at risk (the sidebar/bell
      warning stays) and notify the user once — telling them files may be
      lost if they don't act.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_

    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.presence import is_peer_offline

    from nexus.core.config import effective_auto_rescue, normalize_bool

    # Cheap guard: skip the whole pass only when the node default is off AND
    # no deposit has a per-deposit override (which could re-enable it).
    if not normalize_bool(LOCAL_SETTINGS.get("fs_auto_rescue", True), True) and not (
        LOCAL_SETTINGS.get("fs_auto_rescue_overrides") or {}
    ):
        return

    now = datetime.now(timezone.utc)

    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.role == "depositor",
                        or_(
                            ForeignStorageDeposit.status == "eviction_requested",
                            ForeignStorageDeposit.status == "in_db_grace",
                            ForeignStorageDeposit.status == "stored",
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

    seen = STATE.foreign_storage_auto_rescue_seen

    for row in rows:
        deposit_id = row.deposit_id
        try:
            # Per-deposit override (every element — enable/disable, mode,
            # trigger, days, cloud cred, dir, rclone targets), each falling
            # back to the node default.
            eff = effective_auto_rescue(deposit_id)

            evicting = row.status in ("eviction_requested", "in_db_grace")
            near_ttl = (
                eff["trigger"] == "days"
                and row.status == "stored"
                and row.ttl_at
                and _parse_iso(row.ttl_at) <= (now + timedelta(days=int(eff["days"])))
            )
            if not (evicting or near_ttl):
                # No longer at risk (e.g. user cancelled, or TTL pushed out)
                # — clear any prior marker so a future risk re-fires.
                seen.pop(deposit_id, None)
                continue
            if seen.get(deposit_id) == "started":
                continue  # rescue already kicked off this session
            if not eff["enabled"]:
                seen.pop(deposit_id, None)
                continue

            # Host must be reachable for any salvage path. Leave queued
            # (warning stays) and retry next tick — no per-tick notify.
            if is_peer_offline(row.host_uuid):
                continue

            fname = row.filename or deposit_id
            mode = eff["mode"]
            cloud_cred = eff["cloud_cred"]

            # A prior cloud attempt failed. For cloud_then_folder we now fall
            # back to the local folder; other modes have nothing left to try.
            if seen.get(deposit_id) == "cloud_failed":
                if mode == "cloud_then_folder":
                    if await _rescue_to_folder(row, fname, eff, seen) == "no_space":
                        await _notify_unrescuable(deposit_id, fname, "no_space", seen)
                continue

            if mode == "folder_only":
                if await _rescue_to_folder(row, fname, eff, seen) == "no_space":
                    await _notify_unrescuable(deposit_id, fname, "no_space", seen)
            elif mode == "cloud_only":
                if not await _rescue_to_cloud(row, fname, eff, seen, cloud_cred):
                    await _notify_unrescuable(deposit_id, fname, "no_cloud", seen)
            elif mode == "cloud_then_folder":
                if not await _rescue_to_cloud(row, fname, eff, seen, cloud_cred):
                    # No cloud configured → go straight to the folder.
                    if await _rescue_to_folder(row, fname, eff, seen) == "no_space":
                        await _notify_unrescuable(deposit_id, fname, "no_space", seen)
            else:  # folder_then_cloud (default)
                if await _rescue_to_folder(row, fname, eff, seen) == "no_space":
                    if not await _rescue_to_cloud(row, fname, eff, seen, cloud_cred):
                        await _notify_unrescuable(deposit_id, fname, "no_space", seen)
        except Exception:
            continue


async def _rescue_to_folder(row, fname: str, eff: dict, seen: dict) -> str:
    """Pull a deposit to the local rescue folder. Returns 'started' or 'no_space'.

    Decrypts straight to the file if the deposit is unlocked this session;
    otherwise saves the ciphertext for decrypt-later (locked path).
    """
    import shutil
    from pathlib import Path

    from nexus.networking.storage_pump import (
        build_storage_retrieve_open,
        rescued_deposit_dir,
        rescued_root,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.telemetry.audit import record_audit_event

    deposit_id = row.deposit_id
    rescue_root = rescued_root(deposit_id)
    try:
        rescue_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(rescue_root).free
    except Exception:
        free = 0
    if free < int(row.total_bytes or 0) + (256 * 1024 * 1024):  # 256 MB head-room
        return "no_space"

    chunk_count = int(row.chunk_count or 0)
    key = foreign_storage_keys.get(deposit_id)
    if key is not None:
        entry = foreign_storage_keys.get_entry(deposit_id) or {}
        entry["save_to"] = str(rescue_root / Path(fname).name)
        audit_action = "storage.auto_rescue_download_started"
        details = f"file={fname} mode=plaintext dir={rescue_root}"
    else:
        raw_dir = rescued_deposit_dir(deposit_id)
        foreign_storage_keys.store(deposit_id, b"", raw_dir=str(raw_dir))
        audit_action = "storage.auto_rescue_encrypted_started"
        details = f"file={fname} mode=encrypted dir={raw_dir}"

    await _send_to_peer(
        row.host_uuid, build_storage_retrieve_open(deposit_id, 0, chunk_count - 1)
    )
    seen[deposit_id] = "started"
    await record_audit_event(
        audit_action,
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=details,
    )
    return "started"


async def _rescue_to_cloud(row, fname: str, eff: dict, seen: dict, cloud_cred: str) -> bool:
    """Kick off a cloud rescue. Returns True if cloud is configured (an attempt
    was made), False if neither a cloud credential nor an rclone target exists.

    Two backends: a stored CloudCredential (host streams to your bucket), or
    depositor-side rclone streaming. The credential takes precedence.
    """
    from nexus.telemetry.audit import record_audit_event

    deposit_id = row.deposit_id
    if cloud_cred:
        from nexus.runtime.foreign_storage_cloud import (
            CloudEvictionError,
            request_cloud_eviction,
        )
        try:
            await request_cloud_eviction(deposit_id, cloud_cred)
            seen[deposit_id] = "started"
            await record_audit_event(
                "storage.auto_rescue_cloud_started",
                actor=LOCAL_SETTINGS.get("node_uuid", ""),
                task_id=deposit_id,
            )
        except CloudEvictionError as exc:
            seen[deposit_id] = "cloud_failed"
            await record_audit_event(
                "storage.auto_rescue_failed",
                actor=LOCAL_SETTINGS.get("node_uuid", ""),
                task_id=deposit_id,
                severity="warning",
                details=f"file={fname} reason=cloud:{exc}",
            )
        return True

    from nexus.runtime import foreign_storage_rclone as _rcl

    targets = [str(t).strip() for t in (eff["rclone_targets"] or []) if str(t).strip()]
    if targets and _rcl.rclone_available():
        seen[deposit_id] = "started"
        asyncio.create_task(
            _rcl.overflow_rescue(
                deposit_id, row.host_uuid, fname, int(row.chunk_count or 0), targets
            ),
            name=f"nexus.foreign_storage.rclone.{deposit_id}",
        )
        await record_audit_event(
            "storage.auto_rescue_cloud_stream_started",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            details=f"file={fname} targets={len(targets)}",
        )
        return True
    return False


async def _notify_unrescuable(deposit_id: str, fname: str, reason: str, seen: dict) -> None:
    """Audit once that a deposit couldn't be recovered (bell warns the user)."""
    from nexus.telemetry.audit import record_audit_event

    if seen.get(deposit_id) != reason:
        seen[deposit_id] = reason
        await record_audit_event(
            "storage.auto_rescue_failed",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            severity="warning",
            details=f"file={fname} reason={reason} msg=files_may_be_lost",
        )


def _parse_iso(s: str):
    from datetime import datetime, timezone

    if not s:
        return datetime.now(timezone.utc)
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        # Repository stores epoch-seconds in some places; fall back.
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except Exception:
            from datetime import datetime as _dt

            return _dt.now(timezone.utc)


async def _move_to_db_grace(db, row, now) -> None:
    from pathlib import Path

    from nexus.storage import ForeignStorageDBGrace
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import iso_now, timestamp

    pump = STATE.foreign_storage_pumps.get(row.deposit_id) or {}
    dpath = Path(pump.get("dir") or "")
    blob = b""
    if dpath.exists():
        # Concat all chunks into a single encrypted blob for DB grace storage.
        chunks = sorted(dpath.glob("chunk_*.enc"))
        try:
            blob = b"".join(c.read_bytes() for c in chunks)
        except Exception:
            blob = b""
        # Wipe the on-disk landing zone now; the bytes live in DB grace.
        try:
            from nexus.runtime import foreign_storage_tripwire
            foreign_storage_tripwire.clear_baseline(dpath)
        except Exception:
            pass
        try:
            for c in chunks:
                c.unlink(missing_ok=True)
        except Exception:
            pass
        # Tear down the now-empty deposit dir + (if empty) its parent
        # depositor dir, so the host's cache doesn't accumulate
        # ghost directories after auto-eviction sweeps.
        try:
            from nexus.runtime.foreign_storage_workflow import _rmdir_cascade
            _rmdir_cascade(dpath)
        except Exception:
            pass

    db.add(
        ForeignStorageDBGrace(
            deposit_id=row.deposit_id,
            encrypted_blob=blob,
            expires_at=iso_now(),
        )
    )
    row.status = "in_db_grace"
    row.db_grace_at = iso_now()
    await record_audit_event(
        "storage.deposit_decommissioned",
        actor=row.deposit_id,
        task_id=row.deposit_id,
        details=f"db_grace_bytes={len(blob)}",
    )


async def _purge_deposit(db, row, now) -> None:
    from sqlalchemy import delete as sa_delete

    from nexus.storage import ForeignStorageDBGrace
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import timestamp

    await db.execute(
        sa_delete(ForeignStorageDBGrace).filter(
            ForeignStorageDBGrace.deposit_id == row.deposit_id
        )
    )
    row.status = "purged"
    row.purged_at = timestamp()
    STATE.foreign_storage_pumps.pop(row.deposit_id, None)
    await record_audit_event(
        "storage.deposit_purged",
        actor=row.deposit_id,
        task_id=row.deposit_id,
    )


async def foreign_storage_key_gc_pass() -> None:
    """Drop session keys idle past the TTL.

    Idle threshold defaults to
    :data:`nexus.runtime.foreign_storage_keys.DEFAULT_IDLE_TTL_S`.
    Each evicted deposit produces a `storage.deposit_locked_idle`
    audit row.
    """
    from nexus.runtime import foreign_storage_keys, preview_pump
    from nexus.telemetry.audit import record_audit_event

    evicted = foreign_storage_keys.gc()
    for deposit_id in evicted:
        preview_pump.drop_deposit(deposit_id)
        await record_audit_event(
            "storage.deposit_locked_idle",
            actor="scheduler",
            task_id=deposit_id,
        )


async def _notify_dependents_after_promotion(
    task_id: str, new_primary: str, rec: dict
) -> None:
    """Tell each consumer of *task_id* about the new primary and grant access."""
    from nexus.networking.tunnel import _send_to_peer

    consumers = list(STATE.service_dependents.get(task_id) or [])
    if not consumers:
        return

    ports = rec.get("expose_ports") or []
    if not ports:
        return
    dep_port = int(ports[0])

    consumer_workers: list[str] = []
    for consumer_task_id in consumers:
        consumer_rec = STATE.service_records.get(consumer_task_id) or {}
        worker = str(consumer_rec.get("worker_id") or "")
        if not worker:
            continue
        consumer_workers.append(worker)
        try:
            await _send_to_peer(
                worker,
                {
                    "type": "service_dep_changed",
                    "task_id": task_id,
                    "primary": new_primary,
                    "port": dep_port,
                },
            )
        except Exception:
            pass

    if consumer_workers:
        try:
            await _send_to_peer(
                new_primary,
                {
                    "type": "service_dep_grant",
                    "task_id": task_id,
                    "peers": consumer_workers,
                },
            )
        except Exception:
            pass


__all__ = ["dag_scheduler_loop", "service_health_pass"]
