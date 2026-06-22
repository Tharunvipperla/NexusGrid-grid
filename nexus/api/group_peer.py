"""``/peer/group/*`` — peer-to-peer handshake protocol.

Two endpoints, both unauthenticated at the wire level because the
*invite token* and the *grant signature* are themselves the auth:

* ``/peer/group/join_request`` — a joiner presents an invite token +
  their pubkey. If the token validates and capacity is available, the
  admin node (a) inserts the joiner as a member with the ``member``
  role, (b) signs a grant blob, (c) consumes one invite slot, and (d)
  returns the grant + group metadata.
* ``/peer/group/challenge_verify`` — a service host or other peer
  presents a grant blob + nonce + signature. The endpoint resolves
  the group's admin pubkeys from local state and runs
  :func:`verify_challenge`. Returns ``{"ok": bool}``.

is single-node-state: only the admin's node holds the
authoritative group state. will replicate it across members.
"""

from __future__ import annotations

import base64
import logging
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

import uuid as _uuid

from nexus.core.identity import get_or_create_node_uuid
from nexus.security import group_grant, group_invite
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_open,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_grant import KEY_HEX_LEN
from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.storage import get_session
from nexus.storage.models import (
    Group,
    GroupGrant,
    GroupInvitationOffer,
    GroupMember,
    GroupMemberRole,
    GroupPendingJoinRequest,
    GroupRelayBinding,
    GroupRole,
)
from nexus.telemetry import write_audit_event
from nexus.utils.time import iso_now


_log = logging.getLogger("nexus.api.group_peer")

router = APIRouter(prefix="/peer/group", tags=["Groups (P2P)"])


# Default TTL for an admin-issued grant. Step 15.6 ships a heartbeat
# loop that re-signs grants before they lapse; until then the TTL just
# defines the absolute outer bound.
GRANT_TTL_SECONDS = 86400  # 24 h
DEFAULT_MEMBER_ROLE = "member"


# ---- bodies -------------------------------------------------------------


class JoinRequestBody(BaseModel):
    # Invite_token now optional when signed_invite_hex is
    # provided (the v=2 signed flow doesn't need a legacy bearer token).
    invite_token: str = Field(default="")
    # Hex-encoded signed envelope from a v=2 nxg://join#... link.
    # When present, the handler verifies the signature, enforces
    # ``GroupJoinInviteV2.max_uses``, and skips the legacy invite_token
    # lookup. Either ``invite_token`` or ``signed_invite_hex`` must be set.
    signed_invite_hex: str = Field(default="")
    joiner_pubkey: str = Field(min_length=KEY_HEX_LEN, max_length=KEY_HEX_LEN)
    # (16.2): optional joiner-supplied note shown to admins on
    # the pending-request row in private-mode groups. Ignored in open
    # mode. Kept short to discourage abuse.
    message: str = Field(default="", max_length=512)
    # (16.2): how the admin can reach the joiner to deliver the
    # decision later (16.4). Optional — open mode doesn't need it.
    joiner_address: str = Field(default="", max_length=256)
    # (post-ship fix): self-declared display name persisted on
    # the admin's GroupMember row so the Members tab can render names.
    display_name: str = Field(default="", max_length=64)
    # Joiner's X25519 pubkey so the founder can ECIES-seal the
    # group symkey to them. Optional in this wave (legacy clients can
    # omit) but expected from any Wave-18+ client.
    joiner_x25519_pub: str = Field(default="", max_length=KEY_HEX_LEN)
    # Joiner's node UUID, stored on the GroupMember row so
    # group fan-out can reach this member over the WS relay when direct
    # HTTP fails. Optional — legacy clients omit it.
    joiner_node_id: str = Field(default="", max_length=128)


class ChallengeVerifyBody(BaseModel):
    group_id: str = Field(min_length=1)
    grant_blob_b64: str = Field(min_length=1)
    nonce_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class RelayCodeRequestBody(BaseModel):
    """A relay:host member asks a current relay host for the group's
    relay module source. The grant + challenge signature authenticate the
    requester and prove (via the grant's roles) they may host a relay."""

    group_id: str = Field(min_length=1)
    grant_blob_b64: str = Field(min_length=1)
    nonce_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class GroupInfoProbeBody(BaseModel):
    """A pre-join lookup so the joiner UI can show context."""

    invite_token: str = Field(min_length=1)


class GroupRosterBody(BaseModel):
    """(post-ship): roster pull by group_id.

    Open to anyone holding the group_id — the roster contains pubkeys
    and reachable addresses, not secrets. Signed-roster replication is
    a later wave.
    """

    group_id: str = Field(min_length=1)


class UpdateDisplayNameBody(BaseModel):
    """(post-ship): signed self-rename pushed by a member.

    Authenticity is established via the same challenge-response primitive
    used for /peer/group/challenge_verify — the nonce binds the signature
    to the *new* display name, so a captured signature can't be replayed
    to set a different name.
    """

    group_id: str = Field(min_length=1)
    member_pubkey: str = Field(min_length=KEY_HEX_LEN, max_length=KEY_HEX_LEN)
    display_name: str = Field(default="", max_length=64)
    grant_blob_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


class InvitationOfferBody(BaseModel):
    """Payload pushed by a founder to a trusted-peer recipient."""

    token: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    group_name: str = Field(default="")
    founder_pubkey: str = Field(default="")
    founder_address: str = Field(default="")
    target_peer_label: str = Field(default="")


class InvitationDeclineBody(BaseModel):
    """Payload pushed by the recipient back to the founder."""

    token: str = Field(min_length=1)


