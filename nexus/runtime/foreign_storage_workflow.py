"""Foreign-storage workflow handler.

Routed by :func:`nexus.networking.storage_pump.dispatch_storage_frame` for
every ``storage_*`` frame that isn't a chunk or chunk_ack. Splits by
role (host vs depositor) and frame type and updates DB rows + STATE
accordingly.

Installed once at app startup via :func:`install_workflow_handler`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS, STATE
from nexus.utils.time import iso_now, timestamp

_log = logging.getLogger("nexus.runtime.foreign_storage_workflow")


async def peer_signing_key(peer_id: str) -> str:
    """Return the per-pair signing key negotiated with *peer_id* ("" if none).

    Peer payload HMACs must use the pair key — the default
    ``.nexus_secret`` is node-local, so a signature made with it can never
    verify on a different node. Same lookup the task-result paths use.
    """
    from nexus.storage import Peer, get_session

    if not peer_id:
        return ""
    async with get_session() as db:
        row = (
            await db.execute(select(Peer).filter(Peer.ip == peer_id))
        ).scalar_one_or_none()
    return (row.signing_key or "") if row else ""


def _rmdir_cascade(dpath) -> None:
    """Remove an emptied chunk directory and any now-empty parent dirs.

    The host stores chunks under
    ``cache_dir(port)/foreign_storage/{depositor_uuid}/{deposit_id}/`` —
    after deleting the chunk files, both the deposit-id dir and the
    depositor-uuid dir become candidates for cleanup. ``rmdir`` raises
    if the directory still has entries, so we cap the walk at the
    ``foreign_storage`` root and stop on the first non-empty ancestor.
    """
    from pathlib import Path as _P
    try:
        d = _P(dpath)
    except Exception:
        return
    for _ in range(2):
        if not d.exists() or d.name == "foreign_storage":
            return
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


async def _handle_offer_rejected(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: a host rejected our offer (opted-out or full)."""
    from nexus.core import events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    reason = str(frame.get("reason") or "rejected")
    if not deposit_id:
        return
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "rejected"
            await db.commit()
    await record_audit_event(
        "storage.deposit_rejected_by_host",
        actor=peer_uuid,
        task_id=deposit_id,
        details=reason,
    )
    events.publish(
        "storage.offer_rejected",
        {"deposit_id": deposit_id, "host": peer_uuid, "reason": reason},
    )


async def _send_offer_rejection(peer_uuid: str, deposit_id: str, reason: str) -> None:
    """Tell the depositor we won't accept the offer (and why)."""
    from nexus.networking.tunnel import _send_to_peer

    try:
        await _send_to_peer(
            peer_uuid,
            {
                "type": "storage_offer_rejected",
                "deposit_id": deposit_id,
                "reason": reason,
            },
        )
    except Exception:
        # The depositor will fall back to its existing offer-timeout path.
        _log.debug("rejection frame delivery failed", exc_info=True)


async def _handle_offer(peer_uuid: str, frame: dict) -> None:
    """Host-side: persist a fresh deposit row with status=offered."""
    from nexus.runtime.foreign_storage_quota import (
        auto_opt_out_reason,
        effective_free_gb,
        is_accepting_offers,
    )
    from nexus.security.crypto import verify_signature
    from nexus.security.foreign_storage_terms import (
        DEFAULT_DEPOSITOR_TERMS,
    )
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return

    # Opted-out hosts reject before any DB work. Manual toggle and
    # auto-opt-out (disk too small to honour pledge) both surface here.
    if not is_accepting_offers():
        await _send_offer_rejection(peer_uuid, deposit_id, "opted_out")
        await record_audit_event(
            "storage.offer_rejected_opted_out",
            actor=peer_uuid,
            task_id=deposit_id,
        )
        return
    auto_reason = auto_opt_out_reason()
    if auto_reason:
        await _send_offer_rejection(peer_uuid, deposit_id, "auto_opted_out")
        await record_audit_event(
            "storage.offer_rejected_auto_opt_out",
            actor=peer_uuid,
            task_id=deposit_id,
            details=auto_reason,
        )
        return

    # Also reject if the offer wouldn't fit. The depositor's
    # capability filter normally prevents this, but stale capability
    # snapshots can still squeeze through — check authoritatively here.
    requested_gb = float(frame.get("total_bytes") or 0) / (1024 ** 3)
    free_gb = effective_free_gb()
    if requested_gb > free_gb:
        await _send_offer_rejection(peer_uuid, deposit_id, "insufficient_space")
        await record_audit_event(
            "storage.offer_rejected_no_space",
            actor=peer_uuid,
            task_id=deposit_id,
        )
        return

    # Reject offers without a depositor T&C signature. The depositor signs
    # with the per-pair key (see peer_signing_key); the default-key check is
    # kept as a fallback for deployments that share .nexus_secret.
    sig = str(frame.get("depositor_signature") or "")
    expected_tc_bytes = DEFAULT_DEPOSITOR_TERMS.encode("utf-8")
    sender_skey = await peer_signing_key(peer_uuid)
    if not (
        verify_signature(
            sig,
            "foreign_storage_terms",
            deposit_id,
            expected_tc_bytes,
            key=sender_skey,
        )
        or verify_signature(
            sig, "foreign_storage_terms", deposit_id, expected_tc_bytes
        )
    ):
        await record_audit_event(
            "storage.deposit_unsigned_terms",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="warning",
        )
        return

    salt = b""
    raw_salt = frame.get("salt_b64") or ""
    if raw_salt:
        try:
            salt = base64.b64decode(raw_salt)
        except Exception:
            salt = b""

    # Terminal statuses that mean the deposit is "over" — a fresh
    # offer with the same id is a legitimate re-send (depositor
    # clicked Resend in their Histories panel after we declined).
    # Active statuses are skipped to avoid racing the existing
    # transfer.
    _REOFFERABLE = {
        "declined", "withdrawn", "failed_in_transit", "purged", "rejected",
    }

    from datetime import datetime as _dt, timezone as _tz

    async with get_session() as db:
        existing = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status not in _REOFFERABLE:
                # Active row — ignore duplicate frame.
                return
            # Re-offer of a previously-terminated row: flip back to
            # ``offered`` and refresh metadata so the host sees the
            # current values in their Incoming panel.
            existing.status = "offered"
            existing.depositor_uuid = peer_uuid
            existing.total_bytes = int(frame.get("total_bytes") or 0)
            existing.chunk_count = int(frame.get("chunk_count") or 0)
            existing.transport = str(frame.get("transport") or "stream")
            existing.cloud_url = str(frame.get("cloud_url") or "")
            if salt:
                existing.salt = salt
            existing.ttl_days = int(frame.get("ttl_days") or 30)
            existing.created_at = _dt.now(_tz.utc).isoformat()
            existing.depositor_signature = sig
            existing.host_signature = ""
            existing.filename = str(frame.get("filename") or "")
            await db.commit()
        else:
            db.add(
                ForeignStorageDeposit(
                    deposit_id=deposit_id,
                    role="host",
                    depositor_uuid=peer_uuid,
                    host_uuid=LOCAL_SETTINGS.get("node_uuid", ""),
                    status="offered",
                    total_bytes=int(frame.get("total_bytes") or 0),
                    chunk_count=int(frame.get("chunk_count") or 0),
                    transport=str(frame.get("transport") or "stream"),
                    cloud_url=str(frame.get("cloud_url") or ""),
                    salt=salt,
                    # Host never stores the depositor's password_hint —
                    # privacy: the hint can leak structure of the password
                    # and is useless to the host (who holds ciphertext only).
                    # Defense-in-depth: build_storage_offer also omits the
                    # field from the wire frame, but we drop it here too in
                    # case an older depositor still sends it.
                    password_hint="",
                    ttl_days=int(frame.get("ttl_days") or 30),
                    created_at=_dt.now(_tz.utc).isoformat(),
                    depositor_signature=sig,
                    filename=str(frame.get("filename") or ""),
                )
            )
            await db.commit()

    await record_audit_event(
        "storage.deposit_offered",
        actor=peer_uuid,
        task_id=deposit_id,
    )
    # Surface the offer to the bell + Foreign Storage tab.
    from nexus.core import events

    events.publish(
        "storage.offer_incoming",
        {"deposit_id": deposit_id, "depositor": peer_uuid},
    )


