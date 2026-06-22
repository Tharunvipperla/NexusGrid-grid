"""Foreign-storage transfer pump.

Mirrors the tunnel pump pattern but for a different problem
shape: one-shot file deposit with chunked WS frames + per-chunk ACK.
Eight kilobyte chunks (smaller than the 32 KB tunnel chunk) so the
event loop yields more often — the goal is to never starve a running
service task.

Frame surface (eleven types, all dispatched through the existing three
sites — worker_client, relay_client, websocket):

* depositor → host:
    - ``storage_offer``       — initial offer + T&C signature.
    - ``storage_chunk``       — encrypted chunk + chunk_idx.
    - ``storage_complete``    — depositor's final signature.
    - ``storage_eviction_response`` — host's eviction got a verdict.
    - ``storage_retrieve_open`` — request to read back chunks.
    - ``storage_delete_now``  — proactive depositor wipe.
* host → depositor:
    - ``storage_offer_response`` — accept or decline + host T&C.
    - ``storage_chunk_ack``      — per-chunk ack.
    - ``storage_eviction_request`` — host wants the bytes off the disk.
    - ``storage_retrieve_chunk`` — bytes coming back to depositor.
* host → new-host (forwarding):
    - ``storage_forward_init``.

This module owns the wire builders + the depositor-side ``transfer_deposit``
async function. Host receivers live in :mod:`nexus.api.local` (for HTTP
endpoints) and the dispatch sites (for direct frame handling).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from nexus.core import STATE
from nexus.security.deposit_crypto import GCM_OVERHEAD, encrypt_chunk

_log = logging.getLogger("nexus.networking.storage_pump")

CHUNK_PLAINTEXT_BYTES = 64 * 1024  # 64 KB plaintext per chunk (single-flight ack pattern: bigger chunks = ~8x throughput on the same RTT)
CHUNK_CIPHERTEXT_BYTES = CHUNK_PLAINTEXT_BYTES + GCM_OVERHEAD
ACK_TIMEOUT_SEC = 30.0


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def build_storage_offer(
    deposit_id: str,
    total_bytes: int,
    chunk_count: int,
    salt: bytes,
    password_hint: str,
    ttl_days: int,
    transport: str,
    cloud_url: str,
    depositor_tc: str,
    depositor_signature: str,
    *,
    filename: str = "",
) -> dict:
    # ``password_hint`` is intentionally not sent over the wire — it's a
    # depositor-only memory aid and never useful to the host (the host
    # holds ciphertext only and never derives the AES key). The argument
    # stays in the signature so old call sites compile cleanly; the value
    # is discarded here. Defense-in-depth alongside the host-side stores
    # in :mod:`nexus.runtime.foreign_storage_workflow` and the host's
    # ``/foreign_storage/incoming`` response in :mod:`nexus.api.local`.
    _ = password_hint
    frame = {
        "type": "storage_offer",
        "deposit_id": deposit_id,
        "total_bytes": int(total_bytes),
        "chunk_count": int(chunk_count),
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "ttl_days": int(ttl_days),
        "transport": transport,
        "cloud_url": cloud_url,
        "depositor_tc_sha256": depositor_tc,
        "depositor_signature": depositor_signature,
    }
    # Surface the original filename so the host UI displays something
    # human-readable instead of a raw uuid. Sealed inside the manifest
    # too, but the host has no key — this is the only path that gives
    # them a name to show.
    if filename:
        frame["filename"] = str(filename)
    return frame


def build_storage_offer_response(
    deposit_id: str,
    accepted: bool,
    host_tc: str,
    host_signature: str,
    reason: str = "",
) -> dict:
    return {
        "type": "storage_offer_response",
        "deposit_id": deposit_id,
        "accepted": bool(accepted),
        "host_tc_sha256": host_tc,
        "host_signature": host_signature,
        "reason": reason,
    }


def build_storage_offer_cancelled(deposit_id: str, reason: str = "") -> dict:
    """P2: depositor → candidate, "you can drop this offer".

    Sent when the auto-mode fan-out has already been won by another
    candidate, or the user-configured timeout elapsed. Reason is
    cosmetic ("won_by_other" | "timeout").
    """
    return {
        "type": "storage_offer_cancelled",
        "deposit_id": deposit_id,
        "reason": reason,
    }


def build_storage_pause(deposit_id: str, reason: str) -> dict:
    """P8: bidirectional "pause this transfer" notification.

    Sent on graceful shutdown (reason=host_shutdown / depositor_shutdown)
    or when the sender's pump itself bailed (reason=send_failed /
    silent). The receiver flips the row to ``paused_<reason>`` and stops
    sending/listening until the lifecycle pass triggers a resume.
    """
    return {
        "type": "storage_pause",
        "deposit_id": deposit_id,
        "reason": reason,
    }


def build_storage_resume_request(deposit_id: str) -> dict:
    """P8: depositor → host, "what chunk_idx do you actually have?"

    Sent before restarting the pump after a pause. The host replies
    with ``storage_resume_reply`` listing the chunk indices it holds on
    disk so the depositor sends only what's missing.
    """
    return {
        "type": "storage_resume_request",
        "deposit_id": deposit_id,
    }


def build_storage_resume_reply(
    deposit_id: str, received_chunks: list[int]
) -> dict:
    """P8: host → depositor, the list of chunk_idx already on host disk.

    Sparse int list rather than a bitmap — for the typical "mostly
    contiguous up to N" case it's compact and JSON-friendly. Depositor
    diffs this against ``[0..chunk_count)`` to know what to send.
    """
    return {
        "type": "storage_resume_reply",
        "deposit_id": deposit_id,
        "received_chunks": list(received_chunks),
    }


def build_storage_missing_chunks(
    deposit_id: str, missing: list[int]
) -> dict:
    """P8.8: host → depositor, "I never received these chunk indices."

    Sent from ``_handle_complete`` when the host scans its on-disk chunk
    dir and finds gaps relative to the depositor's declared chunk_count.
    Depositor re-launches the pump with ``chunk_indices_to_send=missing``;
    bounded by ``fs_transit_max_retries`` rounds before the row flips to
    ``failed_in_transit``.
    """
    return {
        "type": "storage_missing_chunks",
        "deposit_id": deposit_id,
        "missing": [int(i) for i in missing],
    }


def build_storage_chunk(deposit_id: str, chunk_idx: int, blob: bytes) -> dict:
    return {
        "type": "storage_chunk",
        "deposit_id": deposit_id,
        "chunk_idx": int(chunk_idx),
        "b64": base64.b64encode(blob).decode("ascii"),
    }


def build_storage_chunk_ack(
    deposit_id: str, chunk_idx: int, ok: bool, reason: str = ""
) -> dict:
    return {
        "type": "storage_chunk_ack",
        "deposit_id": deposit_id,
        "chunk_idx": int(chunk_idx),
        "ok": bool(ok),
        "reason": reason,
    }


def build_storage_complete(deposit_id: str, depositor_signature: str) -> dict:
    return {
        "type": "storage_complete",
        "deposit_id": deposit_id,
        "depositor_signature_final": depositor_signature,
    }


def build_storage_eviction_request(
    deposit_id: str,
    response_window_days: int,
    urgency: str = "normal",
    *,
    total_days: int = 0,
) -> dict:
    """Build the host→depositor eviction-request frame.

    ``total_days`` is the host's full configured countdown (response
    window + db grace). Old clients that ignore it fall back to the
    legacy 1+2=3 day default in their UI math.
    """
    frame = {
        "type": "storage_eviction_request",
        "deposit_id": deposit_id,
        "response_window_days": int(response_window_days),
        "urgency": urgency,
    }
    if total_days > 0:
        frame["total_days"] = int(total_days)
    return frame


def build_storage_eviction_cancelled(deposit_id: str) -> dict:
    """Host→depositor: an eviction request was cancelled (e.g. host
    clicked the Cancel button on an accidentally-evicted deposit).
    The depositor reverts the row to ``stored`` and clears countdowns.
    """
    return {
        "type": "storage_eviction_cancelled",
        "deposit_id": deposit_id,
    }


def build_storage_eviction_response(
    deposit_id: str,
    action: str,
    target_uuid: str = "",
    *,
    cloud_provider: str = "",
    cloud_dest: str = "",
    cloud_eviction_nonce_b64: str = "",
    cloud_credential_blob_b64: str = "",
) -> dict:
    """Depositor's verdict on an eviction.

    actions: ``download | forward | let_go``.
    adds ``cloud`` — the host streams the still-encrypted bundle
    to the depositor's external bucket. The four ``cloud_*`` fields are
    only populated for ``action == "cloud"`` and carry the per-eviction
    transit-wrapped credential blob (see
    :mod:`nexus.security.cred_crypto`).
    """
    return {
        "type": "storage_eviction_response",
        "deposit_id": deposit_id,
        "action": action,  # "download" | "forward" | "let_go" | "cloud"
        "target_uuid": target_uuid,
        "cloud_provider": cloud_provider,
        "cloud_dest": cloud_dest,
        "cloud_eviction_nonce_b64": cloud_eviction_nonce_b64,
        "cloud_credential_blob_b64": cloud_credential_blob_b64,
    }


def build_storage_cloud_upload_progress(
    deposit_id: str, bytes_sent: int, total_bytes: int
) -> dict:
    """Host → depositor periodic upload progress."""
    return {
        "type": "storage_cloud_upload_progress",
        "deposit_id": deposit_id,
        "bytes_sent": int(bytes_sent),
        "total_bytes": int(total_bytes),
    }


def build_storage_cloud_upload_complete(
    deposit_id: str, cloud_object_id: str, sha256_uploaded: str = ""
) -> dict:
    """Host → depositor cloud upload finished, host wiped local copy."""
    return {
        "type": "storage_cloud_upload_complete",
        "deposit_id": deposit_id,
        "cloud_object_id": cloud_object_id,
        "sha256_uploaded": sha256_uploaded,
    }


def build_storage_cloud_upload_failed(deposit_id: str, reason: str) -> dict:
    """Host → depositor cloud upload failed, deposit fell back to db_grace."""
    return {
        "type": "storage_cloud_upload_failed",
        "deposit_id": deposit_id,
        "reason": reason,
    }


def build_storage_retrieve_open(
    deposit_id: str, first_chunk_idx: int, last_chunk_idx: int
) -> dict:
    return {
        "type": "storage_retrieve_open",
        "deposit_id": deposit_id,
        "first_chunk_idx": int(first_chunk_idx),
        "last_chunk_idx": int(last_chunk_idx),
    }


def build_storage_retrieve_chunk(
    deposit_id: str, chunk_idx: int, blob: bytes
) -> dict:
    return {
        "type": "storage_retrieve_chunk",
        "deposit_id": deposit_id,
        "chunk_idx": int(chunk_idx),
        "b64": base64.b64encode(blob).decode("ascii"),
    }


def build_storage_delete_now(deposit_id: str, signature: str) -> dict:
    return {
        "type": "storage_delete_now",
        "deposit_id": deposit_id,
        "signature": signature,
    }


def build_storage_forward_init(
    deposit_id: str,
    depositor_uuid: str,
    total_bytes: int,
    chunk_count: int,
    transferred_signature: str,
) -> dict:
    return {
        "type": "storage_forward_init",
        "deposit_id": deposit_id,
        "depositor_uuid": depositor_uuid,
        "total_bytes": int(total_bytes),
        "chunk_count": int(chunk_count),
        "transferred_signature": transferred_signature,
    }


# ---------------------------------------------------------------------------
# Depositor-side pump
# ---------------------------------------------------------------------------

async def transfer_deposit(
    deposit_id: str,
    peer_uuid: str,
    file_path: Path,
    derived_key: bytes,
    throttle: Any | None = None,
    *,
    on_progress: Any = None,
    chunk_indices_to_send: Any = None,
) -> bool:
    """Stream *file_path* to *peer_uuid* as encrypted chunks.

    Returns True iff every chunk was acked. Designed to be cancellation-safe:
    on cancel the pump entry is removed and the partial state is left for
    the lifecycle pass to clean up.

    Throttle is any object with ``async def acquire(n: int) -> None`` (the
    class:`StorageThrottle`); pass ``None`` to skip.

    P8: ``chunk_indices_to_send`` is an optional iterable of chunk_idx
    values to send. When None, behaves like before (iterate 0..N).
    Resume callers pass the diff between ``[0..chunk_count)`` and the
    host's already-received list. On any send-failure / ack-timeout
    the pump emits ``storage_pause(reason="send_failed")`` to the host
    and persists the row as ``paused_send_failed`` so the lifecycle
    retry pass can pick it up.
    """
    from nexus.core import LOCAL_SETTINGS as _LS
    from nexus.networking.tunnel import _send_to_peer

    # P8.8: capture the pre-pump size + mtime so we can detect file-change
    # during transit. If either drifts between chunk reads, the pump aborts
    # and the row flips to ``failed_in_transit`` (the user must redo the
    # deposit — no resume path for a tampered source file).
    initial_stat = await asyncio.to_thread(_stat_file, file_path)
    file_size = initial_stat["size"]
    initial_mtime_ns = initial_stat["mtime_ns"]
    chunk_count = (file_size + CHUNK_PLAINTEXT_BYTES - 1) // CHUNK_PLAINTEXT_BYTES

    # P8: per-chunk ack timeout is operator-tunable (default 30 s); the
    # deposit row may carry its own value (read together with the window
    # override below). Resolved after the row lookup.
    ack_timeout = max(
        5.0, float(_LS.get("fs_transit_chunk_ack_timeout_sec", 30) or 30)
    )

    if chunk_indices_to_send is None:
        send_plan: list[int] = list(range(chunk_count))
    else:
        send_plan = [int(i) for i in chunk_indices_to_send]

    pump = {
        "role": "depositor",
        "peer_uuid": peer_uuid,
        "deposit_id": deposit_id,
        "total_bytes": file_size,
        "chunk_count": chunk_count,
        "sent_idx": -1,
        "acked_idx": -1,
        "ack_events": {},
        "status": "transferring",
        "started_at": time.time(),
        # P8: batched persistence — write to DB every 16 acks or 2 s,
        # whichever first, so a crash re-sends at most 16 chunks.
        "last_persist_at": time.time(),
        "acks_since_persist": 0,
    }
    async with STATE.foreign_storage_lock:
        STATE.foreign_storage_pumps[deposit_id] = pump

    # Pipelined sender. The previous version was strict
    # single-flight (send → wait ack → send next), which capped
    # throughput at ``chunk_size / RTT``. We now keep up to
    # ``window`` chunks in flight, naturally backpressuring the
    # producer through an asyncio.Semaphore that the per-chunk ack
    # waiter releases. Window is configurable via
    # ``LOCAL_SETTINGS["storage_window_chunks"]`` so an operator can
    # widen it on a high-BDP link or narrow it on a memory-tight host.

    # Per-deposit overrides first (set by the depositor at creation, ride
    # the row so resumes keep the same values); 0 falls back to the node
    # setting. Clamped to the same bounds the settings path enforces.
    window = 0
    try:
        from nexus.storage import ForeignStorageDeposit, get_session

        async with get_session() as _session:
            _row = await _session.get(ForeignStorageDeposit, deposit_id)
            window = int(getattr(_row, "window_chunks", 0) or 0) if _row else 0
            _row_ack = int(getattr(_row, "ack_timeout_sec", 0) or 0) if _row else 0
            if _row_ack:
                ack_timeout = float(max(5, min(300, _row_ack)))
    except Exception:
        window = 0
    if window <= 0:
        window = int(_LS.get("storage_window_chunks", 32) or 32)
    window = max(1, min(128, window))
    window_sem = asyncio.Semaphore(window)
    ack_tasks: list[asyncio.Task] = []
    failure: list[str] = []  # populated by first failing waiter; one slot is enough

    async def _wait_ack(idx: int) -> None:
        evt = pump["ack_events"].get(idx)
        try:
            if evt is None:
                if not failure:
                    failure.append(f"missing ack event for chunk {idx}")
                return
            try:
                await asyncio.wait_for(evt.wait(), timeout=ack_timeout)
            except asyncio.TimeoutError:
                if not failure:
                    failure.append(f"ack_timeout chunk={idx}")
                return
            pump["ack_events"].pop(idx, None)
            # ``acked_idx`` tracks the highest contiguous-acked index for
            # progress reporting. Out-of-order acks are fine; we just record
            # the max and let the host's filename-by-index storage handle
            # the actual byte placement.
            pump["acked_idx"] = max(pump["acked_idx"], idx)
            pump["acks_since_persist"] = pump.get("acks_since_persist", 0) + 1
            now_ack = time.time()
            if (
                pump["acks_since_persist"] >= 16
                or (now_ack - pump.get("last_persist_at", 0)) >= 2.0
            ):
                # Best-effort persistence — failures fall through and the
                # next ack will retry. Never block the pump on a DB hiccup.
                try:
                    await _persist_acked_progress(deposit_id, pump["acked_idx"])
                except Exception:
                    pass
                pump["acks_since_persist"] = 0
                pump["last_persist_at"] = now_ack
            if on_progress is not None:
                try:
                    on_progress(pump["acked_idx"] + 1, chunk_count)
                except Exception:
                    pass
        finally:
            window_sem.release()

    fh = await asyncio.to_thread(open, file_path, "rb")
    try:
        for chunk_idx in send_plan:
            if failure:
                break
            # Throttle gate first so the producer respects the configured
            # bandwidth profile even when the window has room.
            if throttle is not None:
                await throttle.acquire(CHUNK_CIPHERTEXT_BYTES)

            await window_sem.acquire()
            if failure:
                window_sem.release()
                break

            # P8.8: cheap drift check before each read. If size/mtime have
            # moved since the pump started, the file under us has changed;
            # bail with file_changed so the workflow can wipe the host's
            # chunks and surface a "start a new deposit" toast.
            try:
                live = await asyncio.to_thread(_fstat_file, fh)
            except Exception:
                live = None
            if live is not None and (
                live["size"] != file_size or live["mtime_ns"] != initial_mtime_ns
            ):
                window_sem.release()
                pump["status"] = "file_changed"
                if not failure:
                    failure.append(f"file_changed chunk={chunk_idx}")
                break

            # P8: seek to the chunk's plaintext offset so resume mode
            # (non-contiguous indices) reads from the right place. The
            # sequential case is the degenerate seek-to-the-next-byte.
            await asyncio.to_thread(fh.seek, chunk_idx * CHUNK_PLAINTEXT_BYTES)
            plaintext = await asyncio.to_thread(fh.read, CHUNK_PLAINTEXT_BYTES)
            if not plaintext:
                window_sem.release()
                break
            blob = encrypt_chunk(derived_key, plaintext, chunk_idx)
            evt: asyncio.Event = asyncio.Event()
            pump["ack_events"][chunk_idx] = evt

            ok = await _send_to_peer(
                peer_uuid, build_storage_chunk(deposit_id, chunk_idx, blob)
            )
            if not ok:
                window_sem.release()
                pump["status"] = "send_failed"
                if not failure:
                    failure.append(f"send_failed chunk={chunk_idx}")
                break
            pump["sent_idx"] = chunk_idx
            ack_tasks.append(asyncio.create_task(_wait_ack(chunk_idx)))

        # Drain whatever's still in flight, even on failure, so callers
        # don't return before the per-chunk waiters have finished
        # touching pump state.
        if ack_tasks:
            await asyncio.gather(*ack_tasks, return_exceptions=True)

        # P8: final flush — persist the highest contiguous ack regardless
        # of whether we crossed the batch threshold. Resume protocol
        # depends on this being durable.
        try:
            await _persist_acked_progress(deposit_id, pump["acked_idx"])
        except Exception:
            pass

        if failure:
            reason = failure[0].split()[0]
            pump["status"] = reason
            # P8.8: file_changed is terminal — the source file drifted under
            # us, the depositor must redo the deposit from scratch. We tell
            # the host to wipe and flip the row to failed_in_transit (not a
            # pause). Other failures keep the existing send_failed → resume
            # path.
            if reason == "file_changed":
                try:
                    from nexus.security.crypto import sign_bytes as _sb
                    sig = _sb("foreign_storage_delete", deposit_id, b"")
                    await _send_to_peer(
                        peer_uuid,
                        build_storage_delete_now(deposit_id, sig),
                    )
                except Exception:
                    pass
                try:
                    await _persist_failed_in_transit(
                        deposit_id, "file_changed"
                    )
                except Exception:
                    pass
                # Drop the cached key — no resume is possible from here.
                try:
                    from nexus.runtime import foreign_storage_keys as _keys
                    _keys.drop(deposit_id)
                except Exception:
                    pass
                try:
                    from nexus.core import events as _events
                    _events.publish(
                        "storage.transit_failed",
                        {
                            "deposit_id": deposit_id,
                            "reason": "file_changed",
                        },
                    )
                except Exception:
                    pass
                return False
            # P8: tell the host we're pausing and persist our row so the
            # retry pass can pick it up. Best-effort; pump exit is the
            # ground truth, the pause frame is a hint to the host.
            try:
                await _send_to_peer(
                    peer_uuid,
                    build_storage_pause(deposit_id, reason="send_failed"),
                )
            except Exception:
                pass
            try:
                await _persist_paused(deposit_id, "send_failed")
            except Exception:
                pass
            return False
        pump["status"] = "completed"
        return True
    finally:
        await asyncio.to_thread(fh.close)


async def _persist_acked_progress(deposit_id: str, acked_idx: int) -> None:
    """P8: write the highest contiguous ack idx + last_progress timestamp
    to the depositor row. Cheap (one row, two cols) and bounded by the
    batched-every-16-or-2s policy in the pump.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select as _sel
    from nexus.storage import ForeignStorageDeposit, get_session

    if acked_idx < 0:
        return
    async with get_session() as db:
        row = (
            await db.execute(
                _sel(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        prior = int(row.transferred_chunks or 0)
        if acked_idx + 1 <= prior:
            return  # nothing newer to record
        row.transferred_chunks = acked_idx + 1
        row.last_progress_at = datetime.now(timezone.utc).isoformat()
        await db.commit()


async def _persist_failed_in_transit(deposit_id: str, reason: str) -> None:
    """P8.8: terminal failure on the depositor row. No retry path from here —
    the user must create a new deposit. Used for file_changed and exhausted
    retries.
    """
    from sqlalchemy import select as _sel
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                _sel(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.status in {"stored", "withdrawn", "purged"}:
            return
        row.status = "failed_in_transit"
        row.pause_reason = reason
        await db.commit()


async def _persist_paused(deposit_id: str, reason: str) -> None:
    """P8: flip a depositor row to ``paused_<reason>`` so the lifecycle
    retry pass can find it. Idempotent — re-pause of an already-paused
    row updates the reason but does not bump retry_count (that belongs
    to the lifecycle pass, not the pump).
    """
    from sqlalchemy import select as _sel
    from nexus.storage import ForeignStorageDeposit, get_session

    async with get_session() as db:
        row = (
            await db.execute(
                _sel(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        # Stored/withdrawn/failed_in_transit are terminal — don't regress.
        if row.status in {"stored", "withdrawn", "failed_in_transit", "purged"}:
            return
        row.status = f"paused_{reason}"
        row.pause_reason = reason
        await db.commit()


def record_chunk_ack(deposit_id: str, chunk_idx: int, ok: bool) -> None:
    """Called by the dispatch site when a ``storage_chunk_ack`` arrives."""
    pump = STATE.foreign_storage_pumps.get(deposit_id)
    if not pump or pump.get("role") != "depositor":
        return
    evt = pump.get("ack_events", {}).get(chunk_idx)
    if evt is None:
        return
    if not ok:
        pump["status"] = "ack_negative"
    evt.set()


# ---------------------------------------------------------------------------
# Host-side chunk receiver
# ---------------------------------------------------------------------------

def _stat_size(path: Path | str) -> int:
    return Path(path).stat().st_size


def _stat_file(path: Path | str) -> dict:
    """P8.8: capture (size, mtime_ns) atomically for drift detection."""
    st = Path(path).stat()
    return {"size": st.st_size, "mtime_ns": int(st.st_mtime_ns)}


def _fstat_file(fh) -> dict:
    """P8.8: re-stat an already-open fh — cheap inode lookup, no path resolution."""
    import os as _os
    st = _os.fstat(fh.fileno())
    return {"size": st.st_size, "mtime_ns": int(st.st_mtime_ns)}


def deposit_dir(deposit_id: str, depositor_uuid: str) -> Path:
    """On-disk landing zone for a deposit's ciphertext."""
    from nexus.core import cache_dir, get_node_port

    base = (
        cache_dir(get_node_port())
        / "foreign_storage"
        / depositor_uuid
        / deposit_id
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def rescued_root(deposit_id: str | None = None) -> Path:
    """Depositor-side folder where auto-rescued deposits land.

    Defaults to ``<data>/rescued``. A ``deposit_id`` resolves through that
    deposit's per-deposit override dir (if set) before the node-wide
    ``fs_auto_rescue_dir`` setting, so the rescue pass and the
    decrypt-later endpoint always agree on the location.
    """
    from nexus.core import cache_dir, get_node_port

    base = ""
    if deposit_id:
        from nexus.core.config import effective_auto_rescue
        base = str(effective_auto_rescue(deposit_id).get("dir") or "").strip()
    if not base:
        from nexus.core import LOCAL_SETTINGS
        base = str(LOCAL_SETTINGS.get("fs_auto_rescue_dir", "") or "").strip()
    return Path(base) if base else (cache_dir(get_node_port()) / "rescued")


def rescued_deposit_dir(deposit_id: str) -> Path:
    """Per-deposit folder holding rescued *ciphertext* chunks awaiting decrypt."""
    return rescued_root(deposit_id) / deposit_id


async def receive_chunk(
    peer_uuid: str, frame: dict, throttle: Any | None = None
) -> None:
    """Host-side handler for an incoming ``storage_chunk`` frame.

    Persists the encrypted blob to ``cache_dir/foreign_storage/<dep>/<id>/
    chunk_NNNNNNNN.enc`` and emits a positive ``storage_chunk_ack``. On
    failure the ack is negative and the depositor's pump bails.
    """
    from nexus.networking.tunnel import _send_to_peer

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return
    try:
        chunk_idx = int(frame.get("chunk_idx", -1))
    except (TypeError, ValueError):
        chunk_idx = -1
    if chunk_idx < 0:
        return

    pump = STATE.foreign_storage_pumps.get(deposit_id) or {}
    if pump.get("role") != "host":
        await _send_to_peer(
            peer_uuid,
            build_storage_chunk_ack(deposit_id, chunk_idx, False, "no_pump"),
        )
        return

    # Security F-011: bound the index to the count the host agreed to host, so an
    # accepted depositor can't write extra/high-index chunks beyond the agreed
    # deposit size (disk-exhaustion). chunk_count==0 (unknown) skips the bound.
    agreed = int(pump.get("chunk_count") or 0)
    if agreed and chunk_idx >= agreed:
        await _send_to_peer(
            peer_uuid,
            build_storage_chunk_ack(deposit_id, chunk_idx, False, "out_of_range"),
        )
        return

    if throttle is not None:
        await throttle.acquire(CHUNK_CIPHERTEXT_BYTES)

    try:
        blob = base64.b64decode(frame.get("b64") or "")
    except Exception:
        await _send_to_peer(
            peer_uuid,
            build_storage_chunk_ack(deposit_id, chunk_idx, False, "bad_b64"),
        )
        return

    target = Path(pump["dir"]) / f"chunk_{chunk_idx:08d}.enc"
    try:
        await asyncio.to_thread(_write_bytes, target, blob)
    except Exception as exc:
        _log.warning("[storage:%s] write chunk %d failed: %s", deposit_id, chunk_idx, exc)
        await _send_to_peer(
            peer_uuid,
            build_storage_chunk_ack(deposit_id, chunk_idx, False, "io_error"),
        )
        return

    pump["received_idx"] = max(pump.get("received_idx", -1), chunk_idx)
    pump["last_chunk_at"] = time.time()
    # Throttled progress emission for the host's UI row.
    last_emit = pump.get("last_progress_emit", 0.0)
    total = int(pump.get("chunk_count") or 0)
    received_idx = pump["received_idx"]
    now = time.time()
    if (now - last_emit) >= 0.5 or (total and received_idx + 1 >= total):
        pump["last_progress_emit"] = now
        started = pump.get("started_at") or now
        bytes_received = (received_idx + 1) * CHUNK_CIPHERTEXT_BYTES
        elapsed = max(0.001, now - started)
        try:
            from nexus.core import events as _events

            _events.publish(
                "storage.transfer_progress",
                {
                    "deposit_id": deposit_id,
                    "role": "host",
                    "received_idx": received_idx,
                    "total": total,
                    "bytes_received": bytes_received,
                    "speed_bps": bytes_received / elapsed,
                },
            )
        except Exception:
            pass
    await _send_to_peer(peer_uuid, build_storage_chunk_ack(deposit_id, chunk_idx, True))


def _write_bytes(path: Path, blob: bytes) -> None:
    path.write_bytes(blob)


# ---------------------------------------------------------------------------
# Central frame dispatcher (used by worker_client / relay_client / websocket)
# ---------------------------------------------------------------------------

async def dispatch_storage_frame(peer_uuid: str, frame: dict) -> None:
    """Route an inbound ``storage_*`` frame to the right handler.

    The dispatcher is intentionally tolerant: unknown / malformed types
    are dropped silently rather than raising — defective peers must not
    be able to crash the WS receive loop.
    """
    ftype = str(frame.get("type") or "")
    if not ftype.startswith("storage_"):
        return

    if ftype == "storage_chunk":
        # Throttle is supplied by the lifecycle layer (5b.3); for now any
        # caller can pass one in via STATE.foreign_storage_throttle if set.
        throttle = getattr(STATE, "foreign_storage_throttle", None)
        await receive_chunk(peer_uuid, frame, throttle=throttle)
        return

    if ftype == "storage_chunk_ack":
        deposit_id = str(frame.get("deposit_id") or "")
        try:
            chunk_idx = int(frame.get("chunk_idx", -1))
        except (TypeError, ValueError):
            return
        record_chunk_ack(deposit_id, chunk_idx, bool(frame.get("ok")))
        return

    # The remaining frames (offer, offer_response, complete, eviction_*,
    # retrieve_*, delete_now, forward_init) carry workflow state that
    # belongs in a higher-level lifecycle handler. The lifecycle module
    # (5b.6) registers itself via ``set_storage_workflow_handler``; until
    # then, log-only.
    handler = getattr(STATE, "foreign_storage_workflow_handler", None)
    if handler is None:
        _log.debug("storage frame %s with no workflow handler yet", ftype)
        return
    try:
        await handler(peer_uuid, frame)
    except Exception as exc:
        _log.warning("workflow handler crashed on %s: %s", ftype, exc)


__all__ = [
    "CHUNK_PLAINTEXT_BYTES",
    "CHUNK_CIPHERTEXT_BYTES",
    "build_storage_offer",
    "build_storage_offer_response",
    "build_storage_offer_cancelled",
    "build_storage_pause",
    "build_storage_resume_request",
    "build_storage_resume_reply",
    "build_storage_missing_chunks",
    "build_storage_chunk",
    "build_storage_chunk_ack",
    "build_storage_complete",
    "build_storage_eviction_request",
    "build_storage_eviction_response",
    "build_storage_retrieve_open",
    "build_storage_retrieve_chunk",
    "build_storage_delete_now",
    "build_storage_forward_init",
    "build_storage_cloud_upload_progress",
    "build_storage_cloud_upload_complete",
    "build_storage_cloud_upload_failed",
    "transfer_deposit",
    "record_chunk_ack",
    "receive_chunk",
    "deposit_dir",
    "dispatch_storage_frame",
]