class InvitationAcceptBody(BaseModel):
    """(post-ship): payload pushed by the recipient back to the founder
    when they accept a targeted invitation, so the sender-side row can flip
    out of ``pending`` without waiting for a manual refresh."""

    token: str = Field(min_length=1)


class JoinDecisionBody(BaseModel):
    """Payload pushed by an admin to the joiner's node."""

    request_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    group_name: str = Field(default="")
    founder_pubkey: str = Field(min_length=1)
    decision: str = Field(min_length=1)  # 'approved' | 'rejected'
    grant_blob_b64: str = Field(default="")
    default_role: str = Field(default="member")
    issued_at: str = Field(default="")
    expires_at: str = Field(default="")
    reason: str = Field(default="", max_length=512)
    # (post-ship fix): so the joiner's local cache shows the
    # correct privacy_mode and renders the founder in the Members tab.
    privacy_mode: str = Field(default="open")
    founder_display_name: str = Field(default="", max_length=64)
    # (post-ship): founder/admin's reachable host:port so the
    # joiner can later pull the roster via /peer/group/roster.
    founder_address: str = Field(default="", max_length=256)
    # Founder's node UUID so the joiner can DM the founder
    # (the Message button needs node_id) without a manual roster pull.
    founder_node_id: str = Field(default="", max_length=128)
    # ECIES-sealed group symkey for this joiner; empty for
    # legacy admins who haven't started sealing yet.
    symkey_envelope_b64: str = Field(default="")


class GroupEventBody(BaseModel):
    """An opaque ``GroupFrame`` published to a group channel.

    Carried as direct admin-to-admin HTTP fanout today; the
    relay-channel transport (+) reuses the same frame shape.
    """

    frame_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    frame_type: str = Field(min_length=1)
    sender_grant_b64: str = Field(min_length=1)
    nonce_b64: str = Field(min_length=1)
    ciphertext_b64: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)


# ---- helpers ------------------------------------------------------------


def _expires_iso(ttl_seconds: int) -> str:
    """Return an ISO timestamp ``ttl_seconds`` from now."""
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).isoformat()


async def _admin_pubkeys(session, group_id: str) -> list[str]:
    """Return every pubkey holding the founder or admin role in *group_id*."""
    rows = await session.execute(
        select(GroupMemberRole.member_pubkey).where(
            (GroupMemberRole.group_id == group_id)
            & (GroupMemberRole.role_name.in_(("founder", "admin")))
        )
    )
    return sorted({row[0] for row in rows.fetchall()})


async def _ensure_group_symkey(session, group_id: str) -> bytes:
    """Return the plaintext group symkey, minting + self-sealing on first use.

    The encrypted copy is stored on ``Group.group_symkey_enc`` so this
    node can re-open it later (founder/admin's own copy). Called from
    :func:`issue_member_grant` right before sealing to a joiner.
    """
    group = await session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    my_privkey = get_local_group_privkey()
    if group.group_symkey_enc:
        return ecies_open(bytes(group.group_symkey_enc), my_privkey)
    # Lazy mint — first grant being issued for this group.
    symkey = mint_group_symkey()
    my_x25519_pub = derive_x25519_pubkey_hex(my_privkey)
    group.group_symkey_enc = ecies_seal(symkey, my_x25519_pub)
    await session.flush()
    return symkey


async def issue_member_grant(
    session,
    *,
    group_id: str,
    joiner_pubkey: str,
    invite_token: str,
    display_name: str = "",
    peer_address: str = "",
    joiner_x25519_pub: str = "",
    joiner_node_id: str = "",
) -> dict:
    """Consume one invite slot, mint a grant, persist membership.

    Shared by /peer/group/join_request (open mode) and the admin
    approve endpoint in /local/groups (private mode). Caller commits
    the session after this returns.

    Raises HTTPException on closed/rotated invite.

    ``invite_token=""`` signals the signed v=2 flow — the
    caller has already gated on :class:`GroupJoinInviteV2`'s
    ``max_uses``, so we skip the legacy bearer-token consume here.
    """
    if invite_token:
        consume = await group_invite.consume_invite(
            session=session, token=invite_token, group_id=group_id
        )
        if not consume.ok:
            raise HTTPException(
                status_code=410, detail=f"invite closed: {consume.reason}"
            )

    now = iso_now()
    expires_at = _expires_iso(GRANT_TTL_SECONDS)
    nonce_hex = secrets.token_hex(16)
    roles = (DEFAULT_MEMBER_ROLE,)
    admin_privkey = get_local_group_privkey()
    grant_blob = group_grant.sign_grant(
        group_id=group_id,
        member_pubkey=joiner_pubkey,
        roles=roles,
        admin_privkey=admin_privkey,
        issued_at=now,
        expires_at=expires_at,
        nonce=nonce_hex,
    )

    session.add(
        GroupMember(
            group_id=group_id,
            pubkey=joiner_pubkey,
            joined_at=now,
            last_heartbeat_at=now,
            display_name=display_name,
            peer_address=peer_address,
            member_x25519_pub=joiner_x25519_pub,
            node_id=joiner_node_id,
        )
    )
    session.add(
        GroupMemberRole(
            group_id=group_id,
            member_pubkey=joiner_pubkey,
            role_name=DEFAULT_MEMBER_ROLE,
            assigned_by_pubkey=get_local_group_pubkey(),
            assigned_at=now,
        )
    )
    session.add(
        GroupGrant(
            id=str(secrets.token_hex(16)),
            group_id=group_id,
            member_pubkey=joiner_pubkey,
            issued_by_pubkey=get_local_group_pubkey(),
            issued_at=now,
            expires_at=expires_at,
            nonce=nonce_hex,
            signature=grant_blob,
            roles_json='["' + DEFAULT_MEMBER_ROLE + '"]',
        )
    )
    await session.flush()

    # Lazily mint + seal the group symkey for this joiner.
    # If the joiner didn't advertise an X25519 pubkey (legacy client),
    # skip — they'll re-handshake on a later wave to receive a copy.
    symkey_envelope = b""
    if joiner_x25519_pub:
        plaintext_symkey = await _ensure_group_symkey(session, group_id)
        symkey_envelope = ecies_seal(plaintext_symkey, joiner_x25519_pub)

    return {
        "grant_blob": grant_blob,
        "issued_at": now,
        "expires_at": expires_at,
        "default_role": DEFAULT_MEMBER_ROLE,
        "symkey_envelope": symkey_envelope,
    }


