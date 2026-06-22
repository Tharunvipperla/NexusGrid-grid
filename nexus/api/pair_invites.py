"""``/local/pair/*`` — +F.3 pair-invite token API.

Local endpoints for:

* issuing / listing / revoking outbound invites (W36.F.1),
* redeeming an inbound ``nxg://pair#...`` link via relay-mediated
  handshake (W36.F.3),
* surfacing + acting on incoming pair requests parked by
  :mod:`nexus.runtime.pair_handshake` (W36.F.3).

Security model lives in ``nexus/security/pair_invite.py``; relay-side
gating lives in ``nexus/relay/server.py``'s
``pair_invite_probe_ws`` endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import websockets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from nexus.networking.relay_client import my_relay_pool_urls, relay_send
from nexus.security import verify_local_auth
from nexus.security.group_keys import (
    get_local_group_privkey,
    get_local_group_pubkey,
)
from nexus.security.pair_invite import (
    decode_pair_link,
    encode_pair_link,
    sign_pair_invite,
    verify_pair_invite,
)
from nexus.storage import get_session
from nexus.storage.models import PairAttempt, PairInvite, Peer
from nexus.utils.time import iso_now


_log = logging.getLogger("nexus.api.pair_invites")

router = APIRouter(
    prefix="/local/pair",
    tags=["Pair Invites"],
    dependencies=[Depends(verify_local_auth)],
)


# Pair invites are now a singleton permanent "follow link"
# per node — Twitter/Instagram-style. The signed envelope's expires_at
# is set to a 100-year horizon so it never trips verify_pair_invite's
# expiry gate; per-redeemer rate-limiting lives in the PairAttempt table.
PERMANENT_HORIZON_YEARS = 100
PERMANENT_MAX_USES = 1_000_000


def _row_summary(row: PairInvite, link: str = "") -> dict:
    return {
        "invite_id": row.invite_id,
        "issuer_pubkey": row.issuer_pubkey or "",
        "issued_at": row.issued_at or "",
        "expires_at": row.expires_at or "",
        "max_uses": int(row.max_uses or 1),
        "used_count": int(row.used_count or 0),
        "status": row.status or "active",
        "last_used_at": row.last_used_at or "",
        "last_redeemer_pubkey": row.last_redeemer_pubkey or "",
        "is_permanent": bool(row.is_permanent),
        "link": link,
    }


@router.get(
    "/permanent_link",
    summary="Get this node's permanent pair-invite (follow) link",
)
async def get_permanent_pair_link() -> dict:
    """Return the singleton ``nxg://pair#...`` link for this node.

    Twitter/Instagram-style follow link. Lazy-created on first
    call and reused forever (same ``invite_id`` until the user explicitly
    rotates via ``/local/pair/permanent_link/rotate``). The signed
    envelope's ``expires_at`` is set to a 100-year horizon. Per-redeemer
    rate-limit (one attempt per ``bob_pubkey`` across the link's
    lifetime) is enforced by :class:`PairAttempt`.
    """
    issuer_pubkey = get_local_group_pubkey()
    issuer_privkey = get_local_group_privkey()
    if not issuer_pubkey or not issuer_privkey:
        raise HTTPException(
            status_code=409,
            detail="local group keypair not initialised",
        )

    # Pair invites are inherently cross-region: the redeemer can't reach
    # the issuer without a relay (there's no LAN-only pair-link flow —
    # LAN pairing uses the Connect button + direct address). Surface the
    # missing-relay condition as a 409 so the UI shows the "Run / Paste
    # relay" prompt before we mint a dead link.
    relay_urls = await my_relay_pool_urls()
    if not relay_urls:
        raise HTTPException(
            status_code=409,
            detail=(
                "no relay is currently subscribed — start or bind a relay "
                "first (Network → Local Relay), then try again"
            ),
        )

    from nexus.core.identity import get_or_create_node_uuid

    async with get_session() as session:
        row = (
            await session.execute(
                select(PairInvite).filter(
                    PairInvite.is_permanent == True,  # noqa: E712
                    PairInvite.issuer_pubkey == issuer_pubkey,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            invite_id = secrets.token_hex(32)
            issued_at = iso_now()
            expires_at = (
                datetime.now(timezone.utc)
                + timedelta(days=365 * PERMANENT_HORIZON_YEARS)
            ).isoformat()
            signed_blob = sign_pair_invite(
                invite_id=invite_id,
                issuer_pubkey=issuer_pubkey,
                issued_at=issued_at,
                expires_at=expires_at,
                max_uses=PERMANENT_MAX_USES,
                issuer_privkey=issuer_privkey,
            )
            row = PairInvite(
                invite_id=invite_id,
                issuer_pubkey=issuer_pubkey,
                issued_at=issued_at,
                expires_at=expires_at,
                max_uses=PERMANENT_MAX_USES,
                used_count=0,
                status="active",
                last_used_at="",
                last_redeemer_pubkey="",
                signed_blob=signed_blob,
                is_permanent=True,
            )
            session.add(row)
            await session.commit()

    link = encode_pair_link(
        issuer_pubkey=issuer_pubkey,
        issuer_node_id=get_or_create_node_uuid(),
        relay_urls=relay_urls,
        signed_invite_b64=row.signed_blob,
    )
    return _row_summary(row, link=link)


@router.get("/invites", summary="List pair invites issued by this node")
async def list_pair_invites() -> dict:
    """All pair invites this node has ever issued, newest first.

    Note: ``link`` field is empty for stored rows (the link is only
    surfaced at creation time so the user can copy it).
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(PairInvite).order_by(PairInvite.issued_at.desc())
            )
        ).scalars().all()
    return {"invites": [_row_summary(r) for r in rows]}


