"""Depositor-side cloud-eviction core (shared by the HTTP endpoint and the
auto-rescue lifecycle pass).

shipped cloud eviction as an HTTP-only flow: the depositor picks a
stored :class:`CloudCredential`, we transit-wrap it for the host, and send a
``storage_eviction_response{action: cloud}`` frame. The host then streams the
ciphertext it already holds straight to the depositor's bucket — the host
never sees plaintext and the depositor never needs the deposit password.

The auto-rescue pass needs the exact same behaviour without an HTTP request,
so the logic lives here. :func:`request_cloud_eviction` raises
:class:`CloudEvictionError` with a short machine reason on failure; the
endpoint maps those to HTTP codes, the pass audits them.
"""

from __future__ import annotations

import base64
import os


class CloudEvictionError(Exception):
    """Raised when a cloud-eviction request can't be assembled or sent.

    ``reason`` is a short slug (e.g. ``"credential not found"``); callers
    decide how to surface it.
    """


async def request_cloud_eviction(
    deposit_id: str, cred_id: str, cloud_dest: str = ""
) -> None:
    """Ship a transit-wrapped credential to the host so it evicts to cloud.

    Mirrors the original ``/foreign_storage/evict_to_cloud`` endpoint body.
    Flips the depositor row to ``evicting_to_cloud`` and stamps the
    credential's ``last_used_at`` before sending.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import undefer

    from nexus.core import LOCAL_SETTINGS
    from nexus.networking.storage_pump import build_storage_eviction_response
    from nexus.networking.tunnel import _send_to_peer
    from nexus.security.cred_crypto import (
        EVICTION_NONCE_BYTES,
        unwrap_credential_blob,
        wrap_for_transit,
    )
    from nexus.storage import (
        CloudCredential,
        ForeignStorageDeposit,
        get_session,
    )
    from nexus.storage.repositories import get_peer_by_ip
    from nexus.telemetry.audit import record_audit_event
    from nexus.utils.time import timestamp

    if not cred_id:
        raise CloudEvictionError("credential_id required")

    async with get_session() as db:
        cred = (
            await db.execute(
                select(CloudCredential)
                .options(undefer(CloudCredential.encrypted_blob))
                .filter(CloudCredential.id == cred_id)
            )
        ).scalar_one_or_none()
        if cred is None:
            raise CloudEvictionError("credential not found")
        cred_provider = cred.provider
        cred_default = cred.default_folder or ""
        wrapped_at_rest = bytes(cred.encrypted_blob or b"")

        row = (
            await db.execute(
                select(ForeignStorageDeposit).filter(
                    ForeignStorageDeposit.deposit_id == deposit_id,
                    ForeignStorageDeposit.role == "depositor",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise CloudEvictionError("deposit not found")
        host_uuid = row.host_uuid
        row.status = "evicting_to_cloud"
        row.cloud_provider = cred_provider
        row.cloud_dest = cloud_dest or cred_default
        cred.last_used_at = timestamp()
        await db.commit()

    host_peer = await get_peer_by_ip(host_uuid)
    host_signing_key = (host_peer.signing_key if host_peer else "") or ""
    if not host_signing_key:
        raise CloudEvictionError("no signing_key for host peer")

    try:
        creds_plain = unwrap_credential_blob(wrapped_at_rest)
    except Exception as exc:
        raise CloudEvictionError(f"could not unwrap credential: {exc}")

    nonce = os.urandom(EVICTION_NONCE_BYTES)
    transit_blob = wrap_for_transit(host_signing_key, nonce, creds_plain)
    # Best-effort scrub of the cleartext creds.
    if isinstance(creds_plain, (bytes, bytearray)):
        creds_plain = b"\x00" * len(creds_plain)

    sent = await _send_to_peer(
        host_uuid,
        build_storage_eviction_response(
            deposit_id,
            "cloud",
            cloud_provider=cred_provider,
            cloud_dest=cloud_dest or cred_default,
            cloud_eviction_nonce_b64=base64.b64encode(nonce).decode(),
            cloud_credential_blob_b64=base64.b64encode(transit_blob).decode(),
        ),
    )
    if not sent:
        raise CloudEvictionError("could not reach host")

    await record_audit_event(
        "storage.cloud_eviction_requested",
        actor=LOCAL_SETTINGS.get("node_uuid", ""),
        task_id=deposit_id,
        details=f"provider={cred_provider}",
    )


__all__ = ["request_cloud_eviction", "CloudEvictionError"]