# ---- /peer/group/join_request -------------------------------------------


@router.post("/join_request", summary="Submit an invite token + joiner pubkey")
async def peer_group_join_request(body: JoinRequestBody) -> dict:
    """Validate the invite and (on success) issue a member grant.

    accepts both legacy bearer-token invites (``invite_token``)
    and signed v=2 envelopes (``signed_invite_hex``). The signed path
    enforces per-link ``max_uses`` (via :class:`GroupJoinInviteV2`) and
    per-group ``max_members`` (via :attr:`Group.max_members`).
    """
    # Validate joiner_pubkey is hex.
    try:
        bytes.fromhex(body.joiner_pubkey)
    except ValueError:
        raise HTTPException(status_code=400, detail="joiner_pubkey not hex")

    if not body.invite_token and not body.signed_invite_hex:
        raise HTTPException(
            status_code=400,
            detail="invite_token or signed_invite_hex must be provided",
        )

    async with get_session() as session:
        signed_invite_row = None
        if body.signed_invite_hex:
            # Signed v=2 path. Verify signature against the
            # local group's founder_pubkey, then consult the local
            # GroupJoinInviteV2 row for status + max_uses gate.
            from nexus.security.group_invite_token import (
                verify_group_join_invite,
            )
            from nexus.storage.models import GroupJoinInviteV2

            inv = verify_group_join_invite(body.signed_invite_hex)
            if inv is None:
                raise HTTPException(
                    status_code=400,
                    detail="signed invite is invalid, expired, or malformed",
                )
            signed_invite_row = await session.get(
                GroupJoinInviteV2, inv.invite_id
            )
            if signed_invite_row is None:
                raise HTTPException(
                    status_code=404,
                    detail="invite not recognised by this founder (revoked or never issued)",
                )
            if signed_invite_row.status in ("revoked", "exhausted"):
                raise HTTPException(
                    status_code=410,
                    detail=f"invite {signed_invite_row.status}",
                )
            if int(signed_invite_row.used_count or 0) >= int(
                signed_invite_row.max_uses or 1
            ):
                raise HTTPException(
                    status_code=410, detail="invite max_uses reached"
                )
            if signed_invite_row.group_id != inv.group_id:
                raise HTTPException(
                    status_code=400, detail="invite group mismatch"
                )
            # Security F-013: pin the signature to the founder key THIS node
            # recorded when it issued the invite. verify_group_join_invite only
            # checks the envelope is self-consistently signed by its *claimed*
            # founder_pubkey; without this pin, anyone holding a valid invite_id
            # could mint a self-signed envelope (their own key) with tampered
            # fields — e.g. a future expires_at to revive an expired invite.
            if signed_invite_row.founder_pubkey != inv.founder_pubkey:
                raise HTTPException(
                    status_code=403,
                    detail="invite not signed by this group's founder",
                )
            group_id = inv.group_id
        else:
            validation = await group_invite.validate_invite(
                session=session, token=body.invite_token
            )
            if not validation.ok:
                # Map invite reasons to HTTP status codes that distinguish
                # "token does not exist" from "token is closed".
                if validation.reason == group_invite.REASON_NOT_FOUND:
                    raise HTTPException(status_code=404, detail="invite not found")
                raise HTTPException(
                    status_code=410,
                    detail=f"invite closed: {validation.reason}",
                )
            group_id = validation.invite.group_id

        # Already a member? Refuse — don't double-bill the invite.
        existing = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == body.joiner_pubkey)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Diagnostic: when the joiner's pubkey matches this admin's
            # own pubkey, the two nodes are sharing a .nexus_group_key
            # file (same working directory). Steer the operator at the
            # real fix instead of the misleading "already a member".
            my_pubkey = get_local_group_pubkey()
            if body.joiner_pubkey == my_pubkey:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "joiner pubkey matches this admin node's own "
                        "group pubkey — both nodes are sharing the "
                        "same .nexus_group_key file. Run each node "
                        "from its own working directory."
                    ),
                )
            raise HTTPException(
                status_code=409, detail="already a member of this group"
            )

        group = await session.get(Group, group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")

        # Enforce the group's hard-cap on membership before doing
        # any state mutation. Counts current GroupMember rows; the new
        # joiner would be +1 → refuse if that exceeds max_members.
        max_members = int(group.max_members or 0)
        if max_members > 0:
            current_count = int(
                (
                    await session.execute(
                        select(GroupMember).where(
                            GroupMember.group_id == group_id
                        )
                    )
                ).scalars().all().__len__()
            )
            if current_count >= max_members:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"group is full ({current_count}/{max_members} members)"
                    ),
                )

        # (16.2): private groups defer the slot-consume + grant
        # issuance to an explicit admin approval. We park the request in
        # the pending queue (no slot consumed) and return 202.
        # The token is still validated above so a closed/rotated token
        # is rejected before it ever reaches the pending queue.
        privacy_mode = group.privacy_mode or "open"
        if privacy_mode == "private":
            request_id = _uuid.uuid4().hex
            now = iso_now()
            pending_row = GroupPendingJoinRequest(
                id=request_id,
                group_id=group_id,
                joiner_pubkey=body.joiner_pubkey,
                joiner_address=body.joiner_address or "",
                invite_token=body.invite_token,
                message=body.message or "",
                display_name=body.display_name or "",
                joiner_x25519_pub=body.joiner_x25519_pub or "",
                joiner_node_id=body.joiner_node_id or "",
                status="pending",
                created_at=now,
            )
            session.add(pending_row)
            # Replicate this request to every other admin so any
            # group:approve holder can act on it — not just this node.
            from nexus.runtime.group_inbox import publish_pending_request

            await publish_pending_request(session, pending_row)
            await session.commit()

            await write_audit_event(
                action="group.join.pending",
                actor="peer",
                task_id="",
                details=(
                    f"group_id={group_id} joiner={body.joiner_pubkey[:8]} "
                    f"request_id={request_id[:8]}"
                ),
            )
            return {
                "status": "pending",
                "request_id": request_id,
                "group_id": group_id,
                "group_name": group.name,
                "founder_pubkey": group.founder_pubkey,
                "privacy_mode": "private",
            }

        # Open mode (Wave-15 flow): issue the grant inline.
        grant_data = await issue_member_grant(
            session,
            group_id=group_id,
            joiner_pubkey=body.joiner_pubkey,
            invite_token=body.invite_token,
            display_name=body.display_name or "",
            peer_address=body.joiner_address or "",
            joiner_x25519_pub=body.joiner_x25519_pub or "",
            joiner_node_id=body.joiner_node_id or "",
        )
        # Consume one max_uses slot on the signed invite.
        # Mark exhausted when the cap is hit.
        if signed_invite_row is not None:
            signed_invite_row.used_count = int(
                signed_invite_row.used_count or 0
            ) + 1
            signed_invite_row.last_used_at = iso_now()
            if signed_invite_row.used_count >= int(
                signed_invite_row.max_uses or 1
            ):
                signed_invite_row.status = "exhausted"
        await session.commit()
        # Broadcast the freshly-joined member so every other
        # member's roster — and crucially the joiner's node_id — converges
        # without waiting for a manual refresh_members pull.
        from nexus.runtime.group_inbox import publish_roster_update

        await publish_roster_update(session, group_id, body.joiner_pubkey)
        # Commit so the "joined the chat" system message that
        # publish_roster_update inserts actually persists (the session was
        # already committed above, so this second commit is required).
        await session.commit()
        admin_pubkeys = await _admin_pubkeys(session, group_id)
        founder_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == group.founder_pubkey)
                )
            )
        ).scalar_one_or_none()

    await write_audit_event(
        action="group.join.granted",
        actor="peer",
        task_id="",
        details=(
            f"group_id={group_id} joiner={body.joiner_pubkey[:8]} "
            f"invite_token={body.invite_token[:8]}"
        ),
    )
    return {
        "status": "ok",
        "privacy_mode": "open",
        "group_id": group_id,
        "group_name": group.name,
        "founder_pubkey": group.founder_pubkey,
        "founder_display_name": (founder_member.display_name if founder_member else "") or "",
        "admin_pubkeys": admin_pubkeys,
        "grant_blob_b64": base64.b64encode(grant_data["grant_blob"]).decode("ascii"),
        "default_role": grant_data["default_role"],
        "issued_at": grant_data["issued_at"],
        "expires_at": grant_data["expires_at"],
        "symkey_envelope_b64": (
            base64.b64encode(grant_data["symkey_envelope"]).decode("ascii")
            if grant_data["symkey_envelope"]
            else ""
        ),
    }