@router.delete(
    "/invites/{invite_id}", summary="Revoke an issued pair invite"
)
async def revoke_pair_invite(invite_id: str) -> dict:
    """Mark an issued invite as revoked.

    Revocation is local-only: a scraper who already has the signed
    blob can still present it to a relay, but the relay 
    consults this node's revocation cache before forwarding. Until
    F.2 lands, revocation is informational — the issuer simply won't
    accept the incoming request.
    """
    async with get_session() as session:
        row = await session.get(PairInvite, invite_id)
        if row is None:
            raise HTTPException(status_code=404, detail="invite not found")
        if row.status in ("redeemed", "rejected"):
            # Terminal states — nothing further to do.
            return {"invite_id": invite_id, "status": row.status}
        row.status = "revoked"
        row.last_used_at = iso_now()
        await session.commit()
    return {"invite_id": invite_id, "status": "revoked"}


# ---------------------------------------------------------------------------
# Redeem inbound link + incoming-request UI surface
# ---------------------------------------------------------------------------


REDEEM_REPLY_TIMEOUT_SEC = 65  # slightly above the relay's 60s reply window


class RedeemPairLinkBody(BaseModel):
    link: str = Field(..., min_length=12)
    display_name: Optional[str] = Field(default="", max_length=80)
    message: Optional[str] = Field(default="", max_length=500)


