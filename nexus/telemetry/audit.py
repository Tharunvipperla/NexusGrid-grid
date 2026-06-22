"""Append-only audit log.

Extracted from Phase-1/node_modified.py (lines 1875-1894).

Every significant event the node takes or observes is persisted as an
``AuditEvent`` row. Rows are never updated or deleted except by the
retention pruner (``audit_retention_days`` setting).

Action vocabulary
-----------------

Kept deliberately open: any string that a reviewer would recognise is fine.
Existing Phase-1 actions include ``task.dispatched``, ``task.completed``,
``task.disrupted``, ``peer.joined``, ``peer.revoked``, ``settings.changed``.
When adding a new action, use dotted ``domain.verb`` form and document it
in the PR that introduces it.

(depositor in-browser preview) actions:

* ``storage.deposit_unlocked`` — depositor cached a session key after
  password verification.
* ``storage.deposit_locked`` — explicit lock endpoint dropped the key.
* ``storage.deposit_locked_idle`` — scheduler GC dropped a key past
  its idle TTL.
* ``storage.preview_served`` — one row per HTTP preview request.
* ``storage.preview_decrypt_failed`` — the preview stream raised while
  decrypting (corrupted ciphertext, wrong key, etc.).

(cloud task-data sources) actions:

* ``task.data_source_attached`` — a task manifest references a cloud
  ``data_sources``/``workspace_source`` entry at submit time.
* ``task.data_source_transmitted`` — at dispatch the master unwrapped
  the at-rest credential and transit-wrapped it for one worker.
* ``task.data_source_fetched`` — worker successfully downloaded a
  cloud folder into the workspace.
* ``task.data_source_fetch_failed`` — worker download failed; bundle
  aborted before execution.
* ``task.data_terms_accepted`` — depositor accepted the IP/copyright
  terms required to dispatch any cloud-data task.

(per-deposit host-view grant) actions:

* ``storage.view_grant_sent`` — depositor transmitted a wrapped key to
  the host as part of a view-grant frame.
* ``storage.view_grant_accepted`` — host's ``allow_view_grants`` setting
  was on; the host cached the unwrapped key.
* ``storage.view_grant_rejected_disabled`` — host received a grant
  while ``allow_view_grants`` was off; the frame was rejected and the
  key never cached.
* ``storage.view_grant_revoked`` — host dropped the cached key after a
  revoke frame; depositor logs the same action when it sends one. Note
  this only zeroes the RAM key — any plaintext already materialized to
  disk via ``storage.view_grant_materialized`` is unaffected.
* ``storage.deposit_view_revoke_unauthorized`` — a peer that doesn't
  own the deposit attempted to revoke; the request was ignored.
* ``storage.view_grant_materialized`` — host clicked Open on a granted
  deposit; the chunks were decrypted and the plaintext file is now on
  disk at ``host_view_decrypted_dir``. This is a one-way action: the
  depositor's revoke cannot undo it.
* ``storage.view_grant_disk_deleted`` — host explicitly deleted the
  on-disk plaintext copy (ciphertext + cached key are unaffected; host
  can re-materialize via the same endpoint later).
* ``storage.view_grant_rejected_by_host`` — depositor-side audit row
  emitted when the host bounced our grant frame (e.g. their
  ``allow_view_grants`` setting was off, or a protocol-level failure
  like wrong signing key). The depositor's optimistic grant flag is
  rolled back; UI surfaces a toast with the rejection reason.
"""

from __future__ import annotations

import logging
import uuid

from nexus.storage.database import get_session
from nexus.storage.models import AuditEvent
from nexus.utils.time import now_epoch

_log = logging.getLogger("nexus.telemetry.audit")


async def record_audit_event(
    action: str,
    actor: str,
    task_id: str = "",
    details: str = "",
    severity: str = "info",
) -> None:
    """Persist an audit event. Never raises — audit must not break callers."""
    try:
        async with get_session() as db:
            db.add(
                AuditEvent(
                    id=str(uuid.uuid4()),
                    ts=str(now_epoch()),
                    action=action,
                    actor=actor,
                    task_id=task_id,
                    severity=severity,
                    details=details,
                )
            )
            await db.commit()
    except Exception:
        _log.debug("record_audit_event failed for %s", action, exc_info=True)


# Phase-1 spelling. Kept as an alias so ported handlers read 1:1 against the
# monolith during the transition; new code should prefer ``record_audit_event``.
write_audit_event = record_audit_event


__all__ = ["record_audit_event", "write_audit_event"]