# ---- /peer/group/challenge_verify ---------------------------------------


@router.post("/challenge_verify", summary="Verify a grant + challenge signature")
async def peer_group_challenge_verify(body: ChallengeVerifyBody) -> dict:
    """Stateless verification helper.

    Useful when a service host or relay wants to delegate verification
    to the admin node (which has the authoritative admin set). Members
    will typically verify locally against the admin pubkeys they
    cached at join time, without needing this round-trip.
    """
    try:
        grant_blob = base64.b64decode(body.grant_blob_b64.encode("ascii"))
        nonce = base64.b64decode(body.nonce_b64.encode("ascii"))
        signature = base64.b64decode(body.signature_b64.encode("ascii"))
    except (ValueError, TypeError):
        return {"ok": False, "reason": "malformed_base64"}

    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            return {"ok": False, "reason": "group_not_found"}
        admin_pubkeys = await _admin_pubkeys(session, body.group_id)

    ok = group_grant.verify_challenge(
        grant_blob=grant_blob,
        nonce=nonce,
        signature=signature,
        group_admin_pubkeys=admin_pubkeys,
    )
    return {"ok": bool(ok)}


# ---- /peer/group/relay_code (relay-code live-host pull) --------


@router.post(
    "/relay_code",
    summary="Serve the group's relay module source to a relay:host member.",
)
async def peer_group_relay_code(body: RelayCodeRequestBody) -> dict:
    """Return this host's relay module source matching the group's frozen
    fingerprint — the live-host fallback for the copy flow.

    Auth: the grant + challenge proves the caller controls ``member_pubkey``
    and holds a valid admin-signed grant (= a real group member); authorization
    is then checked against THIS host's authoritative current role assignments
    (``has_permission`` over the live roster), not the grant's embedded roles —
    so a member promoted to ``relay:host`` *after* their grant was issued is
    still served. Integrity: we serve ONLY the local module whose fingerprint
    equals the group's frozen fingerprint, so a member can trust the result
    will pass the W63 bind check.
    """
    from nexus.runtime import local_relay
    from nexus.security.group_permissions import PERM_RELAY_HOST, has_permission

    try:
        grant_blob = base64.b64decode(body.grant_blob_b64.encode("ascii"))
        nonce = base64.b64decode(body.nonce_b64.encode("ascii"))
        signature = base64.b64decode(body.signature_b64.encode("ascii"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="malformed_base64")

    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")
        admin_pubkeys = await _admin_pubkeys(session, body.group_id)

        if not group_grant.verify_challenge(
            grant_blob=grant_blob,
            nonce=nonce,
            signature=signature,
            group_admin_pubkeys=admin_pubkeys,
        ):
            raise HTTPException(status_code=403, detail="grant/challenge invalid")

        # Identity proven; authorize against this host's authoritative roster so
        # a post-join promotion to relay:host is honored (the grant's embedded
        # roles can be stale — grants aren't re-issued on a role change).
        parsed = group_grant.verify_grant(
            grant_blob, group_admin_pubkeys=admin_pubkeys
        )
        if parsed is None or not await has_permission(
            session, body.group_id, parsed.member_pubkey, PERM_RELAY_HOST
        ):
            raise HTTPException(
                status_code=403,
                detail="caller lacks relay:host in this group",
            )

        frozen = (group.relay_code_fingerprint or "").strip()

    if not frozen:
        raise HTTPException(
            status_code=409,
            detail="group has no frozen relay fingerprint",
        )
    src = local_relay.module_source_for_fingerprint(frozen)
    if not src:
        raise HTTPException(
            status_code=409,
            detail="this host has no relay code matching the group's fingerprint",
        )
    return {
        "group_id": body.group_id,
        "name": src.get("name") or "",
        "source": src.get("source") or "",
        "fingerprint": src.get("fingerprint") or "",
    }


# ---- /peer/group/join_decision -----------------------------


@router.post(
    "/join_decision",
    summary="Receive an admin's approval or rejection for a private-group join.",
)
async def peer_group_join_decision(body: JoinDecisionBody) -> dict:
    """Idempotent inbound decision handler.

    On ``approved``: materializes the group + this node as member +
    the grant locally (same shape as the open-mode /local/groups/join
    happy path). On ``rejected``: writes an audit row so the UI can
    surface the reason. Either way, repeated deliveries are no-ops
    after the first apply — the admin's retry loop may call us more
    than once.
    """
    from nexus.security.group_permissions import (
        DEFAULT_ROLES,
        encode_role_permissions,
    )

    if body.decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")

    me = get_local_group_pubkey()

    if body.decision == "rejected":
        await write_audit_event(
            action="group.join.rejected_by_admin",
            actor="peer",
            task_id="",
            details=(
                f"group_id={body.group_id} request_id={body.request_id[:8]} "
                f"reason={body.reason!r}"
            ),
        )
        return {"ok": True, "applied": True, "decision": "rejected"}

    # decision == "approved" — materialize membership.
    if not body.grant_blob_b64:
        raise HTTPException(
            status_code=400, detail="approved decisions must carry grant_blob_b64"
        )
    try:
        grant_blob = base64.b64decode(body.grant_blob_b64.encode("ascii"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="grant_blob_b64 is not base64")

    async with get_session() as session:
        # Idempotent: if we already hold a grant for this group from
        # this founder, treat the call as a no-op.
        existing = (
            await session.execute(
                select(GroupGrant).where(
                    (GroupGrant.group_id == body.group_id)
                    & (GroupGrant.member_pubkey == me)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"ok": True, "applied": False, "decision": "approved"}

        now = iso_now()
        symkey_envelope_bytes = b""
        if body.symkey_envelope_b64:
            try:
                symkey_envelope_bytes = base64.b64decode(
                    body.symkey_envelope_b64.encode("ascii")
                )
            except (ValueError, TypeError):
                symkey_envelope_bytes = b""
        existing_group = await session.get(Group, body.group_id)
        if existing_group is None:
            session.add(
                Group(
                    id=body.group_id,
                    name=body.group_name,
                    founder_pubkey=body.founder_pubkey,
                    created_at=now,
                    deleted_at="",
                    privacy_mode=body.privacy_mode or "open",
                    founder_address=body.founder_address or "",
                    group_symkey_enc=symkey_envelope_bytes or None,
                )
            )
            for role_name, perms in DEFAULT_ROLES.items():
                session.add(
                    GroupRole(
                        group_id=body.group_id,
                        name=role_name,
                        permissions_json=encode_role_permissions(perms),
                        created_at=now,
                        updated_at=now,
                    )
                )
            # Stub the founder as a member so the joiner's Members tab
            # isn't empty. Roster replication arrives via the explicit
            # /local/groups/{id}/refresh_members call; this is the
            # bootstrap view.
            session.add(
                GroupMember(
                    group_id=body.group_id,
                    pubkey=body.founder_pubkey,
                    joined_at=now,
                    last_heartbeat_at="",
                    display_name=body.founder_display_name or "",
                    peer_address=body.founder_address or "",
                    node_id=body.founder_node_id or "",
                )
            )
            session.add(
                GroupMemberRole(
                    group_id=body.group_id,
                    member_pubkey=body.founder_pubkey,
                    role_name="founder",
                    assigned_by_pubkey=body.founder_pubkey,
                    assigned_at=now,
                )
            )

        existing_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == body.group_id)
                    & (GroupMember.pubkey == me)
                )
            )
        ).scalar_one_or_none()
        if existing_member is None:
            from nexus.core import LOCAL_SETTINGS as _LS
            from nexus.storage.models import GroupMessage as _GMsg
            my_name = str(_LS.get("user_display_name") or "")
            my_x25519 = derive_x25519_pubkey_hex(get_local_group_privkey())
            session.add(
                GroupMember(
                    group_id=body.group_id,
                    pubkey=me,
                    joined_at=now,
                    last_heartbeat_at=now,
                    display_name=my_name,
                    member_x25519_pub=my_x25519,
                    node_id=get_or_create_node_uuid(),
                )
            )
            # "joined the chat" line on the joiner's own node (they
            # never receive a roster.update about themselves).
            _sid = f"sysjoin-{me}-{now}"
            if await session.get(_GMsg, (body.group_id, _sid)) is None:
                session.add(_GMsg(
                    group_id=body.group_id, msg_id=_sid,
                    sender_pubkey="system", sender_name="",
                    body=f"{my_name or me[:8]} joined the chat",
                    sent_at=now, received_at=now,
                ))
            session.add(
                GroupMemberRole(
                    group_id=body.group_id,
                    member_pubkey=me,
                    role_name=body.default_role or "member",
                    assigned_by_pubkey=body.founder_pubkey,
                    assigned_at=now,
                )
            )

        session.add(
            GroupGrant(
                id=secrets.token_hex(16),
                group_id=body.group_id,
                member_pubkey=me,
                issued_by_pubkey=body.founder_pubkey,
                issued_at=body.issued_at or now,
                expires_at=body.expires_at or now,
                nonce="",
                signature=grant_blob,
                roles_json='["' + (body.default_role or "member") + '"]',
            )
        )
        await session.commit()

    # The founder side emits this on its own join broadcast, but the
    # joiner never receives a roster.update about itself — so emit here too,
    # otherwise the joiner's own "joined the chat" line never live-appears.
    from nexus.runtime import event_bus

    await event_bus.publish({"type": "group.message", "group_id": body.group_id})

    await write_audit_event(
        action="group.join.decision_received",
        actor="peer",
        task_id="",
        details=(
            f"group_id={body.group_id} request_id={body.request_id[:8]} "
            f"decision=approved"
        ),
    )
    return {"ok": True, "applied": True, "decision": "approved"}