@router.post("/redeem", summary="Redeem an inbound nxg://pair#... link")
async def redeem_pair_link(body: RedeemPairLinkBody) -> dict:
    """Drive the sender side of the pair handshake.

    Steps:

    1. Parse the link + locally verify the embedded signed invite
       (re-checks the relay's gate against tampering on the way to us).
    2. Walk the link's relay URLs and open a transient WS to
       ``/relay/pair_invite/{issuer_node_id}`` on each — first reply
       wins (accept or reject); first connect refusal falls through
       to the next relay.
    3. On accept, store/upsert a Peer row with the issuer's pubkey +
       advertised relay set + grid_key (private payload).
    4. Return ``{status, ...}`` synchronously so the UI can show
       success / rejection / timeout immediately.
    """
    parsed = decode_pair_link(body.link)
    if parsed is None:
        raise HTTPException(status_code=400, detail="malformed pair link")

    issuer_pubkey = str(parsed["k"])
    issuer_node_id = str(parsed["n"])
    relay_urls = [str(u) for u in (parsed.get("r") or []) if isinstance(u, str)]
    inv_b64 = str(parsed["inv"])

    # Refuse self-pair attempts. Without this, a user pasting
    # their own permanent link into the redeem box would create a useless
    # round-trip — and on the issuer side, _handle_incoming_pair_probe
    # would self-reject anyway. Cheap upfront check.
    if issuer_pubkey == (get_local_group_pubkey() or ""):
        raise HTTPException(
            status_code=409,
            detail="cannot pair with yourself — that's your own link",
        )

    inv = verify_pair_invite(inv_b64, expected_issuer_pubkey=issuer_pubkey)
    if inv is None:
        raise HTTPException(
            status_code=400,
            detail="signed invite is invalid, expired, or doesn't match its pubkey",
        )
    if not relay_urls:
        raise HTTPException(
            status_code=400,
            detail="link contains no relay URLs — issuer is LAN-only",
        )

    from nexus.core.identity import get_or_create_node_uuid, get_node_port

    bob_pubkey = get_local_group_pubkey()
    if not bob_pubkey:
        raise HTTPException(
            status_code=409, detail="local group keypair not initialised"
        )

    bob_node_id = get_or_create_node_uuid()
    bob_relay_urls = await my_relay_pool_urls()
    request_id = str(_uuid.uuid4())

    probe_frame = {
        "type": "pair_invite_probe",
        "inv": inv_b64,
        "bob_pubkey": bob_pubkey,
        "bob_node_id": bob_node_id,
        "bob_relay_urls": bob_relay_urls,
        "bob_display_name": (body.display_name or "").strip()[:80],
        "bob_message": (body.message or "").strip()[:500],
        "request_id": request_id,
    }

    last_error = "no relay reachable"
    for relay_url in relay_urls:
        relay_url = relay_url.strip()
        if not relay_url:
            continue
        ws_url = f"{relay_url}/relay/pair_invite/{quote(issuer_node_id, safe='')}"
        try:
            async with websockets.connect(
                ws_url, open_timeout=10
            ) as ws:
                await ws.send(json.dumps(probe_frame))
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=REDEEM_REPLY_TIMEOUT_SEC
                    )
                except asyncio.TimeoutError:
                    last_error = "issuer did not respond in time"
                    continue
                try:
                    reply = json.loads(raw)
                except Exception:
                    last_error = "malformed reply from relay"
                    continue
                reply_type = str(reply.get("type", ""))
                if reply_type == "pair_reject":
                    return {
                        "status": "rejected",
                        "reason": str(reply.get("reason", "")),
                        "issuer_pubkey": issuer_pubkey,
                    }
                if reply_type != "pair_reply":
                    last_error = f"unexpected reply type: {reply_type}"
                    continue
                decision = str(reply.get("decision", ""))
                payload = reply.get("payload") or {}
                if decision == "reject":
                    return {
                        "status": "rejected",
                        "reason": str(payload.get("reason", "rejected by user")),
                        "issuer_pubkey": issuer_pubkey,
                    }
                if decision != "accept":
                    last_error = f"unknown decision: {decision}"
                    continue

                # Accept: store/upsert Peer row.
                accepted_at = iso_now()
                # Issuer shares their grid_key privately in
                # the accept payload so we can fall through to a
                # transient-WS connection when our pool overlap is empty.
                issuer_grid_key = str(payload.get("issuer_grid_key", "") or "")
                async with get_session() as session:
                    peer = (
                        await session.execute(
                            select(Peer).filter(Peer.ip == issuer_node_id)
                        )
                    ).scalar_one_or_none()
                    relay_urls_blob = json.dumps(sorted(set(relay_urls)))
                    if peer is None:
                        peer = Peer(
                            ip=issuer_node_id,
                            status="trusted",
                            role="master",
                            display_name=str(payload.get(
                                "issuer_display_name", ""
                            ) or "")[:80],
                            peer_relay_urls=relay_urls_blob,
                            peer_grid_key=issuer_grid_key,
                        )
                        session.add(peer)
                    else:
                        peer.status = "trusted"
                        if issuer_grid_key:
                            peer.peer_grid_key = issuer_grid_key
                        peer.peer_relay_urls = relay_urls_blob
                    await session.commit()
                return {
                    "status": "accepted",
                    "issuer_pubkey": issuer_pubkey,
                    "issuer_node_id": issuer_node_id,
                    "accepted_at": accepted_at,
                }
        except (websockets.WebSocketException, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            continue

    return {"status": "unreachable", "reason": last_error}


@router.get("/incoming", summary="List pending incoming pair requests")
async def list_incoming_pair_requests() -> dict:
    """Pair requests forwarded by the relay, awaiting the user's
    accept/reject decision."""
    from nexus.runtime import pair_handshake

    await pair_handshake.prune_stale()
    items = await pair_handshake.list_pending()
    return {
        "incoming": [
            {
                "transient_id": r.transient_id,
                "invite_id": r.invite_id,
                "bob_pubkey": r.bob_pubkey,
                "bob_relay_urls": r.bob_relay_urls,
                "bob_display_name": r.bob_display_name,
                "received_at": r.received_at,
            }
            for r in items
        ]
    }


async def _send_pair_reply(transient_id: str, decision: str, payload: dict) -> bool:
    return await relay_send({
        "type": "pair_reply",
        "transient_id": transient_id,
        "decision": decision,
        "payload": payload,
    })


@router.post(
    "/incoming/{transient_id}/accept",
    summary="Accept a pending pair request",
)
async def accept_pair_request(transient_id: str) -> dict:
    """Approve a pending incoming pair request.

    Marks the local ``PairInvite`` row redeemed, upserts a trusted
    Peer row for the requester, and tells the relay to forward the
    accept (with our pubkey + relay set + display name) to the
    requester's parked transient WS.
    """
    from nexus.core.identity import get_or_create_node_uuid
    from nexus.runtime import pair_handshake

    req = await pair_handshake.pop(transient_id)
    if req is None:
        raise HTTPException(status_code=404, detail="pending request not found")

    issuer_pubkey = get_local_group_pubkey()
    issuer_node_id = get_or_create_node_uuid()
    issuer_relay_urls = await my_relay_pool_urls()

    async with get_session() as session:
        row = await session.get(PairInvite, req.invite_id)
        if row is not None:
            # Permanent links stay "active" — per-redeemer rate-limit is
            # carried by PairAttempt, not the parent row's status.
            if not bool(row.is_permanent):
                row.status = "redeemed"
            row.used_count = int(row.used_count or 0) + 1
            row.last_used_at = iso_now()
            row.last_redeemer_pubkey = req.bob_pubkey

        # Flip the per-redeemer attempt to "accepted" so any
        # future probe from the same bob_pubkey via this link is
        # auto-rejected by _handle_incoming_pair_probe.
        attempt = await session.get(
            PairAttempt, (req.invite_id, req.bob_pubkey)
        )
        if attempt is not None:
            attempt.decision = "accepted"
            attempt.decided_at = iso_now()

        peer = (
            await session.execute(
                select(Peer).filter(Peer.ip == req.bob_pubkey)
            )
        ).scalar_one_or_none()
        relay_urls_blob = json.dumps(sorted(set(req.bob_relay_urls)))
        if peer is None:
            session.add(
                Peer(
                    ip=req.bob_pubkey,
                    status="trusted",
                    role="master",
                    display_name=req.bob_display_name[:80],
                    peer_relay_urls=relay_urls_blob,
                )
            )
        else:
            peer.status = "trusted"
            peer.peer_relay_urls = relay_urls_blob
        await session.commit()

    # Share our grid_key privately so the peer can use the
    # transient-WS fallback when their subscribed pool has no overlap
    # with our relay set. This is post-consent (user clicked Accept),
    # so private grid_key disclosure to the now-trusted peer is OK.
    from nexus.core import LOCAL_SETTINGS as _LS

    issuer_grid_key = str(_LS.get("relay_grid_key", "") or "")
    payload = {
        "issuer_pubkey": issuer_pubkey,
        "issuer_node_id": issuer_node_id,
        "issuer_relay_urls": issuer_relay_urls,
        "issuer_display_name": "",
        "issuer_grid_key": issuer_grid_key,
        "accepted_at": iso_now(),
    }
    delivered = await _send_pair_reply(transient_id, "accept", payload)
    return {
        "status": "accepted",
        "transient_id": transient_id,
        "delivered": delivered,
    }


@router.post(
    "/incoming/{transient_id}/reject",
    summary="Reject a pending pair request",
)
async def reject_pair_request(
    transient_id: str, reason: str = ""
) -> dict:
    from nexus.runtime import pair_handshake

    req = await pair_handshake.pop(transient_id)
    if req is None:
        raise HTTPException(status_code=404, detail="pending request not found")

    async with get_session() as session:
        row = await session.get(PairInvite, req.invite_id)
        if row is not None:
            # Permanent links stay "active"; the per-redeemer PairAttempt
            # row below carries the "rejected" verdict.
            if not bool(row.is_permanent):
                row.status = "rejected"
            row.used_count = int(row.used_count or 0) + 1
            row.last_used_at = iso_now()
            row.last_redeemer_pubkey = req.bob_pubkey
        attempt = await session.get(
            PairAttempt, (req.invite_id, req.bob_pubkey)
        )
        if attempt is not None:
            attempt.decision = "rejected"
            attempt.decided_at = iso_now()
        await session.commit()

    delivered = await _send_pair_reply(
        transient_id, "reject", {"reason": reason or "rejected by user"}
    )
    return {
        "status": "rejected",
        "transient_id": transient_id,
        "delivered": delivered,
    }


__all__ = ["router"]