async def _handle_offer_response(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: when host accepts, kick off the chunk pump.

    P2: in auto-mode (``status == "offering_multi"``), the first accept
    wins — we set ``host_uuid`` to the accepting peer, send cancels to
    the losers, and proceed. Late accepts on a row that's already
    transferring also get a cancel so the loser can clean up.
    """
    from nexus.networking.storage_pump import (
        build_storage_offer_cancelled,
        transfer_deposit,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    accepted = bool(frame.get("accepted"))
    if not deposit_id:
        return

    # P2: arbitration is needed when two candidates accept the same
    # auto-mode offer concurrently. The lock serializes the
    # read-status / set-status / commit window across the dispatcher.
    async with STATE.foreign_storage_auto_lock:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return
            current_status = row.status

            # P2: auto-mode acceptance arbitration.
            if current_status == "offering_multi":
                if not accepted:
                    # A candidate declined; remove them from the pool.
                    candidates = STATE.foreign_storage_auto_candidates.get(
                        deposit_id, []
                    )
                    candidates = [c for c in candidates if c != peer_uuid]
                    if candidates:
                        STATE.foreign_storage_auto_candidates[deposit_id] = candidates
                        await record_audit_event(
                            "storage.auto_offer_candidate_declined",
                            actor=peer_uuid,
                            task_id=deposit_id,
                            details=f"remaining={len(candidates)}",
                        )
                        return
                    # All candidates declined — give up.
                    STATE.foreign_storage_auto_candidates.pop(deposit_id, None)
                    STATE.foreign_storage_auto_started_at.pop(deposit_id, None)
                    row.status = "withdrawn"
                    await db.commit()
                    await record_audit_event(
                        "storage.auto_offer_all_declined",
                        actor=peer_uuid,
                        task_id=deposit_id,
                    )
                    from nexus.core import events as _events
                    _events.publish(
                        "storage.auto_offer_failed",
                        {
                            "deposit_id": deposit_id,
                            "reason": "all_declined",
                        },
                    )
                    return
                # ACCEPT — this candidate wins.
                row.status = "transferring"
                row.host_uuid = peer_uuid
                row.host_signature = str(frame.get("host_signature") or "")
                losers = [
                    c
                    for c in STATE.foreign_storage_auto_candidates.get(
                        deposit_id, []
                    )
                    if c != peer_uuid
                ]
                STATE.foreign_storage_auto_candidates.pop(deposit_id, None)
                STATE.foreign_storage_auto_started_at.pop(deposit_id, None)
                peer = peer_uuid
                await db.commit()
                # Best-effort cancel broadcast — failures don't abort the win.
                for loser in losers:
                    try:
                        await _send_to_peer(
                            loser,
                            build_storage_offer_cancelled(
                                deposit_id, reason="won_by_other"
                            ),
                        )
                    except Exception:
                        _log.debug(
                            "auto-mode cancel to %s failed", loser[:24],
                            exc_info=True,
                        )
                await record_audit_event(
                    "storage.auto_offer_won",
                    actor=peer_uuid,
                    task_id=deposit_id,
                    details=f"losers={len(losers)}",
                )
            elif current_status in {"transferring", "stored"} and accepted:
                # Late accept on an auto-mode deposit that already chose
                # a winner — tell this loser to drop the offer.
                try:
                    await _send_to_peer(
                        peer_uuid,
                        build_storage_offer_cancelled(
                            deposit_id, reason="won_by_other"
                        ),
                    )
                except Exception:
                    _log.debug(
                        "late auto-mode cancel to %s failed", peer_uuid[:24],
                        exc_info=True,
                    )
                return
            elif not accepted:
                row.status = "declined"
                row.host_signature = ""
                await db.commit()
                await record_audit_event(
                    "storage.deposit_declined",
                    actor=peer_uuid,
                    task_id=deposit_id,
                )
                return
            else:
                # Manual-mode accept (status == "offered"): original path.
                row.status = "transferring"
                row.host_signature = str(frame.get("host_signature") or "")
                peer = row.host_uuid
                await db.commit()

    from nexus.runtime import foreign_storage_keys

    cached = foreign_storage_keys.get_entry(deposit_id) or {}
    if not cached.get("key") or not cached.get("file_path"):
        _log.warning("offer_response %s but no cached key/file", deposit_id)
        return

    throttle = getattr(STATE, "foreign_storage_throttle", None)

    # Progress events + post-transfer temp-dir cleanup.
    from nexus.core import events as _events

    from nexus.networking.storage_pump import CHUNK_PLAINTEXT_BYTES as chunk_bytes
    progress_state = {"last_emit": 0.0, "started": time.time()}

    def _on_progress(sent_idx: int, total: int) -> None:
        # Throttle to ~2 updates per second to avoid flooding the WS bus.
        now = time.time()
        if (now - progress_state["last_emit"] < 0.5) and sent_idx < total:
            return
        progress_state["last_emit"] = now
        bytes_sent = sent_idx * chunk_bytes
        elapsed = max(0.001, now - progress_state["started"])
        speed_bps = bytes_sent / elapsed
        _events.publish(
            "storage.transfer_progress",
            {
                "deposit_id": deposit_id,
                "role": "depositor",
                "sent_idx": sent_idx,
                "total": total,
                "bytes_sent": bytes_sent,
                "speed_bps": speed_bps,
            },
        )

    async def _run_transfer():
        try:
            ok = await transfer_deposit(
                deposit_id,
                peer,
                cached["file_path"],
                bytes(cached["key"]),
                throttle=throttle,
                on_progress=_on_progress,
            )
            if ok:
                # Tell the host we're done so its row flips to
                # "stored", then flip our depositor row too. Without this,
                # both sides sit at "transferring" forever and the host's
                # advertised free space never reflects the new bytes.
                from nexus.networking.storage_pump import (
                    build_storage_complete,
                )
                from nexus.networking.tunnel import _send_to_peer
                from nexus.storage import (
                    ForeignStorageDeposit as _Dep,
                    get_session as _get_session,
                )

                await _send_to_peer(
                    peer,
                    build_storage_complete(deposit_id, depositor_signature=""),
                )
                async with _get_session() as db:
                    row = (
                        await db.execute(
                            select(_Dep).filter(
                                _Dep.deposit_id == deposit_id,
                                _Dep.role == "depositor",
                            )
                        )
                    ).scalar_one_or_none()
                    if row is not None:
                        row.status = "stored"
                        # Round 1: TTL countdown — set expiry now that the
                        # deposit landed. Mirror the host-side calc so the
                        # depositor's row has a real ttl_at to render.
                        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                        ttl_days = int(row.ttl_days or 0) or 30
                        row.ttl_at = (_dt.now(_tz.utc) + _td(days=ttl_days)).isoformat()
                        await db.commit()
                _events.publish(
                    "storage.deposit_completed",
                    {"deposit_id": deposit_id, "role": "depositor"},
                )
        finally:
            staged_dir = STATE.upload_temp_dirs_by_deposit.pop(deposit_id, None)
            if staged_dir:
                import shutil
                await asyncio.to_thread(
                    shutil.rmtree, staged_dir, ignore_errors=True
                )
                _log.info(
                    "[FOREIGN-STORAGE] cleaned up upload temp dir for %s",
                    deposit_id,
                )

    asyncio.create_task(
        _run_transfer(), name=f"nexus.foreign_storage.pump.{deposit_id}"
    )


async def _handle_complete(peer_uuid: str, frame: dict) -> None:
    """Host-side: mark deposit as stored; persist final depositor signature.

    P8.8: before flipping to ``stored``, scan the on-disk chunk dir for
    gaps. If any chunk_idx in ``[0..chunk_count)`` is missing, reply with
    ``storage_missing_chunks`` so the depositor can resend just those —
    the row stays at ``transferring`` until a follow-up complete with no
    gaps lands. If the host wiped everything (zero chunks but chunk_count
    > 0), surface a terminal failure to the depositor.
    """
    from nexus.networking.storage_pump import build_storage_missing_chunks
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        chunk_count = int(row.chunk_count or 0)
        depositor_uuid = row.depositor_uuid

    # P8.8: scan the chunk dir before declaring success.
    received = set(_scan_received_chunks(deposit_id, depositor_uuid))
    expected = set(range(chunk_count)) if chunk_count > 0 else set()
    missing = sorted(expected - received)

    if chunk_count > 0 and not received:
        # Host wiped everything (or never received any chunks). The
        # depositor cannot recover from this without re-deposit — surface
        # a terminal failure via the standard missing_chunks frame with
        # every index marked missing; the depositor handler caps retries
        # at fs_transit_max_retries and flips to failed_in_transit.
        await _send_to_peer(
            peer_uuid,
            build_storage_missing_chunks(deposit_id, list(range(chunk_count))),
        )
        await record_audit_event(
            "storage.complete_host_wiped",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="warning",
            details=f"expected={chunk_count}",
        )
        return

    if missing:
        # Gap detected: tell the depositor which indices to resend. Row
        # stays at transferring so the next storage_complete is required
        # before stored.
        await _send_to_peer(
            peer_uuid,
            build_storage_missing_chunks(deposit_id, missing),
        )
        await record_audit_event(
            "storage.complete_partial",
            actor=peer_uuid,
            task_id=deposit_id,
            details=f"missing_count={len(missing)}/{chunk_count}",
        )
        return

    # Happy path: every expected chunk is on disk.
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = "stored"
        row.depositor_signature = str(
            frame.get("depositor_signature_final") or row.depositor_signature
        )
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        ttl_days = int(row.ttl_days or 0) or 30
        row.ttl_at = (_dt.now(_tz.utc) + _td(days=ttl_days)).isoformat()
        await db.commit()
    # Batch C: arm the tripwire on the freshly-landed chunk dir so the
    # lifecycle pass can detect host-side tampering from here on.
    try:
        from nexus.networking.storage_pump import deposit_dir as _ddir
        from nexus.runtime import foreign_storage_tripwire
        foreign_storage_tripwire.record_baseline(_ddir(deposit_id, depositor_uuid))
    except Exception:
        _log.debug("tripwire baseline arm failed", exc_info=True)
    await record_audit_event(
        "storage.deposit_completed",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _handle_eviction_request(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: surface the eviction prompt to the UI."""
    from nexus.core import events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = "eviction_requested"
        row.eviction_requested_at = iso_now()
        # Mirror the host's configured countdown so the depositor's UI
        # renders the same numbers. Fall back to legacy 3-day default
        # for old hosts that don't send ``total_days``.
        try:
            total_days = int(frame.get("total_days") or 0)
        except Exception:
            total_days = 0
        if total_days > 0:
            row.eviction_total_days = total_days
        await db.commit()
    events.publish(
        "storage.eviction_requested",
        {"deposit_id": deposit_id, "host": peer_uuid},
    )
    await record_audit_event(
        "storage.eviction_requested",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _handle_eviction_response(peer_uuid: str, frame: dict) -> None:
    """Host-side: depositor's verdict on an eviction.

    actions: ``download | forward | let_go``.
    adds ``cloud`` — host streams the encrypted bundle to the
    depositor's external bucket.
    """
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    action = str(frame.get("action") or "")

    if action == "cloud":
        await _evict_to_cloud(peer_uuid, deposit_id, frame)
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if action == "let_go":
            row.status = "in_db_grace"
            row.db_grace_at = iso_now()
        await db.commit()
    await record_audit_event(
        f"storage.eviction_{action}",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _evict_to_cloud(
    peer_uuid: str, deposit_id: str, frame: dict
) -> None:
    """Host streams the encrypted bundle to the depositor's cloud.

    The host never sees plaintext: it ships the same on-disk ciphertext
    chunks straight to the provider. Credentials arrive transit-wrapped
    with a per-eviction HKDF key; we decrypt, hold them in memory only
    for the upload window, then zeroize in :keyword:`finally`.

    Failure path: emit ``storage_cloud_upload_failed``, audit, and fall
    back to the standard ``in_db_grace`` lifecycle so the depositor can
    still download via the classic retrieve path.
    """
    from pathlib import Path

    from nexus.networking.storage_pump import (
        build_storage_cloud_upload_complete,
        build_storage_cloud_upload_progress,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.cred_crypto import unwrap_from_transit
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.storage.cloud import PROVIDERS
    from nexus.storage.repositories import get_peer_by_ip
    from nexus.telemetry.audit import record_audit_event

    provider_name = str(frame.get("cloud_provider") or "")
    cloud_dest = str(frame.get("cloud_dest") or "")
    nonce_b64 = str(frame.get("cloud_eviction_nonce_b64") or "")
    creds_b64 = str(frame.get("cloud_credential_blob_b64") or "")

    creds_plain: bytearray = bytearray()
    try:
        if not (provider_name and nonce_b64 and creds_b64):
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "missing_cloud_fields"
            )
            return

        provider_cls = PROVIDERS.get(provider_name)
        if provider_cls is None:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "provider_unknown"
            )
            return

        # Look up the depositor's per-peer signing_key for the transit unwrap.
        depositor_peer = await get_peer_by_ip(peer_uuid)
        peer_signing_key = (
            depositor_peer.signing_key if depositor_peer else ""
        ) or ""
        if not peer_signing_key:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "no_peer_signing_key"
            )
            return

        try:
            eviction_nonce = base64.b64decode(nonce_b64)
            wrapped = base64.b64decode(creds_b64)
            creds_plain.extend(
                unwrap_from_transit(peer_signing_key, eviction_nonce, wrapped)
            )
        except Exception:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "creds_decrypt_failed"
            )
            return

        try:
            provider = provider_cls.from_credential_json(bytes(creds_plain))
        except Exception:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "creds_invalid"
            )
            return

        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "host",
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                await _cloud_eviction_fail(
                    peer_uuid, deposit_id, "deposit_not_found"
                )
                return
            row.status = "evicting_to_cloud"
            row.cloud_provider = provider_name
            row.cloud_dest = cloud_dest
            chunk_count = int(row.chunk_count or 0)
            total_bytes = int(row.total_bytes or 0)
            await db.commit()

        await record_audit_event(
            "storage.cloud_eviction_started",
            actor=peer_uuid,
            task_id=deposit_id,
            details=f"provider={provider_name}",
        )

        pump = STATE.foreign_storage_pumps.get(deposit_id) or {}
        dpath = Path(pump.get("dir") or "")
        if not dpath.exists():
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "host_chunks_missing"
            )
            return

        throttle = getattr(STATE, "foreign_storage_throttle", None)
        bytes_sent = 0
        progress_every = 16  # frames per progress update

        async def _throttle_acquire(n: int) -> None:
            if throttle is not None:
                await throttle.acquire(n)

        async def chunk_iter():
            nonlocal bytes_sent
            for idx in range(chunk_count):
                path = dpath / f"chunk_{idx:08d}.enc"
                if not path.exists():
                    continue
                blob = await asyncio.to_thread(path.read_bytes)
                yield blob
                bytes_sent += len(blob)
                if (idx + 1) % progress_every == 0:
                    await _send_to_peer(
                        peer_uuid,
                        build_storage_cloud_upload_progress(
                            deposit_id, bytes_sent, total_bytes
                        ),
                    )

        try:
            object_id = await provider.upload_stream(
                cloud_dest,
                f"{deposit_id}.enc",
                chunk_iter(),
                total_bytes,
                _throttle_acquire,
            )
        except NotImplementedError:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, "provider_unsupported"
            )
            return
        except Exception as exc:
            await _cloud_eviction_fail(
                peer_uuid, deposit_id, f"upload_failed:{type(exc).__name__}"
            )
            return

        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "host",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.cloud_object_id = object_id
                row.cloud_uploaded_at = int(time.time())
                row.status = "purged"
                row.purged_at = timestamp()
                await db.commit()

        # Wipe local ciphertext after the cloud copy is durable.
        try:
            from nexus.runtime import foreign_storage_tripwire
            foreign_storage_tripwire.clear_baseline(dpath)
        except Exception:
            pass
        for f in dpath.glob("chunk_*.enc"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        _rmdir_cascade(dpath)

        await _send_to_peer(
            peer_uuid,
            build_storage_cloud_upload_complete(deposit_id, object_id),
        )
        await record_audit_event(
            "storage.cloud_eviction_completed",
            actor=peer_uuid,
            task_id=deposit_id,
            details=f"object_id={object_id}",
        )
    finally:
        # Zero the credential bytes — small but cheap defence.
        for i in range(len(creds_plain)):
            creds_plain[i] = 0


async def _cloud_eviction_fail(
    peer_uuid: str, deposit_id: str, reason: str
) -> None:
    """Common failure path: tell the depositor + audit + fall back to db_grace."""
    from nexus.networking.storage_pump import build_storage_cloud_upload_failed
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.status = "in_db_grace"
            row.db_grace_at = iso_now()
            await db.commit()

    await _send_to_peer(
        peer_uuid, build_storage_cloud_upload_failed(deposit_id, reason)
    )
    severity = "warning" if reason == "provider_unsupported" else "error"
    await record_audit_event(
        "storage.cloud_eviction_failed",
        actor=peer_uuid,
        task_id=deposit_id,
        severity=severity,
        details=f"reason={reason}",
    )


async def _handle_cloud_upload_progress(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: surface upload progress to the UI."""
    from nexus.core import events

    deposit_id = str(frame.get("deposit_id") or "")
    events.publish(
        "storage.cloud_upload_progress",
        {
            "deposit_id": deposit_id,
            "bytes_sent": int(frame.get("bytes_sent") or 0),
            "total_bytes": int(frame.get("total_bytes") or 0),
        },
    )


async def _handle_cloud_upload_complete(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: persist cloud_object_id, mark deposit purged-on-host."""
    from nexus.core import events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    object_id = str(frame.get("cloud_object_id") or "")
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            row.cloud_object_id = object_id
            row.cloud_uploaded_at = int(time.time())
            row.status = "purged"
            row.purged_at = timestamp()
            await db.commit()
    events.publish(
        "storage.cloud_upload_complete",
        {"deposit_id": deposit_id, "cloud_object_id": object_id},
    )
    await record_audit_event(
        "storage.cloud_eviction_completed",
        actor=peer_uuid,
        task_id=deposit_id,
        details=f"object_id={object_id}",
    )


async def _handle_cloud_upload_failed(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: surface the failure; deposit stays in db_grace on host."""
    from nexus.core import events
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    reason = str(frame.get("reason") or "")
    events.publish(
        "storage.cloud_upload_failed",
        {"deposit_id": deposit_id, "reason": reason},
    )
    await record_audit_event(
        "storage.cloud_eviction_failed",
        actor=peer_uuid,
        task_id=deposit_id,
        severity="warning",
        details=f"reason={reason}",
    )


async def _handle_retrieve_open(peer_uuid: str, frame: dict) -> None:
    """Host-side: ship the requested chunks back to the depositor."""
    from pathlib import Path

    from nexus.core import events as _events
    from nexus.networking.storage_pump import (
        build_storage_retrieve_chunk,
        deposit_dir as _deposit_dir,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session

    deposit_id = str(frame.get("deposit_id") or "")
    first = int(frame.get("first_chunk_idx") or 0)
    last = int(frame.get("last_chunk_idx") or 0)
    if last < first:
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        depositor_uuid = row.depositor_uuid

    # Security F-014: only the deposit's own depositor may pull its chunks back.
    # The bytes are encrypted (a stranger couldn't decrypt them), but without
    # this check any authenticated peer that learns a deposit_id could force the
    # host to stream the ciphertext (resource-abuse DoS) and confirm the
    # deposit's existence/size. Mirrors the delete-handler authz.
    if peer_uuid != depositor_uuid:
        return

    # Pump["dir"] only exists after a fresh deposit landed in this
    # process; after a host restart the in-memory dict is empty even though
    # the ciphertext is still on disk. Derive the canonical directory from
    # storage_pump.deposit_dir() so retrieves and previews work post-restart.
    pump = STATE.foreign_storage_pumps.get(deposit_id) or {}
    dpath_str = pump.get("dir") or ""
    dpath = Path(dpath_str) if dpath_str else _deposit_dir(deposit_id, depositor_uuid)
    if not dpath.exists():
        return
    throttle = getattr(STATE, "foreign_storage_throttle", None)
    total_back = max(1, last - first + 1)
    sent_back = 0
    started_at = time.time()
    last_emit = 0.0
    bytes_back = 0
    for idx in range(first, last + 1):
        path = dpath / f"chunk_{idx:08d}.enc"
        if not path.exists():
            continue
        if throttle is not None:
            await throttle.acquire(path.stat().st_size)
        blob = await asyncio.to_thread(path.read_bytes)
        bytes_back += len(blob)
        await _send_to_peer(
            depositor_uuid,
            build_storage_retrieve_chunk(deposit_id, idx, blob),
        )
        sent_back += 1
        # Emit a host-side download-send progress tick at most twice a sec.
        now = time.time()
        if (now - last_emit) >= 0.5 or sent_back == total_back:
            last_emit = now
            elapsed = max(0.001, now - started_at)
            try:
                _events.publish(
                    "storage.transfer_progress",
                    {
                        "deposit_id": deposit_id,
                        "role": "dl_send",
                        "sent_idx": sent_back,
                        "total": total_back,
                        "bytes_sent": bytes_back,
                        "speed_bps": bytes_back / elapsed,
                    },
                )
            except Exception:
                pass


async def _handle_retrieve_chunk(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: decrypt + append chunk to ``save_to`` path."""
    from pathlib import Path

    from nexus.security.deposit_crypto import decrypt_chunk
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    chunk_idx = int(frame.get("chunk_idx") or 0)
    blob_b64 = frame.get("b64") or ""
    try:
        blob = base64.b64decode(blob_b64)
    except Exception:
        return

    from nexus.runtime import foreign_storage_keys, preview_pump

    # Hand the still-encrypted chunk to any preview Future
    # waiting on it. The preview path decrypts on its own (caches
    # plaintext keyed by chunk index).
    preview_pump.resolve_chunk(deposit_id, chunk_idx, blob)

    # Cloud-overflow: a streaming task is piping this deposit's ciphertext
    # straight into rclone (local disk full). Hand off the raw chunk and
    # skip all disk paths — nothing is staged locally.
    stream_q = STATE.foreign_storage_stream_queues.get(deposit_id)
    if stream_q is not None:
        try:
            stream_q.put_nowait((chunk_idx, blob))
        except Exception:
            pass
        return

    cached = foreign_storage_keys.get_entry(deposit_id) or {}

    # Encrypted-rescue mode: the deposit is locked (no key cached), so we
    # save the ciphertext chunk verbatim to a local bundle and decrypt it
    # later when the user supplies the password. No decryption here.
    raw_dir = cached.get("raw_dir")
    if raw_dir:
        await _save_raw_chunk(deposit_id, peer_uuid, chunk_idx, blob, raw_dir, cached)
        return

    key = cached.get("key")
    save_to = cached.get("save_to")
    if not key or not save_to:
        return

    try:
        plaintext = decrypt_chunk(bytes(key), blob, chunk_idx)
    except Exception:
        await record_audit_event(
            "storage.decryption_failed",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="error",
        )
        return

    target = Path(save_to)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if chunk_idx > 0 else "wb"
    await asyncio.to_thread(_append_bytes, target, mode, plaintext)

    # Download progress on the depositor side. We don't know the
    # total chunk count at chunk-receipt time without a DB read, so cache
    # it in foreign_storage_keys' entry on first chunk.
    entry = cached  # foreign_storage_keys.get_entry above
    total = int(entry.get("dl_total_chunks") or 0)
    if total <= 0:
        from nexus.storage import ForeignStorageDeposit, get_session
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                total = int(row.chunk_count or 0)
                entry["dl_total_chunks"] = total
                entry["dl_started_at"] = time.time()
                entry["dl_bytes"] = 0
                entry["dl_last_emit"] = 0.0
    if total > 0:
        entry["dl_bytes"] = int(entry.get("dl_bytes") or 0) + len(plaintext)
        now = time.time()
        last_emit = float(entry.get("dl_last_emit") or 0.0)
        finished = chunk_idx + 1 >= total
        if (now - last_emit) >= 0.5 or finished:
            entry["dl_last_emit"] = now
            started_at = float(entry.get("dl_started_at") or now)
            elapsed = max(0.001, now - started_at)
            from nexus.core import events as _ev
            try:
                _ev.publish(
                    "storage.transfer_progress",
                    {
                        "deposit_id": deposit_id,
                        "role": "dl_recv",
                        "received_idx": chunk_idx,
                        "total": total,
                        "bytes_received": int(entry["dl_bytes"]),
                        "speed_bps": int(entry["dl_bytes"]) / elapsed,
                    },
                )
                if finished:
                    _ev.publish(
                        "storage.deposit_completed",
                        {"deposit_id": deposit_id, "role": "downloaded"},
                    )
            except Exception:
                pass
        # Round 1: depositor opted into "delete after download" — fire
        # the standard delete-now frame so the host purges the bundle.
        if finished and bool(entry.get("delete_after_download")):
            try:
                from nexus.networking.storage_pump import build_storage_delete_now
                from nexus.networking.tunnel import _send_to_peer
                from nexus.security.crypto import sign_bytes
                from nexus.storage import ForeignStorageDeposit, get_session

                async with get_session() as db:
                    drow = (
                        await db.execute(
                            select(ForeignStorageDeposit).filter(
                                ForeignStorageDeposit.deposit_id == deposit_id,
                                ForeignStorageDeposit.role == "depositor",
                            )
                        )
                    ).scalar_one_or_none()
                    if drow is not None:
                        host_uuid = drow.host_uuid
                        drow.status = "withdrawn"
                        await db.commit()
                        sig = sign_bytes(
                            "foreign_storage_delete", deposit_id, b""
                        )
                        await _send_to_peer(
                            host_uuid,
                            build_storage_delete_now(deposit_id, sig),
                        )
                        await record_audit_event(
                            "storage.deposit_decommissioned",
                            actor=peer_uuid,
                            task_id=deposit_id,
                        )
            except Exception:
                pass
            entry["delete_after_download"] = False


async def _save_raw_chunk(
    deposit_id: str,
    peer_uuid: str,
    chunk_idx: int,
    blob: bytes,
    raw_dir: str,
    entry: dict,
) -> None:
    """Encrypted-rescue: persist a ciphertext chunk + drive progress/completion.

    Writes ``chunk_{idx:08d}.enc`` into ``raw_dir`` (mirrors the host's
    on-disk layout so the decrypt-later endpoint can reuse the same loop).
    On the last chunk flips the depositor row to ``rescued_encrypted`` and
    emits a completion event; the row keeps the salt + sealed manifest the
    user needs to decrypt with their password.
    """
    from pathlib import Path

    from nexus.core import events as _ev
    from nexus.runtime import foreign_storage_keys
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"chunk_{chunk_idx:08d}.enc"
    await asyncio.to_thread(_append_bytes, path, "wb", blob)

    total = int(entry.get("dl_total_chunks") or 0)
    if total <= 0:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                total = int(row.chunk_count or 0)
        entry["dl_total_chunks"] = total
        entry["dl_started_at"] = time.time()
        entry["dl_bytes"] = 0
        entry["dl_last_emit"] = 0.0

    entry["dl_bytes"] = int(entry.get("dl_bytes") or 0) + len(blob)
    now = time.time()
    last_emit = float(entry.get("dl_last_emit") or 0.0)
    finished = total > 0 and chunk_idx + 1 >= total
    if total > 0 and ((now - last_emit) >= 0.5 or finished):
        entry["dl_last_emit"] = now
        started = float(entry.get("dl_started_at") or now)
        elapsed = max(0.001, now - started)
        try:
            _ev.publish(
                "storage.transfer_progress",
                {
                    "deposit_id": deposit_id,
                    "role": "dl_recv",
                    "received_idx": chunk_idx,
                    "total": total,
                    "bytes_received": int(entry["dl_bytes"]),
                    "speed_bps": int(entry["dl_bytes"]) / elapsed,
                },
            )
        except Exception:
            pass

    if finished:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = "rescued_encrypted"
                await db.commit()
        foreign_storage_keys.drop(deposit_id)
        try:
            _ev.publish(
                "storage.deposit_completed",
                {"deposit_id": deposit_id, "role": "rescued_encrypted"},
            )
        except Exception:
            pass
        await record_audit_event(
            "storage.auto_rescue_encrypted_saved",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
        )


async def _handle_delete_now(peer_uuid: str, frame: dict) -> None:
    """Host-side: depositor wipe — flip to purged + clean disk + audit.

    Authorization: only the deposit's depositor (the trusted peer whose
    ``peer_uuid`` matches ``row.depositor_uuid``) may issue a
    ``storage_delete_now`` for this deposit. Any other trusted peer that
    tries to wipe a deposit they do not own is rejected and audited as
    ``storage.deposit_delete_unauthorized``.
    """
    from pathlib import Path

    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.depositor_uuid != peer_uuid:
            await record_audit_event(
                "storage.deposit_delete_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner={row.depositor_uuid}",
            )
            return
        row.status = "purged"
        row.purged_at = timestamp()
        await db.commit()

    pump = STATE.foreign_storage_pumps.pop(deposit_id, None) or {}
    STATE.foreign_storage_tripwire_fired.pop(deposit_id, None)
    dpath = Path(pump.get("dir") or "")
    if dpath.exists():
        try:
            from nexus.runtime import foreign_storage_tripwire
            foreign_storage_tripwire.clear_baseline(dpath)
        except Exception:
            pass
        for f in dpath.glob("chunk_*.enc"):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        _rmdir_cascade(dpath)
    await record_audit_event(
        "storage.deposit_purged",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _handle_pause(peer_uuid: str, frame: dict) -> None:
    """P8: receiver-side handler for a `storage_pause` notification.

    Both depositors and hosts emit this on graceful shutdown / pump
    failure. The receiver flips the row to ``paused_<reason>`` so the
    lifecycle pass classifies retries correctly.

    Authorization: only one of {depositor, host} on a deposit may pause
    it (matching the delete_now ownership pattern). Anything else is
    audited and dropped.
    """
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    raw_reason = str(frame.get("reason") or "silent")
    if not deposit_id:
        return
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        # The sender must be the other party on this deposit.
        valid_senders = {row.depositor_uuid, row.host_uuid}
        if peer_uuid not in valid_senders:
            await record_audit_event(
                "storage.pause_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner_set={list(valid_senders)}",
            )
            return
        if row.status in {"stored", "purged", "withdrawn", "failed_in_transit"}:
            # Terminal or no-longer-in-flight — pause is meaningless.
            return
        row.status = f"paused_{raw_reason}"
        row.pause_reason = raw_reason
        await db.commit()
    await record_audit_event(
        "storage.transit_paused",
        actor=peer_uuid,
        task_id=deposit_id,
        details=f"reason={raw_reason}",
    )


async def _handle_resume_request(peer_uuid: str, frame: dict) -> None:
    """P8: host-side handler — depositor wants to know what we have on disk.

    Scans the deposit's chunk directory, replies with the list of
    chunk_idx already present. Sender must be the deposit's depositor.

    P8.8: a host restart wipes ``STATE.foreign_storage_pumps`` even though
    the ciphertext is still on disk. Rebuild the pump entry from the DB
    + on-disk dir before scanning so subsequent receive_chunk handlers
    don't drop frames with ``no_pump``.
    """
    import time as _time
    from nexus.networking.storage_pump import (
        build_storage_resume_reply,
        deposit_dir,
    )
    from nexus.networking.tunnel import _send_to_peer
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.depositor_uuid != peer_uuid:
            await record_audit_event(
                "storage.resume_request_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner={row.depositor_uuid}",
            )
            return
        depositor_uuid = row.depositor_uuid
        chunk_count = int(row.chunk_count or 0)
        total_bytes = int(row.total_bytes or 0)

    # P8.8: rebuild pump entry if missing (host process was restarted).
    pump = STATE.foreign_storage_pumps.get(deposit_id)
    if pump is None:
        dpath = deposit_dir(deposit_id, depositor_uuid)
        received_idx = -1
        try:
            existing = _scan_received_chunks(deposit_id, depositor_uuid)
            if existing:
                received_idx = max(existing)
        except Exception:
            existing = []
        STATE.foreign_storage_pumps[deposit_id] = {
            "role": "host",
            "peer_uuid": peer_uuid,
            "deposit_id": deposit_id,
            "total_bytes": total_bytes,
            "chunk_count": chunk_count,
            "dir": str(dpath),
            "received_idx": received_idx,
            "last_chunk_at": _time.time(),
            "status": "transferring",
            "started_at": _time.time(),
            "last_progress_emit": 0.0,
        }

    received = _scan_received_chunks(deposit_id, depositor_uuid)
    await _send_to_peer(
        peer_uuid, build_storage_resume_reply(deposit_id, received)
    )
    await record_audit_event(
        "storage.resume_request_served",
        actor=peer_uuid,
        task_id=deposit_id,
        details=f"received_count={len(received)}",
    )


def _scan_received_chunks(deposit_id: str, depositor_uuid: str) -> list[int]:
    """Walk the host's deposit dir and return the chunk_idx values on disk.

    Filenames are ``chunk_{idx:08d}.enc``; anything that doesn't match
    that pattern is skipped. Synchronous on purpose — the caller is
    already on the workflow loop and the typical deposit has at most a
    few tens of thousands of files (16 KB scan budget for 16k entries).
    """
    from pathlib import Path
    from nexus.networking.storage_pump import deposit_dir

    try:
        dpath = deposit_dir(deposit_id, depositor_uuid)
    except Exception:
        return []
    if not dpath.exists():
        return []
    out: list[int] = []
    for f in dpath.glob("chunk_*.enc"):
        try:
            idx = int(f.stem.split("_", 1)[1])
            out.append(idx)
        except (ValueError, IndexError):
            continue
    out.sort()
    return out


async def _handle_resume_reply(peer_uuid: str, frame: dict) -> None:
    """P8: depositor-side handler — host told us what they actually have.

    Restarts the chunk pump, iterating only the missing indices. The
    re-launch is fire-and-forget; the pump itself manages STATE +
    persistence. Sender must be the deposit's host.
    """
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    received_raw = frame.get("received_chunks") or []
    if not deposit_id or not isinstance(received_raw, list):
        return
    received = sorted({int(i) for i in received_raw if isinstance(i, (int, float))})

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.host_uuid != peer_uuid:
            await record_audit_event(
                "storage.resume_reply_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} expected={row.host_uuid}",
            )
            return
        total_chunks = int(row.chunk_count or 0)
        missing = [i for i in range(total_chunks) if i not in set(received)]
        if not missing:
            # Host has everything — flip straight to stored.
            row.status = "stored"
            await db.commit()
            await record_audit_event(
                "storage.resume_all_chunks_present",
                actor=peer_uuid,
                task_id=deposit_id,
            )
            return
        row.status = "transferring"
        row.transferred_chunks = max(received) if received else 0
        await db.commit()
        host_peer = row.host_uuid

    from nexus.runtime import foreign_storage_keys

    cached = foreign_storage_keys.get_entry(deposit_id) or {}
    if not cached.get("key") or not cached.get("file_path"):
        # No key cached — caller (lifecycle pass) should not have
        # initiated a resume in this state. Defensive no-op + audit.
        await record_audit_event(
            "storage.resume_reply_no_key",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="warning",
        )
        return

    # Hand off to the resume pump (defined below alongside the regular
    # transfer launcher). Fire-and-forget; pump owns its own STATE.
    await _launch_resume_pump(
        deposit_id, host_peer, cached["file_path"], bytes(cached["key"]), missing
    )
    await record_audit_event(
        "storage.resume_started",
        actor=peer_uuid,
        task_id=deposit_id,
        details=f"missing_count={len(missing)}",
    )


async def _launch_resume_pump(
    deposit_id: str,
    peer: str,
    file_path: str,
    derived_key: bytes,
    chunk_indices: list[int],
) -> None:
    """P8: spawn the resume-only chunk pump for *chunk_indices*.

    Defined here as a thin shim so ``_handle_resume_reply`` doesn't have
    to know about ``transfer_deposit``'s call surface. The real
    implementation lives in storage_pump (P8.3 adds the
    ``chunk_indices_to_send`` parameter).
    """
    from nexus.networking.storage_pump import transfer_deposit

    throttle = getattr(STATE, "foreign_storage_throttle", None)

    async def _run():
        try:
            await transfer_deposit(
                deposit_id,
                peer,
                file_path,
                derived_key,
                throttle=throttle,
                chunk_indices_to_send=chunk_indices,
            )
        except Exception:
            _log.exception("[storage:%s] resume pump crashed", deposit_id)

    asyncio.create_task(
        _run(), name=f"nexus.foreign_storage.resume.{deposit_id}"
    )


async def _handle_missing_chunks(peer_uuid: str, frame: dict) -> None:
    """P8.8: depositor-side — host scanned its dir and reports missing chunks.

    Re-launches the pump with ``chunk_indices_to_send=missing`` so only
    those indices are re-encrypted and re-sent. Bounded by
    ``fs_transit_max_retries`` rounds — once we've hit the cap the
    deposit transitions to ``failed_in_transit`` (the user must redo
    the deposit; the host is dropping chunks faster than we can resend).

    Authorization: only the deposit's host may send this frame.
    """
    from nexus.networking.tunnel import _send_to_peer
    from nexus.networking.storage_pump import build_storage_delete_now
    from nexus.security.crypto import sign_bytes
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    missing_raw = frame.get("missing") or []
    if not deposit_id or not isinstance(missing_raw, list):
        return
    missing = sorted(
        {int(i) for i in missing_raw if isinstance(i, (int, float))}
    )

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.host_uuid != peer_uuid:
            await record_audit_event(
                "storage.missing_chunks_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} expected={row.host_uuid}",
            )
            return
        if row.status in {"stored", "withdrawn", "purged", "failed_in_transit"}:
            return
        host_peer = row.host_uuid

    # Empty missing list = host has everything → flip to stored.
    if not missing:
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = "stored"
                await db.commit()
        STATE.foreign_storage_missing_rounds.pop(deposit_id, None)
        return

    # Bounded retries — defeat a host that loses chunks every round.
    # The deposit row may carry its own cap (0 = node default).
    max_retries = max(
        1, int(LOCAL_SETTINGS.get("fs_transit_max_retries", 5) or 5)
    )
    try:
        async with get_session() as db:
            _ovr_row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            _ovr = int(getattr(_ovr_row, "transit_retries", 0) or 0) if _ovr_row else 0
            if _ovr:
                max_retries = max(1, min(20, _ovr))
    except Exception:
        pass
    rounds = STATE.foreign_storage_missing_rounds.get(deposit_id, 0) + 1
    STATE.foreign_storage_missing_rounds[deposit_id] = rounds
    if rounds > max_retries:
        # Give up. Tell host to wipe, flip our row terminal, audit.
        async with get_session() as db:
            row = (
                await db.execute(
                    select(ForeignStorageDeposit).filter(
                        ForeignStorageDeposit.deposit_id == deposit_id,
                        ForeignStorageDeposit.role == "depositor",
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.status = "failed_in_transit"
                row.pause_reason = "missing_chunks_exhausted"
                await db.commit()
        try:
            sig = sign_bytes("foreign_storage_delete", deposit_id, b"")
            await _send_to_peer(
                host_peer, build_storage_delete_now(deposit_id, sig)
            )
        except Exception:
            pass
        from nexus.runtime import foreign_storage_keys
        foreign_storage_keys.drop(deposit_id)
        STATE.foreign_storage_missing_rounds.pop(deposit_id, None)
        await record_audit_event(
            "storage.transit_failed",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="warning",
            details=f"reason=missing_chunks_exhausted rounds={rounds}",
        )
        return

    # Re-launch the pump to resend only what the host says it's missing.
    from nexus.runtime import foreign_storage_keys

    cached = foreign_storage_keys.get_entry(deposit_id) or {}
    if not cached.get("key") or not cached.get("file_path"):
        await record_audit_event(
            "storage.missing_chunks_no_key",
            actor=peer_uuid,
            task_id=deposit_id,
            severity="warning",
        )
        return

    await _launch_missing_chunks_pump(
        deposit_id,
        host_peer,
        cached["file_path"],
        bytes(cached["key"]),
        missing,
    )
    await record_audit_event(
        "storage.missing_chunks_resend",
        actor=peer_uuid,
        task_id=deposit_id,
        details=f"missing_count={len(missing)} round={rounds}",
    )


async def _launch_missing_chunks_pump(
    deposit_id: str,
    peer: str,
    file_path: str,
    derived_key: bytes,
    chunk_indices: list[int],
) -> None:
    """P8.8: spawn the resend-only pump and re-emit ``storage_complete`` on success.

    Unlike a generic resume (where the host hasn't seen ``storage_complete``
    yet), missing-chunks rounds always end with a fresh complete frame so
    the host re-scans and either flips to ``stored`` or emits another
    missing list.
    """
    from nexus.networking.storage_pump import (
        build_storage_complete,
        transfer_deposit,
    )
    from nexus.networking.tunnel import _send_to_peer

    throttle = getattr(STATE, "foreign_storage_throttle", None)

    async def _run():
        try:
            ok = await transfer_deposit(
                deposit_id,
                peer,
                file_path,
                derived_key,
                throttle=throttle,
                chunk_indices_to_send=chunk_indices,
            )
            if ok:
                try:
                    await _send_to_peer(
                        peer,
                        build_storage_complete(
                            deposit_id, depositor_signature=""
                        ),
                    )
                except Exception:
                    pass
        except Exception:
            _log.exception(
                "[storage:%s] missing-chunks pump crashed", deposit_id
            )

    asyncio.create_task(
        _run(), name=f"nexus.foreign_storage.missing.{deposit_id}"
    )


async def _handle_offer_cancelled(peer_uuid: str, frame: dict) -> None:
    """Candidate-side: depositor withdrew the offer (auto-mode loser or timeout).

    Authorization mirrors ``_handle_delete_now``: only the deposit's
    original depositor may cancel. We drop the host-side row + clear it
    from the bell so the user isn't left with a stale offer to act on.
    """
    from nexus.core import events as _events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    reason = str(frame.get("reason") or "")
    if not deposit_id:
        return
    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.depositor_uuid != peer_uuid:
            await record_audit_event(
                "storage.offer_cancel_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner={row.depositor_uuid}",
            )
            return
        # Only meaningful while the offer is still pending or just-accepted —
        # if chunks are already in flight the depositor should use the
        # eviction path instead. We still no-op gracefully.
        if row.status not in {"offered", "accepted"}:
            return
        row.status = "withdrawn"
        await db.commit()
    await record_audit_event(
        "storage.offer_cancelled_by_depositor",
        actor=peer_uuid,
        task_id=deposit_id,
        details=reason,
    )
    _events.publish(
        "storage.offer_cancelled",
        {"deposit_id": deposit_id, "depositor": peer_uuid, "reason": reason},
    )


async def _handle_view_grant(peer_uuid: str, frame: dict) -> None:
    """Depositor wants to share viewing rights for ``deposit_id``.

    The host always accepts view grants by design — there is no host
    opt-in toggle. The depositor's AES key (transit-wrapped via
    :func:`derive_view_grant_wrap_key`) is unwrapped and cached in
    :mod:`foreign_storage_keys`. The sealed manifest piggy-backs on
    the same frame so the host can render preview metadata without
    round-tripping the depositor.
    """
    from nexus.networking.tunnel import _send_to_peer
    from nexus.runtime import foreign_storage_keys
    from nexus.security.cred_crypto import unwrap_view_grant_from_transit
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.storage.repositories import get_peer_by_ip
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return

    nonce_b64 = str(frame.get("grant_nonce_b64") or "")
    blob_b64 = str(frame.get("deposit_key_blob_b64") or "")
    sealed_b64 = str(frame.get("sealed_manifest_b64") or "")
    if not (nonce_b64 and blob_b64):
        await _send_to_peer(peer_uuid, {
            "type": "storage_view_grant_rejected",
            "deposit_id": deposit_id,
            "reason": "missing_fields",
        })
        return

    sender = await get_peer_by_ip(peer_uuid)
    sender_signing_key = (sender.signing_key if sender else "") or ""
    if not sender_signing_key:
        await _send_to_peer(peer_uuid, {
            "type": "storage_view_grant_rejected",
            "deposit_id": deposit_id,
            "reason": "no_peer_signing_key",
        })
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            await _send_to_peer(peer_uuid, {
                "type": "storage_view_grant_rejected",
                "deposit_id": deposit_id,
                "reason": "unknown_deposit",
            })
            return
        if row.depositor_uuid != peer_uuid:
            await record_audit_event(
                "storage.deposit_view_revoke_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner={row.depositor_uuid}",
            )
            return

        try:
            grant_nonce = base64.b64decode(nonce_b64)
            wrapped = base64.b64decode(blob_b64)
            deposit_key = unwrap_view_grant_from_transit(
                sender_signing_key, grant_nonce, wrapped
            )
        except Exception:
            await _send_to_peer(peer_uuid, {
                "type": "storage_view_grant_rejected",
                "deposit_id": deposit_id,
                "reason": "decrypt_failed",
            })
            return

        if sealed_b64:
            try:
                row.encrypted_manifest = base64.b64decode(sealed_b64)
            except Exception:
                pass
        row.host_view_granted_at = int(time.time())
        await db.commit()

    foreign_storage_keys.store(deposit_id, deposit_key)
    await record_audit_event(
        "storage.view_grant_accepted",
        actor=peer_uuid,
        task_id=deposit_id,
    )
    # Acknowledge so the depositor's UI can flip the row from
    # "Share pending" to "Shared". Without this echo the depositor's
    # optimistic UI was lying when the host was offline / opted-out.
    await _send_to_peer(peer_uuid, {
        "type": "storage_view_grant_accepted",
        "deposit_id": deposit_id,
    })


async def _handle_view_grant_accepted(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: host successfully cached the view-grant key.

    Stamps ``host_view_granted_at`` on the depositor's row, which is what
    drives the "Shared" badge in the UI. Pre-ack the row carries a
    sentinel of ``-1`` (set by the grant_view endpoint) so the UI can
    render "Share pending" until this frame lands.
    """
    from nexus.core import events as _events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.host_view_granted_at = int(time.time())
        await db.commit()

    await record_audit_event(
        "storage.view_grant_acked_by_host",
        actor=peer_uuid,
        task_id=deposit_id,
    )
    _events.publish(
        "storage.view_grant_accepted",
        {"deposit_id": deposit_id, "host_uuid": peer_uuid},
    )


async def _handle_eviction_cancelled(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: the host cancelled their pending eviction.

    Reverts the row from ``eviction_requested`` back to ``stored`` and
    clears the countdown so the UI hides the eviction notice.
    """
    from nexus.core import events as _events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        # Only revert if the depositor was actually mid-eviction; a
        # stray cancel for an already-stored row is a no-op.
        if row.status not in {"eviction_requested", "in_db_grace"}:
            return
        row.status = "stored"
        row.eviction_requested_at = ""
        row.db_grace_at = ""
        row.eviction_total_days = 0
        await db.commit()

    _events.publish(
        "storage.eviction_cancelled",
        {"deposit_id": deposit_id, "host_uuid": peer_uuid},
    )
    await record_audit_event(
        "storage.eviction_cancelled",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _handle_view_revoke(peer_uuid: str, frame: dict) -> None:
    """Depositor revokes the host's viewing right for ``deposit_id``.

    Drops the cached AES key (zeroized in place) and clears the
    timestamp on the host's row. Only the deposit's depositor may
    revoke; other peers are audited and ignored.
    """
    from nexus.runtime import foreign_storage_keys
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "host",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.depositor_uuid != peer_uuid:
            await record_audit_event(
                "storage.deposit_view_revoke_unauthorized",
                actor=peer_uuid,
                task_id=deposit_id,
                severity="warning",
                details=f"sender={peer_uuid} owner={row.depositor_uuid}",
            )
            return
        row.host_view_granted_at = 0
        await db.commit()

    foreign_storage_keys.drop(deposit_id)
    await record_audit_event(
        "storage.view_grant_revoked",
        actor=peer_uuid,
        task_id=deposit_id,
    )


async def _handle_view_grant_rejected(peer_uuid: str, frame: dict) -> None:
    """Depositor-side: the host refused our view-grant frame.

    Reasons we currently emit on the host side:

    * ``disabled`` — host's ``allow_view_grants`` setting is off.
    * ``missing_fields`` / ``decrypt_failed`` / ``no_peer_signing_key``
      / ``unknown_deposit`` — protocol-level failures.

    The depositor's ``/foreign_storage/grant_view`` endpoint optimistically
    stamps ``host_view_granted_at`` after ``_send_to_peer`` returns True
    (i.e. the frame was put on the wire), so we have to roll that flag
    back here when the host bounces the grant. Without this rollback the
    depositor's UI shows "Shared" while the host shows nothing — exactly
    the confusion the user hit.
    """
    from nexus.core import events as _events
    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    reason = str(frame.get("reason") or "rejected")
    if not deposit_id:
        return

    async with get_session() as db:
        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.host_view_granted_at:
            row.host_view_granted_at = 0
            await db.commit()

    await record_audit_event(
        "storage.view_grant_rejected_by_host",
        actor=peer_uuid,
        task_id=deposit_id,
        severity="warning",
        details=f"reason={reason}",
    )
    _events.publish(
        "storage.view_grant_rejected",
        {
            "deposit_id": deposit_id,
            "host_uuid": peer_uuid,
            "reason": reason,
        },
    )


async def _handle_tripwire_fired(peer_uuid: str, frame: dict) -> None:
    """Batch C: depositor-side receipt of a host tampering alarm.

    The host's lifecycle pass detected chunk metadata drift and pushed
    a ``storage_tripwire_fired`` frame here. We surface it as both an
    audit row (durable) and a bus event so the depositor's UI can show
    a toast next to the bell.
    """
    from nexus.core import events as _events
    from nexus.telemetry.audit import record_audit_event

    deposit_id = str(frame.get("deposit_id") or "")
    if not deposit_id:
        return
    changed = frame.get("changed_chunks") or []
    if not isinstance(changed, list):
        changed = []
    await record_audit_event(
        "storage.unauthorized_access_detected",
        actor=peer_uuid,
        task_id=deposit_id,
        severity="warning",
        details=f"changed={','.join(str(c) for c in changed[:8])}",
    )
    _events.publish(
        "storage.unauthorized_access_detected",
        {
            "deposit_id": deposit_id,
            "host_uuid": peer_uuid,
            "changed_chunks": list(changed)[:8],
        },
    )


async def workflow_handler(peer_uuid: str, frame: dict) -> None:
    """Central router; called by ``dispatch_storage_frame``."""
    ftype = str(frame.get("type") or "")
    handlers = {
        "storage_offer": _handle_offer,
        "storage_offer_response": _handle_offer_response,
        "storage_complete": _handle_complete,
        "storage_eviction_request": _handle_eviction_request,
        "storage_eviction_response": _handle_eviction_response,
        "storage_retrieve_open": _handle_retrieve_open,
        "storage_retrieve_chunk": _handle_retrieve_chunk,
        "storage_delete_now": _handle_delete_now,
        # P2 auto-mode fan-out: candidate-side reception of a loser/timeout cancel.
        "storage_offer_cancelled": _handle_offer_cancelled,
        # P8 pause/resume: bidirectional pause notification + depositor-driven resume.
        "storage_pause": _handle_pause,
        "storage_resume_request": _handle_resume_request,
        "storage_resume_reply": _handle_resume_reply,
        # P8.8 chunk-loss recovery: host detected gaps on storage_complete.
        "storage_missing_chunks": _handle_missing_chunks,
        # Cloud-eviction tier (depositor-side reception).
        "storage_cloud_upload_progress": _handle_cloud_upload_progress,
        "storage_cloud_upload_complete": _handle_cloud_upload_complete,
        "storage_cloud_upload_failed": _handle_cloud_upload_failed,
        # Host-view grants (host-side reception).
        "storage_view_grant": _handle_view_grant,
        "storage_view_revoke": _handle_view_revoke,
        # Depositor-side: host acknowledged our grant frame after
        # caching the key. Stamps the grant timestamp so the UI flips
        # from "Share pending" to "Shared".
        "storage_view_grant_accepted": _handle_view_grant_accepted,
        # Depositor-side: host bounced our grant frame (e.g. their
        # ``allow_view_grants`` was off). Rolls back the optimistic
        # grant flag + raises a UI toast.
        "storage_view_grant_rejected": _handle_view_grant_rejected,
        # Opt-out / capacity rejection (depositor-side reception).
        "storage_offer_rejected": _handle_offer_rejected,
        # Cancel-evict: host changed their mind on an accidental Evict
        # click. Depositor reverts the row back to ``stored``.
        "storage_eviction_cancelled": _handle_eviction_cancelled,
        # Batch C tripwire (depositor-side reception of host alarm).
        "storage_tripwire_fired": _handle_tripwire_fired,
    }
    handler = handlers.get(ftype)
    if handler is None:
        _log.debug("workflow drop unknown frame %s", ftype)
        return
    await handler(peer_uuid, frame)


def _append_bytes(path, mode, data) -> None:
    with open(path, mode) as fh:
        fh.write(data)


def install_workflow_handler() -> None:
    """Hook the workflow handler onto STATE for storage_pump to find."""
    setattr(STATE, "foreign_storage_workflow_handler", workflow_handler)


__all__ = [
    "workflow_handler",
    "install_workflow_handler",
]