# ---- /peer/group/attachment_pull ------------------------------


class AttachmentPullBody(BaseModel):
    group_id: str
    msg_id: str


@router.post(
    "/attachment_pull",
    summary="Serve a sender-hosted (>5MB) group attachment, sealed with the symkey.",
)
async def peer_group_attachment_pull(body: AttachmentPullBody) -> dict:
    """Return the foreign attachment's bytes sealed with the group symkey
    (base64). Only members hold the symkey, so the ciphertext is useless to
    anyone else — no extra gate needed."""
    import base64 as _b64

    from nexus.runtime.chat_attachments import load_blob, seal_with_symkey
    from nexus.runtime.group_inbox import _local_symkey

    raw = load_blob(body.msg_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="attachment not hosted here")
    async with get_session() as session:
        symkey = await _local_symkey(session, body.group_id)
    if symkey is None:
        raise HTTPException(status_code=404, detail="no symkey for channel")
    sealed = seal_with_symkey(symkey, raw)
    return {"sealed_b64": _b64.b64encode(sealed).decode("ascii")}


# ---- /peer/group/event ----------------------------------------


@router.post(
    "/event",
    summary="Receive an opaque replicated-state frame for a group channel.",
)
async def peer_group_event(body: GroupEventBody) -> dict:
    """Verify + apply an inbound group-channel frame.

    Always returns 200; the body reports whether the frame verified
    and whether it changed local state. A malformed or unverifiable
    frame is dropped — it must never crash the endpoint.
    """
    from nexus.runtime.group_inbox import dispatch_inbound_frame

    return await dispatch_inbound_frame(
        {
            "frame_id": body.frame_id,
            "channel": body.channel,
            "frame_type": body.frame_type,
            "sender_grant_b64": body.sender_grant_b64,
            "nonce_b64": body.nonce_b64,
            "ciphertext_b64": body.ciphertext_b64,
            "signature_b64": body.signature_b64,
        }
    )


