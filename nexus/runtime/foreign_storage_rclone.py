"""Depositor-side rclone cloud-overflow for auto-rescue.

When a deposit can't be rescued to local disk (the disk is full), we stream
the host's ciphertext straight into ``rclone rcat`` so the bytes never have
to be staged locally. The user owns the cloud config — they set up any of
rclone's 70+ backends once (``rclone config``) and list one or more
``remote:path`` targets; we try them in order until one upload succeeds.

The host pushes chunks through the normal retrieve protocol; the workflow
handler hands each ciphertext chunk to the per-deposit queue registered
here (``STATE.foreign_storage_stream_queues``) instead of writing it to
disk. We re-order minor out-of-order arrivals and feed rclone's stdin with
real backpressure (``drain``).

The uploaded object is the concatenated ``.enc`` ciphertext — an encrypted
offsite copy; no password ever leaves this machine.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

from nexus.core import LOCAL_SETTINGS, STATE

_log = logging.getLogger("nexus.runtime.foreign_storage_rclone")

CHUNK_GET_TIMEOUT_S = 180   # max wait for the next chunk from the host
UPLOAD_FINISH_TIMEOUT_S = 600  # max wait for rclone to flush + exit


def rclone_available() -> bool:
    """True if the ``rclone`` binary is on PATH."""
    return shutil.which("rclone") is not None


async def overflow_rescue(
    deposit_id: str,
    host_uuid: str,
    filename: str,
    total_chunks: int,
    targets: list[str],
) -> None:
    """Try each rclone target in order; mark the row done on first success."""
    from sqlalchemy import select

    from nexus.storage import ForeignStorageDeposit, get_session
    from nexus.telemetry.audit import record_audit_event

    ok_target = ""
    for target in targets:
        try:
            if await _stream_one(deposit_id, host_uuid, total_chunks, target):
                ok_target = target
                break
        except Exception:
            _log.debug("rclone target %s failed", target, exc_info=True)

    if ok_target:
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
                row.status = "completed"
                row.cloud_dest = ok_target
                await db.commit()
        await record_audit_event(
            "storage.auto_rescue_cloud_stream_done",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            details=f"target={ok_target}",
        )
    else:
        # All targets failed — stop retrying and warn (files may be lost).
        STATE.foreign_storage_auto_rescue_seen[deposit_id] = "cloud_failed"
        await record_audit_event(
            "storage.auto_rescue_failed",
            actor=LOCAL_SETTINGS.get("node_uuid", ""),
            task_id=deposit_id,
            severity="warning",
            details=(
                f"file={filename} reason=cloud:rclone_all_targets_failed "
                "msg=files_may_be_lost"
            ),
        )


async def _stream_one(
    deposit_id: str, host_uuid: str, total_chunks: int, target: str
) -> bool:
    """Stream the deposit's ciphertext into ``rclone rcat target/<id>.enc``."""
    from nexus.networking.storage_pump import build_storage_retrieve_open
    from nexus.networking.tunnel import _send_to_peer

    obj = target.rstrip("/") + f"/{deposit_id}.enc"
    try:
        proc = await asyncio.create_subprocess_exec(
            "rclone", "rcat", obj,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return False

    q: asyncio.Queue = asyncio.Queue()
    STATE.foreign_storage_stream_queues[deposit_id] = q
    pending: dict[int, bytes] = {}
    next_idx = 0
    written = 0
    try:
        await _send_to_peer(
            host_uuid, build_storage_retrieve_open(deposit_id, 0, total_chunks - 1)
        )
        while written < total_chunks:
            idx, blob = await asyncio.wait_for(
                q.get(), timeout=CHUNK_GET_TIMEOUT_S
            )
            pending[idx] = blob
            # Flush every contiguous chunk we now hold, in order.
            while next_idx in pending:
                proc.stdin.write(pending.pop(next_idx))
                await proc.stdin.drain()
                next_idx += 1
                written += 1
        proc.stdin.close()
        rc = await asyncio.wait_for(proc.wait(), timeout=UPLOAD_FINISH_TIMEOUT_S)
        return rc == 0
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return False
    finally:
        STATE.foreign_storage_stream_queues.pop(deposit_id, None)


__all__ = ["rclone_available", "overflow_rescue"]