# ---- /peer/group/publish --------------------------------------


@router.post(
    "/publish",
    summary="Relay an opaque group-channel frame to the group's audience.",
)
async def peer_group_publish(body: GroupEventBody) -> dict:
    """Relay-host endpoint. A publisher hands a sealed frame here and
    this node fans it out to the channel's audience.

    Requires this node to hold ``relay:host`` for the channel — a node
    can't be conscripted into relaying for a group it isn't authorized
    to serve. Past the gate it always returns 200; the body reports
    how many recipients the frame reached.
    """
    from nexus.runtime.group_inbox import relay_inbound_frame
    from nexus.security.group_permissions import PERM_RELAY_HOST, has_permission

    me = get_local_group_pubkey()
    async with get_session() as session:
        if not await has_permission(session, body.channel, me, PERM_RELAY_HOST):
            raise HTTPException(
                status_code=403,
                detail="this node is not a relay host for that channel",
            )

    return await relay_inbound_frame(
        {
            "frame_id": body.frame_id,
            "channel": body.channel,
            "frame_type": body.frame_type,
            "sender_grant_b64": body.sender_grant_b64,
            "nonce_b64": body.nonce_b64,
            "ciphertext_b64": body.ciphertext_b64,
            "signature_b64": body.signature_b64,
        }
    )


# ---- 16.7 pre-join group-info probe -------------------------------------


@router.post(
    "/info",
    summary="Return basic group metadata for a known invite token.",
)
async def peer_group_info(body: GroupInfoProbeBody) -> dict:
    """Read-only probe — validates the token and returns group context.

    Does **not** consume the token. The UI uses this to decide whether
    to surface a 'reason' textbox before letting the user submit the
    real join request.
    """
    async with get_session() as session:
        check = await group_invite.validate_invite(
            session=session, token=body.invite_token
        )
        if not check.ok or check.invite is None:
            raise HTTPException(
                status_code=404, detail=f"invite invalid: {check.reason}"
            )
        group = await session.get(Group, check.invite.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")
        slots_remaining = (
            max(int(check.invite.slot_cap or 0) - int(check.invite.slots_filled or 0), 0)
            if int(check.invite.slot_cap or 0) > 0
            else -1
        )
        return {
            "group_id": group.id,
            "group_name": group.name or "",
            "founder_pubkey": group.founder_pubkey or "",
            "privacy_mode": group.privacy_mode or "open",
            "slots_remaining": slots_remaining,
        }


# ---- roster pull (post-ship) -------------------------------------------


@router.post(
    "/roster",
    summary="Return the authoritative member list for a group.",
)
async def peer_group_roster(body: GroupRosterBody) -> dict:
    """Return ``{founder_pubkey, privacy_mode, members:[...], roles:[...]}``.

    Each member entry carries ``pubkey``, ``display_name``,
    ``joined_at``, ``roles`` (sorted list), and ``peer_address``.
    Role entries carry ``name`` and ``permissions`` so joiners can
    resolve the perms attached to custom roles (otherwise an admin
    granting a custom role like 'Inviter' wouldn't unlock anything on
    the joiner's side because their local role table doesn't know it).
    """
    from nexus.security.group_permissions import decode_role_permissions
    from nexus.storage.models import GroupRelayBinding, GroupRole

    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")

        member_rows = (
            await session.execute(
                select(GroupMember).where(GroupMember.group_id == body.group_id)
            )
        ).scalars().all()
        member_role_rows = (
            await session.execute(
                select(GroupMemberRole).where(
                    GroupMemberRole.group_id == body.group_id
                )
            )
        ).scalars().all()
        role_rows = (
            await session.execute(
                select(GroupRole).where(GroupRole.group_id == body.group_id)
            )
        ).scalars().all()
        roles_by_member: dict[str, list[str]] = {}
        for mr in member_role_rows:
            roles_by_member.setdefault(mr.member_pubkey, []).append(mr.role_name)

        members = [
            {
                "pubkey": m.pubkey,
                "display_name": m.display_name or "",
                "joined_at": m.joined_at or "",
                "peer_address": m.peer_address or "",
                "node_id": m.node_id or "",
                "roles": sorted(roles_by_member.get(m.pubkey, [])),
            }
            for m in member_rows
        ]
        roles = [
            {
                "name": r.name,
                "permissions": list(decode_role_permissions(r.permissions_json)),
            }
            for r in role_rows
        ]
        # Relay bindings ride the roster pull so a member who
        # missed a `relay.update` frame (offline) can resync them.
        relay_rows = (
            await session.execute(
                select(GroupRelayBinding).where(
                    (GroupRelayBinding.group_id == body.group_id)
                    & (GroupRelayBinding.status != "retired")
                )
            )
        ).scalars().all()
        relays = [
            {"relay_url": r.relay_url, "operator_pubkey": r.operator_pubkey or ""}
            for r in relay_rows
        ]
    return {
        "group_id": body.group_id,
        "founder_pubkey": group.founder_pubkey or "",
        "privacy_mode": group.privacy_mode or "open",
        "members": members,
        "roles": roles,
        "relays": relays,
    }


# ---- display-name update (post-ship) -----------------------------------


def _display_name_nonce(new_name: str) -> bytes:
    """Domain-separated nonce that pins the signature to ``new_name``."""
    import hashlib
    return hashlib.sha256(
        b"set_display_name:" + (new_name or "").encode("utf-8")
    ).digest()


@router.post(
    "/update_display_name",
    summary="Apply a signed display-name update from a group member.",
)
async def peer_group_update_display_name(body: UpdateDisplayNameBody) -> dict:
    try:
        grant_blob = base64.b64decode(body.grant_blob_b64.encode("ascii"))
        signature = base64.b64decode(body.signature_b64.encode("ascii"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="malformed base64")

    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")
        admin_pubkeys = await _admin_pubkeys(session, body.group_id)
        ok = group_grant.verify_challenge(
            grant_blob=grant_blob,
            nonce=_display_name_nonce(body.display_name or ""),
            signature=signature,
            group_admin_pubkeys=admin_pubkeys,
        )
        if not ok:
            raise HTTPException(status_code=401, detail="signature verification failed")

        member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == body.group_id)
                    & (GroupMember.pubkey == body.member_pubkey)
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=404, detail="member not in this group")
        member.display_name = (body.display_name or "").strip()[:64]
        await session.commit()

    await write_audit_event(
        action="group.member.display_name_updated",
        actor="peer",
        task_id="",
        details=(
            f"group_id={body.group_id} member={body.member_pubkey[:8]} "
            f"name={member.display_name!r}"
        ),
    )
    return {"ok": True}


# ---- 16.5 targeted invitation push --------------------------------------


@router.post(
    "/invitation_offer",
    summary="Receive a targeted invitation from a founder/admin.",
)
async def peer_group_invitation_offer(body: InvitationOfferBody) -> dict:
    """Persist a recipient-side ``GroupInvitationOffer`` row.

    Idempotent on ``(token, role='recipient')``: re-delivery of the
    same token (e.g. a resend after the recipient lost the notif) just
    refreshes the row's ``status`` back to ``pending`` and updates the
    metadata, without creating duplicates.
    """
    now = iso_now()
    async with get_session() as session:
        existing = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == body.token)
                    & (GroupInvitationOffer.role == "recipient")
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                GroupInvitationOffer(
                    token=body.token,
                    role="recipient",
                    group_id=body.group_id,
                    group_name=body.group_name or "",
                    founder_pubkey=body.founder_pubkey or "",
                    founder_address=body.founder_address or "",
                    target_peer_label=body.target_peer_label or "",
                    status="pending",
                    created_at=now,
                    responded_at="",
                )
            )
        else:
            existing.group_id = body.group_id
            existing.group_name = body.group_name or existing.group_name
            existing.founder_pubkey = body.founder_pubkey or existing.founder_pubkey
            existing.founder_address = body.founder_address or existing.founder_address
            existing.target_peer_label = (
                body.target_peer_label or existing.target_peer_label
            )
            existing.status = "pending"
            existing.responded_at = ""
        await session.commit()

    await write_audit_event(
        action="group.invitation.received",
        actor="peer",
        task_id="",
        details=(
            f"group_id={body.group_id} token={body.token[:8]} "
            f"from={body.founder_address}"
        ),
    )
    return {"ok": True}


@router.post(
    "/invitation_accept",
    summary="Receive an accept notification from a previously-invited recipient.",
)
async def peer_group_invitation_accept(body: InvitationAcceptBody) -> dict:
    """Flip the founder-side sender row to ``accepted``."""
    async with get_session() as session:
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == body.token)
                    & (GroupInvitationOffer.role == "sender")
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        # Idempotent: if it's already accepted, just refresh the timestamp.
        row.status = "accepted"
        row.responded_at = iso_now()
        await session.commit()

    await write_audit_event(
        action="group.invitation.accepted",
        actor="peer",
        task_id="",
        details=f"token={body.token[:8]}",
    )
    return {"ok": True}


@router.post(
    "/invitation_decline",
    summary="Receive a decline notification from a previously-invited recipient.",
)
async def peer_group_invitation_decline(body: InvitationDeclineBody) -> dict:
    """Flip the founder-side sender row to ``rejected``.

    The token (i.e. the underlying invite-link row) is **not** consumed,
    so the founder can ``/resend`` to the same peer without re-minting.
    """
    async with get_session() as session:
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == body.token)
                    & (GroupInvitationOffer.role == "sender")
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        row.status = "rejected"
        row.responded_at = iso_now()
        await session.commit()

    await write_audit_event(
        action="group.invitation.declined",
        actor="peer",
        task_id="",
        details=f"token={body.token[:8]}",
    )
    return {"ok": True}


# ---- group frame-log catch-up ---------------------------------


class GroupCatchupBody(BaseModel):
    group_id: str
    since_iso: str = ""
    limit: int = 200


@router.post(
    "/catchup",
    summary="Return frames captured after the requester's high-watermark.",
)
async def peer_group_catchup(body: GroupCatchupBody) -> dict:
    """Replay sealed envelopes from this node's frame log so an offline
    member catches up to current state.

    Any node that has been in the group can serve catch-up — the log
    is per-node-local. Caller dispatches the returned
    envelopes back through ``dispatch_inbound_frame`` + the existing
    ``FrameDedupeCache``, so re-applying a frame the caller had
    already seen is a no-op.

    Limit defaults to 200; caller paginates by re-asking with the
    ``captured_at`` of the last frame as ``since_iso``.
    """
    from nexus.runtime.group_inbox import fetch_log_since

    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")

    limit = max(1, min(1000, int(body.limit or 200)))
    frames = await fetch_log_since(body.group_id, body.since_iso or "", limit)
    return {
        "group_id": body.group_id,
        "frames": frames,
        "count": len(frames),
        "since_iso": body.since_iso or "",
    }


# ---- follow-up: direct relay-bindings pull ---------------------


class GroupRelaysPullBody(BaseModel):
    group_id: str


@router.post(
    "/relays",
    summary="Return this node's view of a group's active relay bindings.",
)
async def peer_group_relays(body: GroupRelaysPullBody) -> dict:
    """Any group member that has the bindings can serve them. Used by a
    fresh joiner to backfill the founder's relays when frame-replay
    didn't reach them (race on join, frame log truncated, etc.).
    """
    async with get_session() as session:
        group = await session.get(Group, body.group_id)
        if group is None or group.deleted_at:
            raise HTTPException(status_code=404, detail="group not found")
        rows = (
            await session.execute(
                select(GroupRelayBinding).where(
                    (GroupRelayBinding.group_id == body.group_id)
                    & (GroupRelayBinding.status != "retired")
                )
            )
        ).scalars().all()
    bindings = [
        {
            "relay_url": r.relay_url,
            "operator_pubkey": r.operator_pubkey or "",
            "status": r.status or "active",
            "state": r.state or "online",
            "last_seen_at": r.last_seen_at or "",
            "last_rtt_ms": r.last_rtt_ms,
            "host_node_id": r.host_node_id or "",
            "label": r.label or "",
            "region": r.region or "",
            "priority": int(r.priority or 0),
        }
        for r in rows
    ]
    return {
        "group_id": body.group_id,
        "bindings": bindings,
        "relay_code_fingerprint": group.relay_code_fingerprint or "",
    }


__all__ = ["router", "GRANT_TTL_SECONDS", "DEFAULT_MEMBER_ROLE", "issue_member_grant"]
