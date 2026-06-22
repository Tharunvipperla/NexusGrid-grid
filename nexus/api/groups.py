"""``/local/groups/*`` — group CRUD API.

Scope: everything the local UI on a node needs to set up and operate a
group it founds or is a member of. Peer-to-peer join handshake lives
in :mod:`nexus.api.peer`.

Every mutation is gated on two things:

1. ``verify_local_auth`` — the local API bearer token (UI must hold
   ``.nexus_local_token``).
2. A group-level permission check via
   :func:`nexus.security.group_permissions.effective_permissions`.

The local node's identity in any group is its
:func:`nexus.security.group_keys.get_local_group_pubkey`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from nexus.core import LOCAL_SETTINGS
from nexus.core.identity import get_node_identity, get_or_create_node_uuid
from nexus.security import verify_local_auth
from nexus.security import group_invite
from nexus.security.group_ecies import derive_x25519_pubkey_hex
from nexus.security.group_join_link import (
    encode_join_link,
    parse_join_link,
)
from nexus.security.group_keys import get_local_group_privkey, get_local_group_pubkey
from nexus.security.group_permissions import (
    DEFAULT_ROLES,
    PERM_GROUP_APPROVE,
    PERM_GROUP_INVITE,
    PERM_GROUP_READ,
    PERM_MEMBER_KICK,
    PERM_MEMBER_MUTE,
    PERM_RELAY_HOST,
    PERM_RELAY_SHARE_CONTENT,
    PERM_ROLE_ASSIGN,
    decode_role_permissions,
    effective_permissions,
    encode_role_permissions,
)
from nexus.storage import get_session
from nexus.storage.models import (
    Group,
    GroupGrant,
    GroupInvitationOffer,
    GroupMember,
    GroupMemberRole,
    GroupMessage,
    GroupPendingJoinRequest,
    GroupRelayBinding,
    GroupRelayCode,
    GroupRelayCodeprintProposal,
    GroupRole,
    Peer,
)
from nexus.telemetry import write_audit_event
from nexus.utils.time import iso_now


_log = logging.getLogger("nexus.api.groups")

router = APIRouter(
    prefix="/local/groups",
    tags=["Groups"],
    dependencies=[Depends(verify_local_auth)],
)


# (16.6): joiner-side incoming invitations live under a sibling
# prefix because the rows are scoped to the local node, not to a group.
invitations_router = APIRouter(
    prefix="/local/invitations",
    tags=["Group Invitations"],
    dependencies=[Depends(verify_local_auth)],
)


# Default roles whose existence is load-bearing for the system.
# DELETE is rejected against these names.
_PROTECTED_ROLE_NAMES = frozenset(DEFAULT_ROLES.keys())


# ---- request bodies -----------------------------------------------------


_VALID_PRIVACY_MODES = ("open", "private")


class CreateGroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # (16.2): join policy. ``open`` keeps the Wave-15 flow.
    # ``private`` routes joins into an admin pending queue.
    privacy_mode: str = Field(default="open")
    # Which relays this group is bound to. 1..N entries; an
    # empty list falls back to the local node's configured relay
    # (``relay_server_url`` setting). Members fan out publishes to all
    # Bindings (+); for the joiner just uses the first
    # reachable one. Operators must hold ``relay:host`` to register a
    # Binding starting in unenforced for now.
    relay_urls: list[str] = Field(default_factory=list)
    # Optional hard cap on group membership. 0 = unlimited.
    max_members: int = Field(default=0, ge=0, le=100000)
    # "full" (Groups screen) or "chat" (lightweight message
    # group surfaced in Messages). Same machinery either way.
    kind: str = Field(default="full")


class SetPrivacyModeBody(BaseModel):
    privacy_mode: str = Field(min_length=1)


class AddRelayBody(BaseModel):
    # Bind an additional relay to an existing group.
    relay_url: str = Field(min_length=1, max_length=512)


class RelayConfigBody(BaseModel):
    # Adjust a relay binding's operator-set metadata. Only the
    # provided fields are changed (None = leave as-is).
    relay_url: str = Field(min_length=1, max_length=512)
    label: str | None = Field(default=None, max_length=60)
    region: str | None = Field(default=None, max_length=40)
    priority: int | None = Field(default=None, ge=-100, le=100)


class MintInviteBody(BaseModel):
    slot_cap: int = Field(ge=0, default=0)


class ReopenInviteBody(BaseModel):
    new_slot_cap: Optional[int] = Field(default=None, ge=0)


class UpsertRoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class AssignRolesBody(BaseModel):
    roles: list[str] = Field(default_factory=list)


class JoinGroupBody(BaseModel):
    admin_address: str = Field(default="")
    # Invite_token optional when signed_invite_hex carries a v=2
    # signed envelope from a secure link.
    invite_token: str = Field(default="")
    signed_invite_hex: str = Field(default="")
    # (16.2): optional fields used by the private-mode flow.
    # ``message`` is shown to the admin in the pending-request row.
    # ``joiner_address`` lets the admin call back later (16.4) to
    # deliver the approval / rejection without the joiner having to
    # poll. Both ignored by the open-mode path.
    message: str = Field(default="", max_length=512)
    joiner_address: str = Field(default="", max_length=256)
    # The admin's node UUID, from the join link. Lets the join
    # request route over the relay when ``admin_address`` is unreachable
    # (the admin is behind NAT).
    admin_node_id: str = Field(default="", max_length=128)
    # Link-carried relays + grid_key. When direct HTTP and the
    # joiner's own STATE.relay_ws both fail, we open transient WS
    # connections to these relays authenticated with this grid_key.
    relay_urls: list[str] = Field(default_factory=list)
    grid_key: str = Field(default="", max_length=128)


class RejectPendingBody(BaseModel):
    # (16.3): optional reason shown back to the joiner.
    reason: str = Field(default="", max_length=512)


class ProbeGroupBody(BaseModel):
    # Pre-join probe so the UI can branch on privacy_mode
    # before actually submitting the join request.
    admin_address: str = Field(min_length=1)
    invite_token: str = Field(min_length=1)
    # Admin node UUID for the relay fallback (see JoinGroupBody).
    admin_node_id: str = Field(default="", max_length=128)
    # Link-carried relays + grid_key for the transient-WS
    # fallback (see JoinGroupBody).
    relay_urls: list[str] = Field(default_factory=list)
    grid_key: str = Field(default="", max_length=128)


class InviteFriendsBody(BaseModel):
    # (16.5): IPs (or nexus_<uuid> identifiers) of trusted peers
    # to invite. Each one gets its own fresh single-slot invite token.
    peer_ips: list[str] = Field(default_factory=list)


class JoinLinkBuildBody(BaseModel):
    # Server-side encoder for the join-link blob, so the UI
    # doesn't need to base64+JSON-encode itself. Caller supplies an
    # invite token they have already minted; this endpoint just looks
    # up the group's relay bindings + glues them with the token.
    invite_token: str = Field(min_length=1)


class JoinLinkParseBody(BaseModel):
    # Joiner-side decode of a pasted ``nxg://join#…`` blob.
    # Validation lives in :mod:`nexus.security.group_join_link`.
    join_link: str = Field(min_length=1)


# ---- helpers ------------------------------------------------------------


async def _require_perm(session, group_id: str, perm: str) -> str:
    """Return caller's group pubkey; raise 403 if it lacks ``perm``."""
    me = get_local_group_pubkey()
    perms = await effective_permissions(session, group_id, me)
    if perm not in perms:
        raise HTTPException(
            status_code=403,
            detail=f"caller lacks permission '{perm}' in group {group_id}",
        )
    return me


async def _require_group_exists(session, group_id: str) -> Group:
    row = await session.get(Group, group_id)
    if row is None or row.deleted_at:
        raise HTTPException(status_code=404, detail="group not found")
    return row


def _role_summary(role: GroupRole) -> dict:
    return {
        "name": role.name,
        "permissions": list(decode_role_permissions(role.permissions_json)),
        "created_at": role.created_at or "",
        "updated_at": role.updated_at or "",
    }


def _invite_summary(invite: group_invite.InviteLink) -> dict:
    return {
        "token": invite.token,
        "slot_cap": invite.slot_cap,
        "slots_filled": invite.slots_filled,
        "active": invite.active,
        "created_by_pubkey": invite.created_by_pubkey,
        "created_at": invite.created_at,
        "rotated_at": invite.rotated_at,
    }


def _peer_display_label(peer: Peer) -> str:
    """Best-effort human-readable label for a peer row.

    Display order: ``display_name`` > ``resolved_ip`` > short ``nexus_<id>``
    so the Sent Invitations table doesn't show the bare 32-hex identifier.
    """
    if (peer.display_name or "").strip():
        return peer.display_name
    if (peer.resolved_ip or "").strip():
        return peer.resolved_ip
    raw = peer.ip or ""
    if raw.startswith("nexus_") and len(raw) > 14:
        return raw[:14] + "…"
    return raw


def _offer_summary(row: GroupInvitationOffer) -> dict:
    return {
        "token": row.token,
        "role": row.role,
        "group_id": row.group_id,
        "group_name": row.group_name or "",
        "founder_pubkey": row.founder_pubkey or "",
        "founder_address": row.founder_address or "",
        "target_peer_label": row.target_peer_label or "",
        "status": row.status or "pending",
        "created_at": row.created_at or "",
        "responded_at": row.responded_at or "",
    }


# ---- endpoints ----------------------------------------------------------


@router.post("", summary="Create a new group (this node becomes founder)")
async def create_group(body: CreateGroupBody) -> dict:
    if body.privacy_mode not in _VALID_PRIVACY_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"privacy_mode must be one of {_VALID_PRIVACY_MODES}",
        )
    if body.kind not in ("full", "chat"):
        raise HTTPException(
            status_code=400, detail="kind must be 'full' or 'chat'"
        )
    me = get_local_group_pubkey()
    group_id = str(uuid.uuid4())
    now = iso_now()

    async with get_session() as session:
        session.add(
            Group(
                id=group_id,
                name=body.name,
                founder_pubkey=me,
                created_at=now,
                deleted_at="",
                privacy_mode=body.privacy_mode,
                max_members=int(body.max_members or 0),
                kind=body.kind,
            )
        )
        # Default roles.
        for role_name, perms in DEFAULT_ROLES.items():
            session.add(
                GroupRole(
                    group_id=group_id,
                    name=role_name,
                    permissions_json=encode_role_permissions(perms),
                    created_at=now,
                    updated_at=now,
                )
            )
        # Founder is automatically a member with the founder role.
        my_name = str(LOCAL_SETTINGS.get("user_display_name") or "")
        my_x25519 = derive_x25519_pubkey_hex(get_local_group_privkey())
        session.add(
            GroupMember(
                group_id=group_id,
                pubkey=me,
                joined_at=now,
                last_heartbeat_at="",
                display_name=my_name,
                member_x25519_pub=my_x25519,
                node_id=get_or_create_node_uuid(),
            )
        )
        session.add(
            GroupMemberRole(
                group_id=group_id,
                member_pubkey=me,
                role_name="founder",
                assigned_by_pubkey=me,
                assigned_at=now,
            )
        )

        # Bind the group to its relay(s). Empty list falls back
        # to this node's configured relay. Dedupe + strip empty strings.
        configured_relay = str(
            LOCAL_SETTINGS.get("relay_server_url", "") or ""
        ).strip()
        urls = [u.strip() for u in (body.relay_urls or []) if u and u.strip()]
        if not urls and configured_relay:
            urls = [configured_relay]
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            session.add(
                GroupRelayBinding(
                    group_id=group_id,
                    relay_url=url,
                    operator_pubkey=me,
                    registered_at=now,
                    last_seen_at="",
                    status="active",
                )
            )
        await session.commit()

    # Tell live relay subs about the new group so beacons start
    # routing through its bucket immediately, no reconnect needed.
    from nexus.networking.relay_client import push_grid_key_update
    from nexus.security.grid_keys import derive_group_grid_key
    new_key = derive_group_grid_key(group_id)
    if new_key:
        await push_grid_key_update(added=[new_key])

    await write_audit_event(
        action="group.create",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} name={body.name!r} "
            f"privacy_mode={body.privacy_mode} relays={len(seen)}"
        ),
    )
    return {
        "id": group_id,
        "name": body.name,
        "founder_pubkey": me,
        "created_at": now,
        "privacy_mode": body.privacy_mode,
        "relay_urls": sorted(seen),
    }


@router.get("", summary="List groups this node is a member of")
async def list_groups() -> dict:
    me = get_local_group_pubkey()
    async with get_session() as session:
        member_rows = (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.pubkey == me)
            )
        ).fetchall()
        group_ids = [row[0] for row in member_rows]
        groups = []
        for gid in group_ids:
            g = await session.get(Group, gid)
            if g is None or g.deleted_at:
                continue
            my_roles = (
                await session.execute(
                    select(GroupMemberRole.role_name).where(
                        (GroupMemberRole.group_id == gid)
                        & (GroupMemberRole.member_pubkey == me)
                    )
                )
            ).fetchall()
            relay_rows = (
                await session.execute(
                    select(
                        GroupRelayBinding.status,
                        GroupRelayBinding.last_rtt_ms,
                    ).where(
                        (GroupRelayBinding.group_id == gid)
                        & (GroupRelayBinding.status != "retired")
                    )
                )
            ).fetchall()
            relay_statuses = [row[0] for row in relay_rows]
            # Fastest reachable relay RTT for this group (the probe
            # loop keeps last_rtt_ms fresh; None means the relay didn't answer
            # the last probe). Lets My Groups show "how fast" each group is, or
            # "offline" when bound relays exist but none answered.
            _rtts = [row[1] for row in relay_rows if row[1] is not None]
            relay_best_rtt_ms = min(_rtts) if _rtts else None
            # Message count for the Messages hub unread badge.
            msg_count = (
                await session.execute(
                    select(func.count())
                    .select_from(GroupMessage)
                    .where(GroupMessage.group_id == gid)
                )
            ).scalar() or 0
            groups.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "founder_pubkey": g.founder_pubkey,
                    "created_at": g.created_at or "",
                    "privacy_mode": g.privacy_mode or "open",
                    "avatar": g.avatar or "",
                    "kind": g.kind or "full",
                    "my_roles": sorted({r[0] for r in my_roles}),
                    "relay_count": len(relay_statuses),
                    "relay_active_count": sum(
                        1 for s in relay_statuses if s == "active"
                    ),
                    "relay_best_rtt_ms": relay_best_rtt_ms,
                    "message_count": int(msg_count),
                    # Local pause flag (1=paused).
                    "paused": bool(g.paused or 0),
                }
            )
    return {"groups": groups}


@router.get("/{group_id}", summary="Group detail")
async def get_group_detail(group_id: str) -> dict:
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)

        member_rows = (
            await session.execute(
                select(GroupMember).where(GroupMember.group_id == group_id)
            )
        ).scalars().all()
        role_rows = (
            await session.execute(
                select(GroupRole).where(GroupRole.group_id == group_id)
            )
        ).scalars().all()
        member_role_rows = (
            await session.execute(
                select(GroupMemberRole).where(
                    GroupMemberRole.group_id == group_id
                )
            )
        ).scalars().all()

        roles_by_member: dict[str, list[str]] = {}
        for mr in member_role_rows:
            roles_by_member.setdefault(mr.member_pubkey, []).append(mr.role_name)

        members = [
            {
                "pubkey": m.pubkey,
                "joined_at": m.joined_at or "",
                "display_name": m.display_name or "",
                "peer_address": m.peer_address or "",
                "node_id": m.node_id or "",
                "muted": bool(m.muted),
                "last_seen_at": m.last_seen_at or "",
                "roles": sorted(roles_by_member.get(m.pubkey, [])),
            }
            for m in member_rows
        ]
        roles = [_role_summary(r) for r in role_rows]

        me = get_local_group_pubkey()
        my_permissions = sorted(await effective_permissions(session, group_id, me))

        relay_rows = (
            await session.execute(
                select(GroupRelayBinding).where(
                    (GroupRelayBinding.group_id == group_id)
                    & (GroupRelayBinding.status != "retired")
                )
            )
        ).scalars().all()
        relays = [
            {
                "relay_url": r.relay_url,
                "operator_pubkey": r.operator_pubkey or "",
                "status": r.status or "active",
                "last_seen_at": r.last_seen_at or "",
                # Last-measured RTT (ms) so the UI doesn't
                # need a second /local/relay/latency call.
                "last_rtt_ms": r.last_rtt_ms,
                # Per-binding state machine + host attribution
                # so the Relays subtab can render state badges and show
                # which relay is offline.
                "state": r.state or "online",
                "last_state_change_at": r.last_state_change_at or "",
                "host_node_id": r.host_node_id or "",
                "consecutive_probe_failures": int(
                    r.consecutive_probe_failures or 0
                ),
                # Operator-adjustable metadata.
                "label": r.label or "",
                "region": r.region or "",
                "priority": int(r.priority or 0),
                # Consensual content-share authorization (visible to
                # every member). Default 0 = relay is E2E-blind.
                "content_share": bool(getattr(r, "content_share", 0) or 0),
                "content_share_by": getattr(r, "content_share_by", "") or "",
                "content_share_at": getattr(r, "content_share_at", "") or "",
            }
            for r in relay_rows
        ]

    return {
        "id": g.id,
        "name": g.name,
        "founder_pubkey": g.founder_pubkey,
        "created_at": g.created_at or "",
        "privacy_mode": g.privacy_mode or "open",
        "avatar": g.avatar or "",
        "kind": g.kind or "full",
        "paused": bool(g.paused or 0),
        "max_members": int(g.max_members or 0),
        "members": members,
        "roles": roles,
        "my_permissions": my_permissions,
        # Surface caller's group pubkey so the UI can decide
        # founder-vs-admin freeze paths without a second roundtrip.
        "my_pubkey": me,
        "relays": relays,
        # Frozen relay code fingerprint (empty = unset).
        "relay_code_fingerprint": g.relay_code_fingerprint or "",
    }


# ---- relay-binding reachability ------------------------------


async def _probe_relay_url(url: str) -> tuple[bool, int | None]:
    """Probe ``url`` once. Return ``(reachable, rtt_ms_or_None)``.

    extended to capture wall-clock RTT so latency-aware
    routing can pick the lowest-RTT relay.

    Relay URLs are WebSocket URLs; we probe over plain HTTP on the same
    host (``wss``->``https``, ``ws``->``http``). Any HTTP response — even
    a 404 or 426 Upgrade-Required — proves the server is up; only a
    connect error / timeout counts as unreachable.
    """
    probe = (url or "").strip()
    if not probe:
        return False, None
    if probe.startswith("wss://"):
        probe = "https://" + probe[len("wss://"):]
    elif probe.startswith("ws://"):
        probe = "http://" + probe[len("ws://"):]
    elif not probe.startswith(("http://", "https://")):
        probe = "https://" + probe
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(verify=False, timeout=4.0) as client:
            resp = await client.get(probe)
        rtt_ms = int((time.perf_counter() - start) * 1000)
        # Follow-up: cloudflared keeps proxying after the relay's
        # uvicorn thread is stopped, returning 502 / 503 / 504 — without
        # this guard the probe would report "reachable" for a dead relay
        # and the state machine would never flip to offline. Any 5xx
        # therefore counts as unreachable; 2xx/3xx/4xx (including the WS
        # 426 Upgrade Required) still prove the relay's there.
        if 500 <= resp.status_code <= 599:
            return False, None
        return True, rtt_ms
    except httpx.HTTPError:
        return False, None


@router.post(
    "/{group_id}/relays/probe",
    summary="Probe each relay binding's reachability",
)
async def probe_group_relays(group_id: str) -> dict:
    """Live-probe every relay binding and persist the result.

    Updates each :class:`GroupRelayBinding`'s ``status``
    (``active`` / ``unreachable``) and, on success, ``last_seen_at``.
    Any group member may probe — relay health is not a secret.
    """
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        rows = (
            await session.execute(
                select(GroupRelayBinding).where(
                    (GroupRelayBinding.group_id == group_id)
                    & (GroupRelayBinding.status != "retired")
                )
            )
        ).scalars().all()
        probe_results = await asyncio.gather(
            *(_probe_relay_url(r.relay_url) for r in rows)
        )
        now = iso_now()
        relays = []
        # Follow-up: probe-driven state-machine transitions so the
        # UI badge reflects what the manual Probe button just measured,
        # without waiting ~30s for the background latency loop to also
        # cross the offline threshold.
        from nexus.runtime import relay_latency, relay_state
        from nexus.runtime.relay_latency import OFFLINE_FAILURE_THRESHOLD
        for row, (reachable, rtt_ms) in zip(rows, probe_results):
            row.status = "active" if reachable else "unreachable"
            if reachable:
                row.last_seen_at = now
                row.last_rtt_ms = rtt_ms
                # Walk the state machine forward through the recovery path.
                if row.state == relay_state.STATE_OFFLINE:
                    await relay_state.transition(
                        row, relay_state.STATE_RECONNECTING,
                        reason="manual probe ok",
                    )
                if row.state == relay_state.STATE_RECONNECTING:
                    await relay_state.transition(
                        row, relay_state.STATE_SYNCING,
                        reason="manual probe ok",
                    )
                if row.state == relay_state.STATE_SYNCING:
                    await relay_state.transition(
                        row, relay_state.STATE_ONLINE,
                        reason="manual probe ok",
                    )
            else:
                # Unreachable — drop the stale RTT so the UI doesn't show
                # a latency for a relay that isn't answering.
                row.last_rtt_ms = None
                row.consecutive_probe_failures = (
                    int(row.consecutive_probe_failures or 0) + 1
                )
                # Manual Probe is an explicit user action — "go check it
                # right now" — so any failure flips an online binding
                # straight to offline. (The background latency loop still
                # uses the 3-strike threshold to ignore transient blips.)
                if row.state == relay_state.STATE_ONLINE:
                    await relay_state.transition(
                        row, relay_state.STATE_OFFLINE,
                        reason="manual probe failed",
                    )
            relay_latency.record(row.relay_url, rtt_ms)
            relays.append(
                {
                    "relay_url": row.relay_url,
                    "operator_pubkey": row.operator_pubkey or "",
                    "status": row.status,
                    "last_seen_at": row.last_seen_at or "",
                    "last_rtt_ms": row.last_rtt_ms,
                    "reachable": reachable,
                    # State-machine fields (read-only — probe
                    # endpoint doesn't drive the state machine itself;
                    # that's the latency loop's job).
                    "state": row.state or "online",
                    "last_state_change_at": row.last_state_change_at or "",
                    "host_node_id": row.host_node_id or "",
                    "consecutive_probe_failures": int(
                        row.consecutive_probe_failures or 0
                    ),
                }
            )
        await session.commit()
    # A binding that just flipped offline needs no special signal: group
    # traffic auto-routes through the surviving bound relays via the
    # publish_frame fan-out. The UI just shows which relay is offline.
    return {"group_id": group_id, "relays": relays}


@router.post(
    "/{group_id}/relays",
    summary="Bind a relay to a group",
)
async def add_group_relay(group_id: str, body: AddRelayBody) -> dict:
    """Bind an additional relay URL to an existing group.

    Idempotent: re-adding a live binding is a no-op; re-adding a
    previously-removed (``retired``) one reactivates it. Gated on
    ``relay:host`` — only relay operators may register a binding.
    """
    url = body.relay_url.strip()
    if "://" not in url:
        raise HTTPException(
            status_code=400,
            detail="relay_url must include a scheme (wss:// or ws://)",
        )
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_HOST)
        # /63: resolve the ACTUAL code fingerprint of the relay being
        # bound, when it's one of this node's running relays (matched by port).
        bound_fp = ""
        try:
            from nexus.runtime import local_relay
            bound_fp = local_relay.fingerprint_for_url(url)
        except Exception:
            pass
        frozen_fp = g.relay_code_fingerprint or ""
        # /61/63: the founder's relay defines the group's canonical relay
        # code. Auto-freeze on the founder's first bind to whatever relay code
        # is actually bound (custom plugin or bundled), so members who later
        # host a relay for the group must match it.
        if (g.founder_pubkey or "") == me and not frozen_fp:
            if not bound_fp:
                try:
                    from nexus.runtime.relay_codeprint import CURRENT_FINGERPRINT
                    bound_fp = CURRENT_FINGERPRINT
                except Exception:
                    bound_fp = ""
            if bound_fp:
                g.relay_code_fingerprint = bound_fp
        # Reject binding a relay whose code doesn't match the group's
        # frozen build. Only enforced when we could resolve the relay's print
        # locally (port match) — a remote/tunnel URL we can't attest is allowed
        # through unchanged (no regression).
        elif frozen_fp and bound_fp and bound_fp != frozen_fp:
            raise HTTPException(
                status_code=409,
                detail=(f"relay code fingerprint mismatch: this relay runs "
                        f"{bound_fp[:12]}…, but the group requires "
                        f"{frozen_fp[:12]}… — run the group's relay build to host it"),
            )
        now = iso_now()
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None:
            session.add(
                GroupRelayBinding(
                    group_id=group_id,
                    relay_url=url,
                    operator_pubkey=me,
                    registered_at=now,
                    last_seen_at="",
                    status="active",
                )
            )
        elif row.status == "retired":
            row.status = "active"
            row.operator_pubkey = me
            row.registered_at = now
        # Replicate the binding so every member's relay list
        # converges (otherwise GroupRelayBinding is node-local state).
        from nexus.runtime.group_inbox import publish_relay_update

        await publish_relay_update(session, group_id, url, "add", me)
        await session.commit()

    await write_audit_event(
        action="group.relay.bound",
        actor="local",
        task_id="",
        details=f"group_id={group_id} relay_url={url}",
    )
    return {"group_id": group_id, "relay_url": url, "status": "active"}


@router.delete(
    "/{group_id}/relays",
    summary="Unbind a relay from a group",
)
async def remove_group_relay(group_id: str, relay_url: str) -> dict:
    """Unbind a relay (soft-delete to ``retired``). Gated on ``relay:host``."""
    url = (relay_url or "").strip()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_HOST)
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None or row.status == "retired":
            raise HTTPException(
                status_code=404, detail="relay binding not found"
            )
        row.status = "retired"
        # Replicate the removal to every member.
        from nexus.runtime.group_inbox import publish_relay_update

        await publish_relay_update(session, group_id, url, "remove", me)
        await session.commit()

    await write_audit_event(
        action="group.relay.unbound",
        actor="local",
        task_id="",
        details=f"group_id={group_id} relay_url={url}",
    )
    return {"group_id": group_id, "relay_url": url, "status": "retired"}


@router.post(
    "/{group_id}/relays/config",
    summary="Adjust a relay binding's label / region / priority",
)
async def configure_group_relay(group_id: str, body: RelayConfigBody) -> dict:
    """Set operator-adjustable metadata on a relay binding.

    Gated on ``relay:host``. Only the fields present in the request are
    changed. The delta replicates to every member via a ``relay.update``
    ``config`` frame so the group's view converges.
    """
    url = body.relay_url.strip()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_HOST)
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None or row.status == "retired":
            raise HTTPException(
                status_code=404, detail="relay binding not found"
            )
        meta: dict = {}
        if body.label is not None:
            row.label = body.label.strip()
            meta["label"] = row.label
        if body.region is not None:
            row.region = body.region.strip()
            meta["region"] = row.region
        if body.priority is not None:
            row.priority = int(body.priority)
            meta["priority"] = row.priority
        result = {
            "group_id": group_id,
            "relay_url": url,
            "label": row.label or "",
            "region": row.region or "",
            "priority": int(row.priority or 0),
        }
        if meta:
            from nexus.runtime.group_inbox import publish_relay_update

            await publish_relay_update(
                session, group_id, url, "config", me, meta=meta
            )
        await session.commit()

    return result


# ---- consensual relay content-share ---------------------------


class RelayContentShareBody(BaseModel):
    relay_url: str = Field(..., min_length=4)


@router.post(
    "/{group_id}/relays/content_share",
    summary="Authorize a relay to read group content (share the symkey)",
)
async def share_group_relay_content(
    group_id: str, body: RelayContentShareBody
) -> dict:
    """Authorize a bound relay to read group content.

    Relays are E2E-blind by default. This makes a *visible* group decision to
    let a specific relay read content; it is gated on ``relay:share_content``
    (founder/admin) — a relay operator can NOT self-authorize. The decision
    replicates to every member via a ``relay.update`` ``content_share`` frame.
    """
    url = body.relay_url.strip()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_SHARE_CONTENT)
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None or row.status == "retired":
            raise HTTPException(status_code=404, detail="relay binding not found")
        row.content_share = 1
        row.content_share_by = me
        row.content_share_at = iso_now()
        from nexus.runtime.group_inbox import publish_relay_update

        await publish_relay_update(
            session, group_id, url, "content_share", me, meta={"share_by": me}
        )
        await session.commit()

    await write_audit_event(
        action="group.relay.content_share",
        actor="local",
        task_id="",
        details=f"group_id={group_id} relay_url={url}",
    )
    return {"group_id": group_id, "relay_url": url, "content_share": True}


@router.post(
    "/{group_id}/relays/content_revoke",
    summary="Revoke a relay's authorization to read group content",
)
async def revoke_group_relay_content(
    group_id: str, body: RelayContentShareBody
) -> dict:
    """Revoke a relay's content-read authorization. Gated on
    ``relay:share_content``; replicates a ``content_revoke`` frame."""
    url = body.relay_url.strip()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_SHARE_CONTENT)
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None or row.status == "retired":
            raise HTTPException(status_code=404, detail="relay binding not found")
        row.content_share = 0
        row.content_share_by = ""
        row.content_share_at = ""
        from nexus.runtime.group_inbox import publish_relay_update

        await publish_relay_update(
            session, group_id, url, "content_revoke", me
        )
        await session.commit()

    await write_audit_event(
        action="group.relay.content_revoke",
        actor="local",
        task_id="",
        details=f"group_id={group_id} relay_url={url}",
    )
    return {"group_id": group_id, "relay_url": url, "content_share": False}


@router.get(
    "/{group_id}/relay_content_key",
    summary="Release the group symkey to an authorized content-aware relay",
)
async def get_group_relay_content_key(group_id: str, relay_url: str) -> dict:
    """Return the group symkey (base64) for a relay the group has authorized
    to read content — the *only* sanctioned path a relay obtains the key.

    A content-aware relay plugin running on this host fetches the key here at
    startup. The endpoint releases it ONLY when an active ``content_share``
    authorization exists for *relay_url*; otherwise 403. This keeps relays
    E2E-blind by default and makes content access a recorded group decision.
    """
    import base64

    url = (relay_url or "").strip()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        row = await session.get(GroupRelayBinding, (group_id, url))
        if row is None or row.status == "retired":
            raise HTTPException(status_code=404, detail="relay binding not found")
        if not (getattr(row, "content_share", 0) or 0):
            raise HTTPException(
                status_code=403,
                detail="relay is not authorized to read content",
            )
        from nexus.runtime.group_inbox import _local_symkey

        symkey = await _local_symkey(session, group_id)
        if symkey is None:
            raise HTTPException(
                status_code=409, detail="no group symkey on this node yet"
            )
    return {
        "group_id": group_id,
        "relay_url": url,
        "symkey_b64": base64.b64encode(symkey).decode("ascii"),
        "authorized_by": row.content_share_by or "",
    }


# ---- group chat -----------------------------------------------


class SendMessageBody(BaseModel):
    body: str = Field(default="", max_length=4000)
    # Reply/quote a specific message.
    reply_to: str = Field(default="", max_length=64)
    reply_snippet: str = Field(default="", max_length=200)
    reply_sender: str = Field(default="", max_length=64)
    reply_to_pubkey: str = Field(default="", max_length=128)
    # Inline attachment (≤5 MB). attach_data is base64.
    attach_name: str = Field(default="", max_length=255)
    attach_mime: str = Field(default="", max_length=128)
    attach_data: str = Field(default="")  # base64


class MuteBody(BaseModel):
    muted: bool = True


def _message_summary(m: GroupMessage) -> dict:
    return {
        "msg_id": m.msg_id,
        "sender_pubkey": m.sender_pubkey or "",
        "sender_name": m.sender_name or "",
        "body": "" if m.deleted else (m.body or ""),
        "sent_at": m.sent_at or "",
        "deleted": bool(m.deleted),
        "reply_to": m.reply_to or "",
        "reply_snippet": m.reply_snippet or "",
        "reply_sender": m.reply_sender or "",
        "attach_kind": m.attach_kind or "",
        "attach_name": m.attach_name or "",
        "attach_mime": m.attach_mime or "",
        "attach_size": int(m.attach_size or 0),
    }


_INLINE_ATTACH_MAX = 5 * 1024 * 1024  # 5 MB


@router.post("/{group_id}/messages", summary="Send a group chat message")
async def send_group_message(group_id: str, body: SendMessageBody) -> dict:
    """Post a message to the group. Any member may send unless muted."""
    import base64 as _b64
    import uuid as _uuid

    if not (body.body or "").strip() and not body.attach_data:
        raise HTTPException(status_code=422, detail="message or attachment required")
    attach_kind = ""
    attach_size = 0
    _foreign_raw = None
    if body.attach_data:
        try:
            raw = _b64.b64decode(body.attach_data)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid attachment encoding")
        attach_size = len(raw)
        if attach_size > _INLINE_ATTACH_MAX:
            # Too big to ride the frame — host it on the sender and
            # let recipients pull it (sealed per-requester) on demand.
            from nexus.runtime.chat_attachments import MAX_ATTACH_BYTES
            if attach_size > MAX_ATTACH_BYTES:
                raise HTTPException(status_code=413, detail="attachment too large (max 100MB)")
            attach_kind = "foreign"
            _foreign_raw = raw
        else:
            attach_kind = "inline"

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_READ)
        my_row = await session.get(GroupMember, (group_id, me))
        if my_row is not None and int(my_row.muted or 0):
            raise HTTPException(status_code=403, detail="you are muted in this group")
        msg_id = _uuid.uuid4().hex
        sent_at = iso_now()
        sender_name = str(LOCAL_SETTINGS.get("user_display_name") or "")
        # Foreign attachments don't ride the DB row or the frame — the sender
        # hosts the plaintext on disk and serves it sealed on pull.
        if attach_kind == "foreign":
            from nexus.runtime.chat_attachments import store_blob
            store_blob(msg_id, _foreign_raw)
        session.add(GroupMessage(
            group_id=group_id,
            msg_id=msg_id,
            sender_pubkey=me,
            sender_name=sender_name,
            body=body.body,
            sent_at=sent_at,
            received_at=sent_at,
            reply_to=body.reply_to or "",
            reply_snippet=body.reply_snippet or "",
            reply_sender=body.reply_sender or "",
            attach_kind=attach_kind,
            attach_name=body.attach_name or "",
            attach_mime=body.attach_mime or "",
            attach_size=attach_size,
            attach_data="" if attach_kind == "foreign" else (body.attach_data or ""),
        ))
        await session.commit()

    # Fan the message out in the background so the sender's UI returns
    # instantly (the relay→direct fan-out can take a moment per member).
    payload = {
        "group_id": group_id,
        "msg_id": msg_id,
        "body": body.body,
        "sender_name": sender_name,
        "sent_at": sent_at,
        "reply_to": body.reply_to or "",
        "reply_snippet": body.reply_snippet or "",
        "reply_sender": body.reply_sender or "",
        "reply_to_pubkey": body.reply_to_pubkey or "",
        "attach_kind": attach_kind,
        "attach_name": body.attach_name or "",
        "attach_mime": body.attach_mime or "",
        "attach_size": attach_size,
        # Foreign rides only a reference; recipients pull the bytes from us.
        "attach_data": "" if attach_kind == "foreign" else (body.attach_data or ""),
    }

    async def _deliver() -> None:
        from nexus.runtime.group_inbox import publish_chat_message

        async with get_session() as s2:
            await publish_chat_message(s2, group_id, payload)
            await s2.commit()

    asyncio.create_task(_deliver())
    return {"group_id": group_id, "msg_id": msg_id, "sent_at": sent_at}


@router.get("/{group_id}/messages", summary="List group chat messages")
async def list_group_messages(
    group_id: str, since: str = "", limit: int = 100
) -> dict:
    """Return up to ``limit`` messages, oldest-first, optionally after ``since``."""
    limit = max(1, min(int(limit or 100), 500))
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        q = select(GroupMessage).where(GroupMessage.group_id == group_id)
        if since:
            q = q.where(GroupMessage.sent_at > since)
        rows = (
            await session.execute(
                q.order_by(GroupMessage.sent_at.desc()).limit(limit)
            )
        ).scalars().all()
    rows = list(reversed(rows))  # oldest-first for display
    return {"group_id": group_id, "messages": [_message_summary(m) for m in rows]}


@router.get("/{group_id}/presence", summary="Member liveness (last-seen) snapshot")
async def group_presence(group_id: str) -> dict:
    """Return each member's ``last_seen_at`` so the UI can refresh online dots
    on a timer without re-pulling the whole group detail."""
    from nexus.runtime.group_presence import PRESENCE_ONLINE_WINDOW_S

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        rows = (
            await session.execute(
                select(GroupMember).where(GroupMember.group_id == group_id)
            )
        ).scalars().all()
    return {
        "group_id": group_id,
        "online_window_s": PRESENCE_ONLINE_WINDOW_S,
        "members": [
            {"pubkey": m.pubkey, "last_seen_at": m.last_seen_at or ""}
            for m in rows
        ],
    }


@router.get("/{group_id}/pool_stats", summary="Group compute-pool usage per member")
async def group_pool_stats(group_id: str) -> dict:
    """Each member's contributed/consumed task counts, visible to
    the whole group. Members with no recorded activity show as zeros."""
    from nexus.storage.models import GroupComputeStat

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        members = (
            await session.execute(
                select(GroupMember).where(GroupMember.group_id == group_id)
            )
        ).scalars().all()
        stats = (
            await session.execute(
                select(GroupComputeStat).where(
                    GroupComputeStat.group_id == group_id
                )
            )
        ).scalars().all()
    by_pubkey = {s.member_pubkey: s for s in stats}
    return {
        "group_id": group_id,
        "members": [
            {
                "pubkey": m.pubkey,
                "display_name": m.display_name or "",
                "tasks_contributed": int(
                    getattr(by_pubkey.get(m.pubkey), "tasks_contributed", 0) or 0
                ),
                "tasks_consumed": int(
                    getattr(by_pubkey.get(m.pubkey), "tasks_consumed", 0) or 0
                ),
            }
            for m in members
        ],
    }


@router.get("/{group_id}/pool_usage", summary="This node's pool-usage history for a group")
async def group_pool_usage(group_id: str, range: str = "7d") -> dict:
    """Time-bucketed pool usage (this node) for *group_id*."""
    from nexus.runtime.group_compute_telemetry import fetch_buckets, range_to_since

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
    buckets = await fetch_buckets(group_id, range_to_since(range))
    return {"group_id": group_id, "range": range, "buckets": buckets}


@router.get("/{group_id}/pool_usage/export", summary="Export pool-usage buckets (CSV/JSON)")
async def group_pool_usage_export(group_id: str, format: str = "json"):
    from fastapi import Response

    from nexus.runtime.group_compute_telemetry import buckets_csv, fetch_buckets

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
    rows = await fetch_buckets(group_id)
    if format == "csv":
        return Response(content=buckets_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="pool_{group_id[:8]}.csv"'})
    return {"group_id": group_id, "buckets": rows}


@router.get(
    "/{group_id}/messages/{msg_id}/attachment",
    summary="Download a message's inline attachment",
)
async def get_group_attachment(group_id: str, msg_id: str):
    import base64 as _b64

    from fastapi import Response

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        m = await session.get(GroupMessage, (group_id, msg_id))
        if m is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        if (m.attach_kind or "") == "foreign":
            # Sender-hosted: served from local disk once we have the bytes
            # (the sender always; a recipient after it has pulled them).
            from nexus.runtime.chat_attachments import load_blob
            raw = load_blob(msg_id)
            if raw is None:
                raise HTTPException(status_code=425, detail="still downloading")
        elif m.attach_data or "":
            raw = _b64.b64decode(m.attach_data)
        else:
            raise HTTPException(status_code=404, detail="attachment not found")
    return Response(
        content=raw,
        media_type=m.attach_mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{(m.attach_name or "file")}"'
        },
    )


@router.delete("/{group_id}/messages/{msg_id}", summary="Delete a group message")
async def delete_group_message(group_id: str, msg_id: str) -> dict:
    """Delete a message. Author may delete own; ``member:kick`` deletes any."""
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_READ)
        row = await session.get(GroupMessage, (group_id, msg_id))
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        # Only the author may delete their own message.
        if row.sender_pubkey != me:
            raise HTTPException(status_code=403, detail="you can only delete your own messages")
        row.deleted = 1
        row.body = ""
        await session.commit()

    async def _publish() -> None:
        from nexus.runtime.group_inbox import publish_chat_delete

        async with get_session() as s2:
            await publish_chat_delete(s2, group_id, msg_id)
            await s2.commit()

    asyncio.create_task(_publish())
    return {"group_id": group_id, "msg_id": msg_id, "deleted": True}


@router.post(
    "/{group_id}/members/{member_pubkey}/mute",
    summary="Mute or unmute a member's group chat",
)
async def mute_group_member(
    group_id: str, member_pubkey: str, body: MuteBody
) -> dict:
    """Set/clear a member's muted flag. Gated on ``member:mute``."""
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_MEMBER_MUTE)
        if member_pubkey == (g.founder_pubkey or ""):
            raise HTTPException(status_code=403, detail="cannot mute the founder")
        row = await session.get(GroupMember, (group_id, member_pubkey))
        if row is None:
            raise HTTPException(status_code=404, detail="member not found")
        row.muted = 1 if body.muted else 0
        await session.commit()

    # Fan the mute/unmute out in the background so the button returns
    # instantly (the frame fan-out can take a moment per member).
    async def _publish() -> None:
        from nexus.runtime.group_inbox import publish_chat_mute

        async with get_session() as s2:
            await publish_chat_mute(s2, group_id, member_pubkey, body.muted)
            await s2.commit()

    asyncio.create_task(_publish())
    return {"group_id": group_id, "member_pubkey": member_pubkey, "muted": body.muted}


# ---- relay code fingerprint freeze / propose / accept ---------


_FP_RE = "^[0-9a-f]{32}$"


class SetFingerprintBody(BaseModel):
    fingerprint: str = Field(
        ...,
        pattern=f"{_FP_RE}|^$",
        description="32 hex chars (sha256 prefix) or empty to clear the freeze.",
    )


class ProposeFingerprintBody(BaseModel):
    fingerprint: str = Field(..., pattern=_FP_RE)


def _proposal_summary(row) -> dict:
    return {
        "id": row.id,
        "group_id": row.group_id or "",
        "proposed_fingerprint": row.proposed_fingerprint or "",
        "proposed_by_pubkey": row.proposed_by_pubkey or "",
        "proposed_at": row.proposed_at or "",
        "status": row.status or "pending",
        "decided_at": row.decided_at or "",
        "decided_by_pubkey": row.decided_by_pubkey or "",
    }


@router.post(
    "/{group_id}/relays/code_fingerprint",
    summary="Founder sets or clears the group's frozen relay code fingerprint",
)
async def set_group_relay_fingerprint(
    group_id: str, body: SetFingerprintBody
) -> dict:
    """Founder-only: write ``Group.relay_code_fingerprint`` directly.

    Empty string clears the freeze (any code accepted). 403 for non-founders;
    admins must use ``/propose`` + founder ``/accept``.
    """
    me = get_local_group_pubkey()
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        if (g.founder_pubkey or "") != me:
            raise HTTPException(
                status_code=403, detail="only the founder may set the fingerprint",
            )
        g.relay_code_fingerprint = body.fingerprint or ""
        await session.commit()

    await write_audit_event(
        action="group.relay.fingerprint.set",
        actor="local",
        task_id="",
        details=f"group_id={group_id} fingerprint={body.fingerprint or '<cleared>'}",
    )
    return {"group_id": group_id, "fingerprint": body.fingerprint or ""}


@router.post(
    "/{group_id}/relays/code_fingerprint/propose",
    summary="Admin opens a fingerprint-change proposal for the founder",
)
async def propose_group_relay_fingerprint(
    group_id: str, body: ProposeFingerprintBody
) -> dict:
    """Admin-only (``relay:host``). Founder must call ``/accept/{id}``."""
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_RELAY_HOST)
        if (g.founder_pubkey or "") == me:
            raise HTTPException(
                status_code=400,
                detail="founder should use the direct set endpoint, not propose",
            )
        proposal_id = uuid.uuid4().hex
        session.add(
            GroupRelayCodeprintProposal(
                id=proposal_id,
                group_id=group_id,
                proposed_fingerprint=body.fingerprint,
                proposed_by_pubkey=me,
                proposed_at=iso_now(),
                status="pending",
            )
        )
        await session.commit()

    await write_audit_event(
        action="group.relay.fingerprint.proposed",
        actor="local",
        task_id="",
        details=f"group_id={group_id} proposal_id={proposal_id} fingerprint={body.fingerprint}",
    )
    return {"proposal_id": proposal_id, "status": "pending"}


@router.get(
    "/{group_id}/relays/code_fingerprint/proposals",
    summary="List pending fingerprint-change proposals",
)
async def list_group_relay_fingerprint_proposals(group_id: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        rows = (
            await session.execute(
                select(GroupRelayCodeprintProposal).where(
                    (GroupRelayCodeprintProposal.group_id == group_id)
                    & (GroupRelayCodeprintProposal.status == "pending")
                )
            )
        ).scalars().all()
    return {"proposals": [_proposal_summary(r) for r in rows]}


@router.post(
    "/{group_id}/relays/code_fingerprint/accept/{proposal_id}",
    summary="Founder accepts (or rejects) a fingerprint proposal",
)
async def accept_group_relay_fingerprint(
    group_id: str,
    proposal_id: str,
    decision: str = "accept",
) -> dict:
    """``decision`` ∈ {``accept``, ``reject``}. Founder only."""
    if decision not in ("accept", "reject"):
        raise HTTPException(
            status_code=400, detail="decision must be 'accept' or 'reject'",
        )
    me = get_local_group_pubkey()
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        if (g.founder_pubkey or "") != me:
            raise HTTPException(
                status_code=403, detail="only the founder may decide",
            )
        row = await session.get(GroupRelayCodeprintProposal, proposal_id)
        if row is None or row.group_id != group_id:
            raise HTTPException(status_code=404, detail="proposal not found")
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"proposal already {row.status}",
            )
        row.status = "accepted" if decision == "accept" else "rejected"
        row.decided_at = iso_now()
        row.decided_by_pubkey = me
        applied_fp = ""
        if decision == "accept":
            g.relay_code_fingerprint = row.proposed_fingerprint or ""
            applied_fp = g.relay_code_fingerprint
        await session.commit()

    await write_audit_event(
        action=f"group.relay.fingerprint.{row.status}",
        actor="local",
        task_id="",
        details=f"group_id={group_id} proposal_id={proposal_id} fingerprint={applied_fp}",
    )
    return {
        "proposal_id": proposal_id,
        "status": row.status,
        "fingerprint": applied_fp,
    }


# ---- relay-code copy (publish / status / obtain) --------------


class PublishRelayCodeBody(BaseModel):
    # Which local relay module's source to publish as the group's canonical
    # copy. "default" is the bundled relay; otherwise a nexus_relays/<name>.py.
    module: str = Field(default="default")


@router.post(
    "/{group_id}/relay_code/publish",
    summary="Publish the group's canonical relay module source (founder/admin)",
)
async def publish_group_relay_code(
    group_id: str, body: PublishRelayCodeBody
) -> dict:
    """Seal a chosen local relay module's source into the group channel so
    members can copy and host the group's relay.

    Gated on ``role:assign`` (founder/admin governance, same set that controls
    the frozen fingerprint). The module's fingerprint MUST equal the group's
    frozen ``relay_code_fingerprint`` — otherwise members couldn't bind what
    they copied — so the group must freeze a fingerprint first.
    """
    from nexus.runtime import local_relay
    from nexus.runtime.group_inbox import publish_relay_code

    src = local_relay.get_module_source(body.module)
    if not src:
        raise HTTPException(status_code=404, detail="no such relay module")
    source = src.get("source") or ""
    fp = src.get("fingerprint") or ""

    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_ROLE_ASSIGN)
        frozen = (g.relay_code_fingerprint or "").strip()
        if not frozen:
            raise HTTPException(
                status_code=409,
                detail="group has no frozen relay fingerprint to publish against",
            )
        if fp != frozen:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"module '{body.module}' fingerprint {fp} does not match the "
                    f"group's frozen fingerprint {frozen}"
                ),
            )
        # Store our own copy (publish_frame excludes self from the fan-out).
        row = await session.get(GroupRelayCode, group_id)
        if row is None:
            session.add(GroupRelayCode(
                group_id=group_id, source=source, fingerprint=fp,
                published_by=me, published_at=iso_now(),
            ))
        else:
            row.source = source
            row.fingerprint = fp
            row.published_by = me
            row.published_at = iso_now()
        summary = await publish_relay_code(session, group_id, source)
        await session.commit()

    await write_audit_event(
        action="group.relay.code.published",
        actor="local",
        task_id="",
        details=f"group_id={group_id} module={body.module} fingerprint={fp}",
    )
    return {"group_id": group_id, "fingerprint": fp, "published": summary}


def _local_module_for_fingerprint(fingerprint: str) -> str:
    """Name of a local relay module whose code fingerprint equals
    *fingerprint*, or "" if none is present on this node."""
    from nexus.runtime import local_relay
    fp = (fingerprint or "").strip()
    if not fp:
        return ""
    for m in local_relay.available_relay_modules():
        if m.get("fingerprint") == fp:
            return m.get("name") or ""
    return ""


@router.get(
    "/{group_id}/relay_code/status",
    summary="Whether this node can copy/host the group's relay code",
)
async def group_relay_code_status(group_id: str) -> dict:
    """Surface what the UI needs to drive the copy flow: the group's frozen
    fingerprint, whether this node already has a matching relay module,
    whether a channel-published copy is available, and whether the caller
    holds ``relay:host``."""
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
    # Smoothing: a member learns the frozen fp only via the founder
    # pull; auto-pull it here so the copy card surfaces without a manual sync.
    frozen = await _ensure_frozen_fingerprint(group_id)
    async with get_session() as session:
        me = get_local_group_pubkey()
        perms = await effective_permissions(session, group_id, me)
        code_row = await session.get(GroupRelayCode, group_id)
        channel_copy_available = bool(
            code_row and frozen and (code_row.fingerprint or "") == frozen
            and (code_row.source or "").strip()
        )
    return {
        "group_id": group_id,
        "frozen_fingerprint": frozen,
        "have_local_module": _local_module_for_fingerprint(frozen),
        "channel_copy_available": channel_copy_available,
        "can_host": PERM_RELAY_HOST in perms,
    }


async def _pull_relay_code_live(group_id: str, frozen: str) -> str:
    """Live-host fallback: ask each of the group's relay hosts for source
    matching *frozen*. Returns the source (string) or "" if none served it.

    Authenticated with this node's own grant + a fresh challenge signature,
    so the serving host can prove we hold ``relay:host`` before releasing it."""
    import secrets as _secrets

    from nexus.security import group_grant
    from nexus.runtime.group_inbox import (
        _local_founder_grant_blob,
        _local_grant_blob,
        _resolve_relay_host_targets,
    )

    me = get_local_group_pubkey()
    async with get_session() as session:
        targets = await _resolve_relay_host_targets(session, group_id, {me})
        grant_blob = await _local_grant_blob(session, group_id)
        if grant_blob is None:
            grant_blob = await _local_founder_grant_blob(session, group_id)
    if not targets or grant_blob is None:
        return ""

    nonce = _secrets.token_bytes(16)
    signature = group_grant.sign_challenge(
        grant_blob=grant_blob, nonce=nonce,
        member_privkey=get_local_group_privkey(),
    )
    body = {
        "group_id": group_id,
        "grant_blob_b64": base64.b64encode(grant_blob).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    for _pubkey, addr, node_id in targets:
        status, response = await _post_to_admin(
            addr, "/peer/group/relay_code", body, node_id, group_id=group_id
        )
        if status == 200 and (response.get("source") or "").strip():
            if (response.get("fingerprint") or "") == frozen:
                return response.get("source") or ""
    return ""


@router.post(
    "/{group_id}/relay_code/obtain",
    summary="Copy the group's relay code as a local plugin (relay:host)",
)
async def obtain_group_relay_code(group_id: str) -> dict:
    """Obtain the group's relay module source — channel copy first, else a
    live pull from a relay host — and import it as a ``nexus_relays/<grp>.py``
    plugin so the caller can then run + bind it.

    Gated on ``relay:host`` (the caller chose this gate). Importing only
    WRITES the file; running it remains the operator's explicit, separate,
    sandboxable action. The obtained source is verified against the group's
    frozen fingerprint before import.
    """
    from nexus.runtime import local_relay
    from nexus.runtime.relay_codeprint import fingerprint_for_bytes

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_RELAY_HOST)
    # Auto-pull the frozen fp if this member hasn't synced it yet (W67 smoothing).
    frozen = await _ensure_frozen_fingerprint(group_id)
    if not frozen:
        raise HTTPException(
            status_code=409,
            detail="group has no frozen relay fingerprint",
        )
    existing = _local_module_for_fingerprint(frozen)
    if existing:
        return {
            "group_id": group_id, "name": existing,
            "fingerprint": frozen, "origin": "local", "already": True,
        }
    async with get_session() as session:
        code_row = await session.get(GroupRelayCode, group_id)
        source = ""
        origin = ""
        if (
            code_row and (code_row.fingerprint or "") == frozen
            and (code_row.source or "").strip()
        ):
            source, origin = code_row.source, "channel"

    if not source:
        source = await _pull_relay_code_live(group_id, frozen)
        origin = "live" if source else origin

    if not source:
        raise HTTPException(
            status_code=404,
            detail="no relay code available (no channel copy; no relay host served it)",
        )

    norm = source.replace("\r\n", "\n").replace("\r", "\n")
    fp = fingerprint_for_bytes(norm.encode("utf-8"))
    if fp != frozen:
        raise HTTPException(
            status_code=409,
            detail=f"obtained source fingerprint {fp} does not match frozen {frozen}",
        )

    name = local_relay.relay_module_name_for_group(group_id)
    try:
        res = local_relay.import_module_source(name, source)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await write_audit_event(
        action="group.relay.code.obtained",
        actor="local",
        task_id="",
        details=f"group_id={group_id} module={res['name']} origin={origin} fingerprint={fp}",
    )
    return {
        "group_id": group_id, "name": res["name"],
        "fingerprint": res["fingerprint"], "origin": origin, "already": False,
    }


# ---- follow-up: pull relay bindings from founder ---------------


async def _pull_relays_from_founder(group_id: str) -> dict:
    """Hit the founder's ``/peer/group/relays`` and merge any missing
    ``active`` bindings + adopt the founder's frozen fingerprint.

    Shared by the ``pull_from_founder`` endpoint and the auto-pull
    (``_ensure_frozen_fingerprint``). Assumes the caller has already done the
    group-exists / permission checks. Returns a summary dict; never raises on
    a network/lookup miss (returns ``{"ok": False, ...}``)."""
    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return {"ok": False, "reason": "group not found"}
        me = get_local_group_pubkey()
        if (g.founder_pubkey or "") == me:
            return {"ok": True, "skipped": "this node is the founder", "added": 0}
        founder_address = g.founder_address or ""
        founder_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == g.founder_pubkey)
                )
            )
        ).scalar_one_or_none()
        founder_node_id = (founder_member.node_id if founder_member else "") or ""

    if not founder_address and not founder_node_id:
        return {"ok": False, "reason": "no founder address cached"}

    status, response = await _post_to_admin(
        founder_address,
        "/peer/group/relays",
        {"group_id": group_id},
        founder_node_id,
        group_id=group_id,
    )
    if status != 200:
        return {
            "ok": False,
            "status": status,
            "reason": response.get("detail") or response.get("error") or "",
        }

    bindings = response.get("bindings") or []
    fingerprint = str(response.get("relay_code_fingerprint") or "")
    added = 0
    async with get_session() as session:
        # Adopt the founder's frozen fingerprint too — keeps the local
        # Group row in sync without a separate handshake.
        g2 = await session.get(Group, group_id)
        if g2 is not None and fingerprint:
            g2.relay_code_fingerprint = fingerprint
        for b in bindings:
            url = (b.get("relay_url") or "").strip()
            if not url:
                continue
            row = await session.get(GroupRelayBinding, (group_id, url))
            if row is None:
                session.add(
                    GroupRelayBinding(
                        group_id=group_id,
                        relay_url=url,
                        operator_pubkey=str(b.get("operator_pubkey") or ""),
                        registered_at=iso_now(),
                        last_seen_at=str(b.get("last_seen_at") or ""),
                        status="active",
                        state=str(b.get("state") or "online"),
                        host_node_id=str(b.get("host_node_id") or ""),
                        label=str(b.get("label") or ""),
                        region=str(b.get("region") or ""),
                        priority=int(b.get("priority") or 0),
                    )
                )
                added += 1
            elif row.status == "retired":
                row.status = "active"
                added += 1
        await session.commit()
    return {"ok": True, "added": added, "fingerprint": fingerprint}


async def _ensure_frozen_fingerprint(group_id: str) -> str:
    """Return the group's frozen relay fingerprint, attempting a one-shot
    ``pull_from_founder`` when this (non-founder) node hasn't learned it yet.

    smoothing: a member only learns the frozen fingerprint via the
    founder pull, and the W67 copy flow (status card + obtain) is gated on
    knowing it. Auto-pulling here means a freshly-joined member doesn't have
    to manually "Sync from founder" first. Returns "" if still unknown."""
    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return ""
        frozen = (g.relay_code_fingerprint or "").strip()
        is_founder = (g.founder_pubkey or "") == get_local_group_pubkey()
    if frozen or is_founder:
        return frozen
    try:
        await _pull_relays_from_founder(group_id)
    except Exception:
        _log.debug("auto-pull fingerprint failed for %s", group_id, exc_info=True)
    async with get_session() as session:
        g = await session.get(Group, group_id)
        return (g.relay_code_fingerprint or "").strip() if g else ""


@router.post(
    "/{group_id}/relays/pull_from_founder",
    summary="Backfill GroupRelayBinding rows from the founder over HTTP.",
)
async def pull_group_relays_from_founder(group_id: str) -> dict:
    """Direct alternative to frame-replay catchup for relay bindings.

    When a member joins a group, the founder's prior ``relay.update``
    frames may have been emitted before the member existed; even with
    catchup, edge cases (frame log truncated, network hiccup)
    can leave the joiner with an empty relay set even though the founder
    has bindings live. This endpoint hits the founder's
    ``/peer/group/relays`` and merges any missing ``active`` bindings
    into the local DB.
    """
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
    return await _pull_relays_from_founder(group_id)


# ---- relay state-change timeline ------------------------------


@router.get(
    "/{group_id}/relays/timeline",
    summary="Recent state-machine transitions for a binding",
)
async def get_relay_state_timeline(
    group_id: str, relay_url: str, limit: int = 50
) -> dict:
    """Return the last ``limit`` ``relay.state_change`` audit events for
    this binding, newest-first. Powers the relay:host expanded view's
    transition timeline.
    """
    from nexus.storage.models import AuditEvent

    limit = max(1, min(int(limit or 50), 500))
    needle = (
        f"group_id={group_id} relay_url={relay_url.strip()} "
    )
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        rows = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.action == "relay.state_change")
                .order_by(AuditEvent.ts.desc())
                .limit(limit * 4)  # over-fetch then filter by relay
            )
        ).scalars().all()
    events = []
    for r in rows:
        if needle not in (r.details or ""):
            continue
        # Parse "...starting->validating reason='probe ok'" out of details.
        transition = ""
        reason = ""
        for part in (r.details or "").split():
            if "->" in part:
                transition = part
        if "reason=" in (r.details or ""):
            tail = (r.details or "").split("reason=", 1)[1]
            reason = tail.strip().strip("'\"")
        events.append({
            "ts": r.ts or "",
            "transition": transition,
            "reason": reason,
        })
        if len(events) >= limit:
            break
    return {
        "group_id": group_id,
        "relay_url": relay_url,
        "events": events,
    }


# ---- secure (signed) group join links + caps ------------------


class SecureJoinLinkBody(BaseModel):
    expires_in_days: int = Field(default=7, ge=1, le=90)
    max_uses: int = Field(default=1, ge=1, le=1000)


class SetMaxMembersBody(BaseModel):
    max_members: int = Field(ge=0, le=100000)


def _v2_invite_summary(row) -> dict:
    """Summary of a signed v=2 group join invite."""
    return {
        "invite_id": row.invite_id,
        "group_id": row.group_id or "",
        "founder_pubkey": row.founder_pubkey or "",
        "issued_at": row.issued_at or "",
        "expires_at": row.expires_at or "",
        "max_uses": int(row.max_uses or 1),
        "used_count": int(row.used_count or 0),
        "status": row.status or "active",
        "last_used_at": row.last_used_at or "",
    }


@router.post(
    "/{group_id}/secure_link",
    summary="Issue a signed (v=2) group join link — safe to post publicly",
)
async def issue_secure_join_link(
    group_id: str, body: SecureJoinLinkBody
) -> dict:
    """Generate an ``nxg://join#…v=2`` link with per-link ``max_uses``.

    The link carries a signed envelope rather than the legacy grid_key,
    so it can be shared on Twitter / Discord without exposing transport
    credentials. The signature is verified at the founder's join handler
    (``/peer/group/join_request``) which also enforces both this link's
    ``max_uses`` *and* the group's ``max_members`` hard cap.
    """
    import secrets as _secrets
    from datetime import datetime, timedelta, timezone

    from nexus.security.group_invite_token import sign_group_join_invite
    from nexus.storage.models import GroupJoinInviteV2

    founder_pubkey = get_local_group_pubkey()
    founder_privkey = get_local_group_privkey()
    if not founder_pubkey or not founder_privkey:
        raise HTTPException(
            status_code=409, detail="local group keypair not initialised"
        )

    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        if (g.founder_pubkey or "") != founder_pubkey:
            raise HTTPException(
                status_code=403,
                detail="only the founder can issue a signed group-join link",
            )
        bindings = (
            await session.execute(
                select(GroupRelayBinding.relay_url).where(
                    (GroupRelayBinding.group_id == group_id)
                    & (GroupRelayBinding.status == "active")
                )
            )
        ).fetchall()
        relay_urls = sorted({row[0] for row in bindings if row[0]})
        # LAN-only groups (no relay bound) can still mint a link — joiners
        # on the same LAN reach the founder via direct address. The 409
        # Guard previously here was rolled back per the UI prompts
        # the user to spawn/paste a relay if they want cross-region reach.

        invite_id = _secrets.token_hex(32)
        issued_at = iso_now()
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(days=int(body.expires_in_days))
        ).isoformat()
        signed_blob = sign_group_join_invite(
            invite_id=invite_id,
            group_id=group_id,
            founder_pubkey=founder_pubkey,
            issued_at=issued_at,
            expires_at=expires_at,
            max_uses=int(body.max_uses),
            founder_privkey=founder_privkey,
        )

        session.add(
            GroupJoinInviteV2(
                invite_id=invite_id,
                group_id=group_id,
                founder_pubkey=founder_pubkey,
                issued_at=issued_at,
                expires_at=expires_at,
                max_uses=int(body.max_uses),
                used_count=0,
                status="active",
                signed_blob=signed_blob,
            )
        )
        await session.commit()

    link = encode_join_link(
        relay_urls=relay_urls,
        admin_address=get_node_identity(),
        invite_token="",
        group_id=group_id,
        admin_node_id=get_or_create_node_uuid(),
        signed_invite_hex=signed_blob,
    )
    return {
        "invite_id": invite_id,
        "link": link,
        "expires_at": expires_at,
        "max_uses": int(body.max_uses),
    }


@router.get(
    "/{group_id}/secure_links",
    summary="List signed (v=2) group-join invites issued for this group",
)
async def list_secure_join_links(group_id: str) -> dict:
    from nexus.storage.models import GroupJoinInviteV2

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        rows = (
            await session.execute(
                select(GroupJoinInviteV2)
                .where(GroupJoinInviteV2.group_id == group_id)
                .order_by(GroupJoinInviteV2.issued_at.desc())
            )
        ).scalars().all()
        bindings = (
            await session.execute(
                select(GroupRelayBinding.relay_url).where(
                    (GroupRelayBinding.group_id == group_id)
                    & (GroupRelayBinding.status == "active")
                )
            )
        ).fetchall()
    relay_urls = sorted({row[0] for row in bindings if row[0]})
    admin_address = get_node_identity()
    admin_node_id = get_or_create_node_uuid()
    invites = []
    for r in rows:
        summary = _v2_invite_summary(r)
        if r.signed_blob:
            summary["link"] = encode_join_link(
                relay_urls=relay_urls,
                admin_address=admin_address,
                invite_token="",
                group_id=group_id,
                admin_node_id=admin_node_id,
                signed_invite_hex=r.signed_blob,
            )
        else:
            summary["link"] = ""
        invites.append(summary)
    return {"invites": invites}


@router.delete(
    "/{group_id}/secure_links/{invite_id}",
    summary="Revoke a signed (v=2) group-join invite",
)
async def revoke_secure_join_link(group_id: str, invite_id: str) -> dict:
    from nexus.storage.models import GroupJoinInviteV2

    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        row = await session.get(GroupJoinInviteV2, invite_id)
        if row is None or row.group_id != group_id:
            raise HTTPException(status_code=404, detail="invite not found")
        if row.status in ("revoked", "exhausted"):
            return {"invite_id": invite_id, "status": row.status}
        row.status = "revoked"
        await session.commit()
    return {"invite_id": invite_id, "status": "revoked"}


@router.put(
    "/{group_id}/max_members",
    summary="Set the hard cap on group membership (0 = unlimited)",
)
async def set_max_members(group_id: str, body: SetMaxMembersBody) -> dict:
    me = get_local_group_pubkey()
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        if (g.founder_pubkey or "") != me:
            raise HTTPException(
                status_code=403,
                detail="only the founder can change max_members",
            )
        g.max_members = int(body.max_members)
        await session.commit()
    return {"group_id": group_id, "max_members": int(body.max_members)}


# ---- catch up from a peer's frame log -------------------------


@router.post(
    "/{group_id}/catchup",
    summary="Pull missed frames from a peer's GroupFrameLog and apply locally",
)
async def post_group_catchup(group_id: str) -> dict:
    """Walk the founder (or any reachable peer) for frames captured
    after our local ``Group.last_catchup_at``, dispatch each through
    the normal inbound handler, then advance the high-watermark.

    Idempotent — the existing frame-dedupe cache + per-frame
    ``apply_*`` checks make re-applied frames no-ops.
    """
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_READ)
        if (g.founder_pubkey or "") == get_local_group_pubkey():
            # We are the founder; we own the canonical log already.
            return {"ok": True, "skipped": "this node is the founder", "applied": 0}
        founder_address = g.founder_address or ""
        since_iso = g.last_catchup_at or ""
        founder_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == g.founder_pubkey)
                )
            )
        ).scalar_one_or_none()
        founder_node_id = (founder_member.node_id if founder_member else "") or ""
        if not founder_address and not founder_node_id:
            return {"ok": False, "reason": "no founder address cached"}

    status, response = await _post_to_admin(
        founder_address,
        "/peer/group/catchup",
        {"group_id": group_id, "since_iso": since_iso, "limit": 200},
        founder_node_id,
        group_id=group_id,
    )
    if status != 200:
        return {
            "ok": False,
            "status": status,
            "reason": response.get("detail") or response.get("error") or "",
        }

    frames = response.get("frames") or []
    if not frames:
        return {"ok": True, "applied": 0, "latest_at": since_iso}

    from nexus.runtime.group_inbox import dispatch_inbound_frame

    applied = 0
    latest_at = since_iso
    for entry in frames:
        env = entry.get("envelope") or {}
        captured_at = str(entry.get("captured_at") or "")
        try:
            res = await dispatch_inbound_frame(env)
            if res.get("applied"):
                applied += 1
        except Exception:
            _log.debug("catchup dispatch failed", exc_info=True)
        if captured_at and captured_at > latest_at:
            latest_at = captured_at

    if latest_at and latest_at > (since_iso or ""):
        async with get_session() as session:
            g2 = await session.get(Group, group_id)
            if g2 is not None:
                g2.last_catchup_at = latest_at
                await session.commit()

    return {
        "ok": True,
        "applied": applied,
        "fetched": len(frames),
        "latest_at": latest_at,
    }


# ---- per-group local pause ----------------------------------


@router.post("/{group_id}/pause", summary="Pause this node's participation in a group")
async def pause_group(group_id: str) -> dict:
    """Stop sending outbound frames to this group and drop inbound ones.

    Local-only — the pause flag is not replicated. Other members keep
    sending; we just don't react. They see us as offline (their roster
    heartbeats lapse on our side).
    """
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        g.paused = 1
        await session.commit()
    return {"group_id": group_id, "paused": True}


@router.post("/{group_id}/resume", summary="Resume participation in a group")
async def resume_group(group_id: str) -> dict:
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        g.paused = 0
        await session.commit()
    return {"group_id": group_id, "paused": False}


@router.delete(
    "/{group_id}",
    summary="Soft-delete a group (founder only)",
)
async def delete_group(group_id: str) -> dict:
    me = get_local_group_pubkey()
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        if (g.founder_pubkey or "") != me:
            raise HTTPException(
                status_code=403,
                detail="only the founder may delete a group",
            )
        g.deleted_at = iso_now()
        await session.commit()
    await write_audit_event(
        action="group.delete",
        actor="local",
        task_id="",
        details=f"group_id={group_id} name={g.name!r}",
    )
    return {"ok": True, "group_id": group_id}


@router.post(
    "/{group_id}/privacy",
    summary="Change a group's privacy_mode (founder/admin only)",
)
async def set_privacy_mode(group_id: str, body: SetPrivacyModeBody) -> dict:
    if body.privacy_mode not in _VALID_PRIVACY_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"privacy_mode must be one of {_VALID_PRIVACY_MODES}",
        )
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        # Editable by anyone holding role:assign (founders + admins per
        # DEFAULT_ROLES). Matches the §11.3 power-balance rule.
        await _require_perm(session, group_id, PERM_ROLE_ASSIGN)
        old_mode = g.privacy_mode or "open"
        g.privacy_mode = body.privacy_mode
        await session.commit()

    await write_audit_event(
        action="group.privacy.change",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} from={old_mode} to={body.privacy_mode}"
        ),
    )
    return {"group_id": group_id, "privacy_mode": body.privacy_mode}


class SetAvatarBody(BaseModel):
    avatar: str = Field(default="")


@router.post(
    "/{group_id}/avatar",
    summary="Set the group's profile picture (role:assign holders)",
)
async def set_group_avatar(group_id: str, body: SetAvatarBody) -> dict:
    """Store a small image data URL and sync it to every member
    via the durable ``group.meta`` frame. Empty string clears it."""
    from nexus.runtime.group_inbox import _avatar_valid, publish_group_meta

    avatar = str(body.avatar or "")
    if not _avatar_valid(avatar):
        raise HTTPException(
            status_code=400,
            detail="avatar must be a data:image/...;base64 URL under 64 KB"
            " (empty string clears it)",
        )
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_ROLE_ASSIGN)
        g.avatar = avatar
        await session.commit()
        fanout = await publish_group_meta(session, group_id, avatar)

    await write_audit_event(
        action="group.avatar.change",
        actor="local",
        task_id="",
        details=f"group_id={group_id} bytes={len(avatar)}",
    )
    return {"group_id": group_id, "avatar_set": bool(avatar), **{
        k: fanout.get(k) for k in ("delivered", "failed") if k in fanout
    }}


# ---- founder pre-delegate UX nudge (15.8) ------------------------------


@router.post(
    "/{group_id}/skip_predelegate",
    summary="Founder dismissed the 'add backup admin' prompt — record an audit row.",
)
async def post_skip_predelegate(group_id: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        # Only the founder sees this prompt, but the perm check is
        # a defensive cross-check so a non-founder can't fabricate
        # audit rows.
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
    await write_audit_event(
        action="group.predelegate.skipped",
        actor="local",
        task_id="",
        details=f"group_id={group_id}",
    )
    return {"ok": True}


# ---- invites ------------------------------------------------------------


@router.post("/{group_id}/invites", summary="Mint an invite link")
async def post_mint_invite(group_id: str, body: MintInviteBody) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_INVITE)
        invite = await group_invite.mint_invite(
            session=session,
            group_id=group_id,
            slot_cap=body.slot_cap,
            created_by_pubkey=me,
        )
        await session.commit()

    await write_audit_event(
        action="group.invite.mint",
        actor="local",
        task_id="",
        details=f"group_id={group_id} slot_cap={body.slot_cap}",
    )
    return _invite_summary(invite)


@router.post(
    "/{group_id}/invites/{token}/rotate",
    summary="Rotate an invite token (kills the old one)",
)
async def post_rotate_invite(group_id: str, token: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_INVITE)
        new = await group_invite.rotate_invite(
            session=session,
            token=token,
            group_id=group_id,
            created_by_pubkey=me,
        )
        if new is None:
            raise HTTPException(status_code=404, detail="invite token not found")
        await session.commit()

    await write_audit_event(
        action="group.invite.rotate",
        actor="local",
        task_id="",
        details=f"group_id={group_id} old_token={token[:8]}",
    )
    return _invite_summary(new)


@router.delete(
    "/{group_id}/invites/{token}",
    summary="Hard-delete an invite (audit row preserved)",
)
async def delete_invite(group_id: str, token: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        ok = await group_invite.delete_invite(
            session=session, token=token, group_id=group_id
        )
        if not ok:
            raise HTTPException(status_code=404, detail="invite token not found")
        await session.commit()

    await write_audit_event(
        action="group.invite.delete",
        actor="local",
        task_id="",
        details=f"group_id={group_id} token={token[:8]}",
    )
    return {"ok": True}


@router.post(
    "/{group_id}/invites/{token}/reopen",
    summary="Re-open a closed invite (flip active back on, optionally raise cap)",
)
async def post_reopen_invite(
    group_id: str,
    token: str,
    body: ReopenInviteBody,
) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        invite = await group_invite.reopen_invite(
            session=session,
            token=token,
            group_id=group_id,
            new_slot_cap=body.new_slot_cap,
        )
        if invite is None:
            raise HTTPException(
                status_code=404,
                detail="invite not found or already rotated",
            )
        await session.commit()

    await write_audit_event(
        action="group.invite.reopen",
        actor="local",
        task_id="",
        details=f"group_id={group_id} token={token[:8]}",
    )
    return _invite_summary(invite)


# ---- roles --------------------------------------------------------------


@router.post("/{group_id}/roles", summary="Create or update a role")
async def upsert_role(group_id: str, body: UpsertRoleBody) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_ROLE_ASSIGN)

        # Founder + member roles are immutable. Founder anchors the rank
        # model; member is the baseline-read floor every member is
        # guaranteed to hold (see assign_member_roles), so removing or
        # weakening its perms would let an admin silently revoke read
        # access from the whole group.
        if body.name in ("founder", "member"):
            raise HTTPException(
                status_code=409,
                detail=f"the {body.name} role is immutable",
            )

        existing = (
            await session.execute(
                select(GroupRole).where(
                    (GroupRole.group_id == group_id)
                    & (GroupRole.name == body.name)
                )
            )
        ).scalar_one_or_none()

        now = iso_now()
        perms_blob = encode_role_permissions(body.permissions)
        if existing is None:
            session.add(
                GroupRole(
                    group_id=group_id,
                    name=body.name,
                    permissions_json=perms_blob,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.permissions_json = perms_blob
            existing.updated_at = now
        await session.commit()

    # Follow-up: defer the broadcast to a background task so the
    # UI returns immediately. The role write is already committed; peer
    # convergence happens when the frame lands.
    permissions_snapshot = sorted(set(body.permissions))
    async def _publish_role_upsert():
        from nexus.runtime.group_inbox import publish_roles_def
        try:
            async with get_session() as bg_session:
                await publish_roles_def(
                    bg_session, group_id, "upsert",
                    body.name, permissions_snapshot, now,
                )
                await bg_session.commit()
        except Exception:
            _log.warning("publish_roles_def background failed", exc_info=True)
    asyncio.create_task(_publish_role_upsert())

    await write_audit_event(
        action="group.role.upsert",
        actor="local",
        task_id="",
        details=f"group_id={group_id} role={body.name!r}",
    )
    return {
        "name": body.name,
        "permissions": permissions_snapshot,
        "updated_at": iso_now(),
    }


@router.delete("/{group_id}/roles/{role_name}", summary="Delete a role")
async def delete_role(group_id: str, role_name: str) -> dict:
    if role_name in _PROTECTED_ROLE_NAMES:
        raise HTTPException(
            status_code=409,
            detail=f"role {role_name!r} is a default role and cannot be deleted",
        )
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_ROLE_ASSIGN)

        existing = (
            await session.execute(
                select(GroupRole).where(
                    (GroupRole.group_id == group_id)
                    & (GroupRole.name == role_name)
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(status_code=404, detail="role not found")

        await session.execute(
            delete(GroupMemberRole).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.role_name == role_name)
            )
        )
        await session.delete(existing)
        # Broadcast the deletion so every member's UI converges.
        from nexus.runtime.group_inbox import publish_roles_def

        await publish_roles_def(
            session, group_id, "delete", role_name, [], iso_now()
        )
        await session.commit()

    await write_audit_event(
        action="group.role.delete",
        actor="local",
        task_id="",
        details=f"group_id={group_id} role={role_name!r}",
    )
    return {"ok": True}


# ---- member-role assignment ---------------------------------------------


@router.post(
    "/{group_id}/members/{member_pubkey}/roles",
    summary="Replace a member's role assignment list",
)
async def assign_member_roles(
    group_id: str,
    member_pubkey: str,
    body: AssignRolesBody = Body(...),
) -> dict:
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_ROLE_ASSIGN)

        member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == member_pubkey)
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise HTTPException(
                status_code=404, detail="member not in this group"
            )

        # Validate every named role exists in the group.
        role_rows = (
            await session.execute(
                select(GroupRole.name).where(GroupRole.group_id == group_id)
            )
        ).fetchall()
        known_roles = {row[0] for row in role_rows}
        requested = set(body.roles)
        unknown = requested - known_roles
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown roles: {sorted(unknown)}",
            )

        # The founder cannot be stripped of the ``founder`` role.
        if member_pubkey == g.founder_pubkey and "founder" not in requested:
            raise HTTPException(
                status_code=409,
                detail="cannot remove the founder role from the founder",
            )

        # Every member always holds ``member`` (baseline group:read).
        # Auto-include so admins can't accidentally lock a peer out by
        # assigning only a custom role.
        requested.add("member")

        # Replace the assignment set.
        me = get_local_group_pubkey()
        now = iso_now()
        await session.execute(
            delete(GroupMemberRole).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.member_pubkey == member_pubkey)
            )
        )
        for role_name in requested:
            session.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=member_pubkey,
                    role_name=role_name,
                    assigned_by_pubkey=me,
                    assigned_at=now,
                )
            )
        await session.commit()

    # Follow-up: defer the broadcast so the API returns immediately.
    # Still applies: ``_sender_can_assign_roles`` re-validates the
    # frame on every recipient.
    requested_snapshot = sorted(requested)
    async def _publish_roles_assign():
        from nexus.runtime.group_inbox import publish_roles_assign
        try:
            async with get_session() as bg_session:
                await publish_roles_assign(
                    bg_session, group_id, member_pubkey,
                    requested_snapshot, now,
                )
                await bg_session.commit()
        except Exception:
            _log.warning("publish_roles_assign background failed", exc_info=True)
    asyncio.create_task(_publish_roles_assign())

    await write_audit_event(
        action="group.member.assign_roles",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} member={member_pubkey[:8]} "
            f"roles={requested_snapshot}"
        ),
    )
    return {
        "group_id": group_id,
        "member_pubkey": member_pubkey,
        "roles": requested_snapshot,
    }


# ---- member kick ---------------------------------------------


@router.post(
    "/{group_id}/members/{member_pubkey}/kick",
    summary="Remove a member and rotate the group symkey",
)
async def kick_member(group_id: str, member_pubkey: str) -> dict:
    """Remove a member and rotate the group symkey.

    Deletes the member's ``GroupMember`` / ``GroupMemberRole`` /
    ``GroupGrant`` rows locally, then mints a fresh symkey and
    broadcasts a ``symkey.rotate`` frame that re-keys every *remaining*
    member. The kicked member keeps its own copy of the old key — so the
    pre-kick history it already holds stays readable — but never
    receives the new one, so it cannot decrypt any frame published after
    the kick.
    """
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_MEMBER_KICK)

        if member_pubkey == me:
            raise HTTPException(status_code=409, detail="cannot kick yourself")
        if member_pubkey == g.founder_pubkey:
            raise HTTPException(
                status_code=409, detail="cannot kick the founder"
            )

        member = await session.get(GroupMember, (group_id, member_pubkey))
        if member is None:
            raise HTTPException(
                status_code=404, detail="member not in this group"
            )

        # Drop the kicked member's local rows.
        await session.execute(
            delete(GroupMember).where(
                (GroupMember.group_id == group_id)
                & (GroupMember.pubkey == member_pubkey)
            )
        )
        await session.execute(
            delete(GroupMemberRole).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.member_pubkey == member_pubkey)
            )
        )
        await session.execute(
            delete(GroupGrant).where(
                (GroupGrant.group_id == group_id)
                & (GroupGrant.member_pubkey == member_pubkey)
            )
        )

        # Rotate the symkey + broadcast it to every remaining member.
        from nexus.runtime.group_inbox import publish_symkey_rotate

        rotate = await publish_symkey_rotate(session, group_id, member_pubkey)
        await session.commit()

    await write_audit_event(
        action="group.member.kicked",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} member={member_pubkey[:8]} "
            f"rotate_via={rotate.get('via', '')}"
        ),
    )
    return {
        "group_id": group_id,
        "kicked_pubkey": member_pubkey,
        "symkey_rotated": True,
        "rotate": rotate,
    }


# ---- member self-leave (follow-up) -----------------------------


@router.post(
    "/{group_id}/leave",
    summary="Leave a group (non-founder member)",
)
async def leave_group(group_id: str) -> dict:
    """Leave a group as a non-founder member.

    Mints a fresh symkey + seals it to every remaining member (skipping
    self), broadcasts the rotation frame with the old key, then deletes
    this node's local membership rows. After the call:

    - Remaining members hold the new symkey; this node does not.
    - The group disappears from this node's ``list_groups`` because
      :func:`list_groups` filters on ``GroupMember.pubkey == me``.
    - The founder cannot use this endpoint — they must
      :http:delete:`/local/groups/{group_id}` instead.
    """
    me = get_local_group_pubkey()
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        if (g.founder_pubkey or "") == me:
            raise HTTPException(
                status_code=409,
                detail="founder cannot leave — delete the group instead",
            )
        member = await session.get(GroupMember, (group_id, me))
        if member is None:
            raise HTTPException(
                status_code=404, detail="you are not in this group",
            )

        # Rotate first (still uses old symkey to seal the frame, leaves us
        # out of the new envelopes because we pass ourselves as both
        # ``me`` and ``kicked_pubkey``).
        from nexus.runtime.group_inbox import publish_symkey_rotate

        rotate = await publish_symkey_rotate(session, group_id, me)

        # Then drop our own rows.
        await session.execute(
            delete(GroupMember).where(
                (GroupMember.group_id == group_id)
                & (GroupMember.pubkey == me)
            )
        )
        await session.execute(
            delete(GroupMemberRole).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.member_pubkey == me)
            )
        )
        await session.execute(
            delete(GroupGrant).where(
                (GroupGrant.group_id == group_id)
                & (GroupGrant.member_pubkey == me)
            )
        )
        await session.commit()

    # Remove this group's grid_key from the relay subs so we
    # stop receiving its bucketed broadcasts.
    from nexus.networking.relay_client import push_grid_key_update
    from nexus.security.grid_keys import derive_group_grid_key
    gone_key = derive_group_grid_key(group_id)
    if gone_key:
        await push_grid_key_update(removed=[gone_key])

    await write_audit_event(
        action="group.member.left",
        actor="local",
        task_id="",
        details=f"group_id={group_id} rotate_via={rotate.get('via', '')}",
    )
    return {
        "ok": True,
        "group_id": group_id,
        "left_pubkey": me,
        "rotate": rotate,
    }


# ---- private-mode pending request queue --------------------


def _pending_summary(row: GroupPendingJoinRequest) -> dict:
    return {
        "id": row.id,
        "group_id": row.group_id,
        "joiner_pubkey": row.joiner_pubkey,
        "joiner_address": row.joiner_address or "",
        "invite_token": row.invite_token,
        "message": row.message or "",
        "display_name": row.display_name or "",
        "status": row.status,
        "created_at": row.created_at or "",
        "decided_at": row.decided_at or "",
        "decided_by_pubkey": row.decided_by_pubkey or "",
        "decision_reason": row.decision_reason or "",
    }


@router.get(
    "/{group_id}/pending_requests",
    summary="List pending join requests (admin only)",
)
async def list_pending_requests(group_id: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_APPROVE)
        rows = (
            await session.execute(
                select(GroupPendingJoinRequest).where(
                    GroupPendingJoinRequest.group_id == group_id
                )
            )
        ).scalars().all()
        return {"requests": [_pending_summary(r) for r in rows]}


@router.post(
    "/{group_id}/pending_requests/{request_id}/approve",
    summary="Approve a pending join request (consumes one invite slot)",
)
async def approve_pending_request(group_id: str, request_id: str) -> dict:
    # Local import to avoid an import cycle: group_peer pulls
    # group_invite, which we already use here.
    from nexus.api.group_peer import issue_member_grant
    from nexus.runtime.group_decisions import attempt_deliver_one

    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_APPROVE)
        row = await session.get(GroupPendingJoinRequest, request_id)
        if row is None or row.group_id != group_id:
            raise HTTPException(status_code=404, detail="pending request not found")
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"request already {row.status}",
            )

        grant_data = await issue_member_grant(
            session,
            group_id=group_id,
            joiner_pubkey=row.joiner_pubkey,
            invite_token=row.invite_token,
            display_name=row.display_name or "",
            peer_address=row.joiner_address or "",
            joiner_x25519_pub=row.joiner_x25519_pub or "",
            joiner_node_id=row.joiner_node_id or "",
        )
        row.status = "approved"
        row.decided_at = iso_now()
        row.decided_by_pubkey = me

        b64_blob = base64.b64encode(grant_data["grant_blob"]).decode("ascii")
        founder_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == g.founder_pubkey)
                )
            )
        ).scalar_one_or_none()
        symkey_envelope_b64 = (
            base64.b64encode(grant_data["symkey_envelope"]).decode("ascii")
            if grant_data["symkey_envelope"]
            else ""
        )
        # Snapshot every field needed by the background task BEFORE the
        # session closes — the row object becomes detached after commit.
        deliver_ctx = {
            "row_id": row.id,
            "joiner_pubkey": row.joiner_pubkey,
            "joiner_address": row.joiner_address or "",
            "group_name": g.name or "",
            "founder_pubkey": g.founder_pubkey or "",
            "grant_blob_b64": b64_blob,
            "default_role": grant_data["default_role"],
            "issued_at": grant_data["issued_at"],
            "expires_at": grant_data["expires_at"],
            "privacy_mode": g.privacy_mode or "open",
            "founder_display_name": (founder_member.display_name if founder_member else "") or "",
            "founder_address": get_node_identity(),
            "symkey_envelope_b64": symkey_envelope_b64,
        }
        await session.commit()

    # Follow-up: hand off delivery + frame fan-out to a background
    # task so the API returns immediately. Previously this endpoint awaited
    # ``attempt_deliver_one`` (HTTP to joiner, can stall if offline) plus
    # ``publish_pending_decision`` + ``publish_roster_update`` (frame
    # fan-out to every member through the relay) — that made the Approve
    # button feel frozen for multi-second windows even on healthy networks.
    # The grant is already committed, the scheduler will retry delivery
    # if this one-shot fails, and the frame log carries replay state.
    async def _deliver_in_background():
        from nexus.runtime.group_inbox import (
            publish_pending_decision,
            publish_roster_update,
        )
        try:
            async with get_session() as bg_session:
                bg_row = await bg_session.get(
                    GroupPendingJoinRequest, deliver_ctx["row_id"]
                )
                if bg_row is None:
                    return
                await attempt_deliver_one(
                    bg_session, bg_row,
                    group_name=deliver_ctx["group_name"],
                    founder_pubkey=deliver_ctx["founder_pubkey"],
                    grant_blob_b64=deliver_ctx["grant_blob_b64"],
                    default_role=deliver_ctx["default_role"],
                    issued_at=deliver_ctx["issued_at"],
                    expires_at=deliver_ctx["expires_at"],
                    privacy_mode=deliver_ctx["privacy_mode"],
                    founder_display_name=deliver_ctx["founder_display_name"],
                    founder_address=deliver_ctx["founder_address"],
                    symkey_envelope_b64=deliver_ctx["symkey_envelope_b64"],
                )
                await publish_pending_decision(bg_session, bg_row)
                await publish_roster_update(
                    bg_session, group_id, deliver_ctx["joiner_pubkey"]
                )
                await bg_session.commit()
        except Exception:
            _log.warning(
                "background deliver/fan-out failed for request %s",
                deliver_ctx["row_id"][:8], exc_info=True,
            )
    asyncio.create_task(_deliver_in_background())

    await write_audit_event(
        action="group.join.approved",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} request_id={request_id[:8]} "
            f"joiner={deliver_ctx['joiner_pubkey'][:8]} delivered=async"
        ),
    )
    return {
        "status": "approved",
        "request_id": request_id,
        "group_id": group_id,
        "joiner_pubkey": deliver_ctx["joiner_pubkey"],
        "joiner_address": deliver_ctx["joiner_address"],
        "delivered": None,  # delivery handed to background task
        "grant_blob_b64": b64_blob,
        "default_role": grant_data["default_role"],
        "issued_at": grant_data["issued_at"],
        "expires_at": grant_data["expires_at"],
        "symkey_envelope_b64": symkey_envelope_b64,
    }


@router.post(
    "/{group_id}/pending_requests/{request_id}/reject",
    summary="Reject a pending join request (slot is NOT consumed)",
)
async def reject_pending_request(
    group_id: str, request_id: str, body: RejectPendingBody = Body(default=None)
) -> dict:
    from nexus.runtime.group_decisions import attempt_deliver_one

    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_APPROVE)
        row = await session.get(GroupPendingJoinRequest, request_id)
        if row is None or row.group_id != group_id:
            raise HTTPException(status_code=404, detail="pending request not found")
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"request already {row.status}",
            )
        row.status = "rejected"
        row.decided_at = iso_now()
        row.decided_by_pubkey = me
        row.decision_reason = (body.reason if body else "") or ""

        delivered = await attempt_deliver_one(
            session,
            row,
            group_name=g.name or "",
            founder_pubkey=g.founder_pubkey or "",
        )
        # Replicate the decision so peer admins flip their
        # mirrored copy of this request out of 'pending'.
        from nexus.runtime.group_inbox import publish_pending_decision

        await publish_pending_decision(session, row)
        await session.commit()

    await write_audit_event(
        action="group.join.rejected",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} request_id={request_id[:8]} "
            f"joiner={row.joiner_pubkey[:8]} delivered={delivered}"
        ),
    )
    return {
        "status": "rejected",
        "request_id": request_id,
        "group_id": group_id,
        "joiner_pubkey": row.joiner_pubkey,
        "joiner_address": row.joiner_address or "",
        "delivered": delivered,
        "reason": row.decision_reason,
    }


# ---- join (joiner side, driven by the local UI) ------------------------


async def _post_to_admin(
    admin_address: str,
    path: str,
    body: dict,
    admin_node_id: str = "",
    link_relay_urls: list[str] | None = None,
    link_grid_key: str = "",
    *,
    group_id: str | None = None,
) -> tuple[int, dict]:
    """POST to an admin node — HTTPS, then HTTP, then the relay.

    No cert pinning yet (the invite token is the trust mechanism for
    ). if direct HTTP can't connect and ``admin_node_id``
    is known, fall back to the joiner's own ``STATE.relay_ws`` so a
    NAT'd admin is still reachable. if that fails too, walk
    ``link_relay_urls`` (from the join link) and open a transient WS
    to each authenticated with ``link_grid_key`` — that lets a joiner
    reach a founder's self-hosted relay without having pre-configured
    its grid_key. Only a *connection* failure falls through to the next
    rung; an admin that answers (even with a 4xx) returns that answer
    directly.

    Returns ``(status_code, response_json)`` or ``(503, {...})``.
    """
    if admin_address:
        for scheme in ("https", "http"):
            try:
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    res = await client.post(
                        f"{scheme}://{admin_address}{path}", json=body
                    )
                    try:
                        return res.status_code, res.json()
                    except ValueError:
                        return res.status_code, {"error": "non-JSON response"}
            except httpx.HTTPError:
                continue
    if admin_node_id:
        try:
            from nexus.networking.relay_client import relay_http_request

            # Restrict relay candidates to this group's
            # bindings (when group_id provided). Pre-join flows leave
            # group_id None and keep pool-wide behavior.
            resp = await relay_http_request(
                admin_node_id, "POST", path, body, group_id=group_id
            )
            status_code = int(resp.get("status", 502))
            if status_code != 503:
                return status_code, (resp.get("body") or {})
        except Exception:
            _log.debug(
                "relay_http_request to admin %s failed",
                admin_node_id, exc_info=True,
            )
    # Last-chance fallback — walk the link's relay URLs with a
    # transient WS connection authenticated by the link's grid_key.
    if admin_node_id and link_grid_key and link_relay_urls:
        from nexus.networking.relay_client import relay_http_request_one_shot

        for relay_url in link_relay_urls:
            if not relay_url:
                continue
            try:
                resp = await relay_http_request_one_shot(
                    relay_url, link_grid_key, admin_node_id,
                    "POST", path, body,
                )
                status_code = int(resp.get("status", 502))
                if status_code != 503:
                    return status_code, (resp.get("body") or {})
            except Exception:
                _log.debug(
                    "one-shot relay to %s failed", relay_url, exc_info=True,
                )
    return 503, {"error": "admin unreachable (direct + relay)"}


# ---- join-link encode / parse --------------------------------


@router.post(
    "/{group_id}/join_link",
    summary="Pack an invite token + this group's relay bindings into a join link",
)
async def post_build_join_link(group_id: str, body: JoinLinkBuildBody) -> dict:
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        bindings = (
            await session.execute(
                select(GroupRelayBinding.relay_url).where(
                    (GroupRelayBinding.group_id == group_id)
                    & (GroupRelayBinding.status == "active")
                )
            )
        ).fetchall()
        # admin_address + admin_node_id: prefer this node's identity when
        # we are the founder; otherwise read the founder's GroupMember
        # Row captured via the roster (post-ship work in). The
        # Node_id lets a joiner reach a NAT'd admin via relay.
        me = get_local_group_pubkey()
        if (g.founder_pubkey or "") == me:
            admin_address = get_node_identity()
            admin_node_id = get_or_create_node_uuid()
        else:
            founder_member = (
                await session.execute(
                    select(GroupMember).where(
                        (GroupMember.group_id == group_id)
                        & (GroupMember.pubkey == g.founder_pubkey)
                    )
                )
            ).scalar_one_or_none()
            admin_address = (founder_member.peer_address if founder_member else "") or ""
            admin_node_id = (founder_member.node_id if founder_member else "") or ""
    relay_urls = sorted({row[0] for row in bindings if row[0]})
    # Bundle the founder's relay grid_key so the joiner can
    # auth a transient WS to a self-hosted relay without having
    # configured the same key locally. The link is already a bearer
    # credential so this doesn't widen the trust surface.
    grid_key = str(LOCAL_SETTINGS.get("relay_grid_key", "") or "")
    link = encode_join_link(
        relay_urls=relay_urls,
        admin_address=admin_address,
        invite_token=body.invite_token,
        group_id=group_id,
        admin_node_id=admin_node_id,
        grid_key=grid_key,
    )
    return {
        "join_link": link,
        "relay_urls": relay_urls,
        "admin_address": admin_address,
        "admin_node_id": admin_node_id,
        "grid_key": grid_key,
        "invite_token": body.invite_token,
        "group_id": group_id,
    }


@router.post(
    "/parse_join_link",
    summary="Parse a join link without joining (UI helper)",
)
async def post_parse_join_link(body: JoinLinkParseBody) -> dict:
    try:
        parsed = parse_join_link(body.join_link)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "relay_urls": list(parsed.relay_urls),
        "admin_address": parsed.admin_address,
        "admin_node_id": parsed.admin_node_id,
        "grid_key": parsed.grid_key,
        "invite_token": parsed.invite_token,
        "group_id": parsed.group_id,
        "version": parsed.version,
        # Signed v=2 envelope, when the link carries one.
        # Empty on legacy v=1 links.
        "signed_invite_hex": parsed.signed_invite_hex,
    }


@router.post(
    "/probe",
    summary="Look up group metadata for an invite token before joining",
)
async def post_probe_group(body: ProbeGroupBody) -> dict:
    status, response = await _post_to_admin(
        body.admin_address,
        "/peer/group/info",
        {"invite_token": body.invite_token},
        body.admin_node_id,
        body.relay_urls,
        body.grid_key,
    )
    if status != 200:
        raise HTTPException(
            status_code=status if status >= 400 else 502,
            detail=response.get("detail") or response.get("error") or response,
        )
    return response


@router.post("/join", summary="Submit an invite to an admin node and store the grant")
async def post_join_group(body: JoinGroupBody) -> dict:
    me = get_local_group_pubkey()
    my_name = str(LOCAL_SETTINGS.get("user_display_name") or "")
    my_x25519 = derive_x25519_pubkey_hex(get_local_group_privkey())
    # Default joiner_address to this node's own identity so the admin can
    # call /peer/group/join_decision back directly. Without this, an empty
    # field forced the decision push through the relay only — and a
    # mis-configured / down relay then left private-mode joiners stuck
    # in "pending" forever even when direct HTTP would have worked.
    joiner_address = body.joiner_address or get_node_identity()
    status, response = await _post_to_admin(
        body.admin_address,
        "/peer/group/join_request",
        {
            "invite_token": body.invite_token,
            # Forward the signed envelope when the link carried one.
            "signed_invite_hex": body.signed_invite_hex,
            "joiner_pubkey": me,
            "message": body.message or "",
            "joiner_address": joiner_address,
            "display_name": my_name,
            "joiner_x25519_pub": my_x25519,
            "joiner_node_id": get_or_create_node_uuid(),
        },
        body.admin_node_id,
        body.relay_urls,
        body.grid_key,
    )
    if status != 200:
        raise HTTPException(
            status_code=status if status >= 400 else 502,
            detail=response.get("detail") or response.get("error") or response,
        )

    # (16.2): private-mode admin returns 200 with status="pending"
    # and no grant blob. Don't persist anything yet — the joiner-side
    # state arrives later via /peer/group/join_decision (16.4) when the
    # admin approves.
    if response.get("status") == "pending":
        await write_audit_event(
            action="group.join.requested",
            actor="local",
            task_id="",
            details=(
                f"group_id={response.get('group_id', '')} "
                f"admin_address={body.admin_address} "
                f"request_id={response.get('request_id', '')[:8]}"
            ),
        )
        return {
            "status": "pending",
            "request_id": response.get("request_id", ""),
            "group_id": response.get("group_id", ""),
            "group_name": response.get("group_name", ""),
            "founder_pubkey": response.get("founder_pubkey", ""),
            "privacy_mode": "private",
        }

    try:
        group_id = response["group_id"]
        group_name = response["group_name"]
        founder_pubkey = response["founder_pubkey"]
        grant_blob = base64.b64decode(response["grant_blob_b64"].encode("ascii"))
        default_role = response.get("default_role", "member")
        issued_at = response["issued_at"]
        expires_at = response["expires_at"]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"malformed admin response: {exc}"
        )
    privacy_mode = response.get("privacy_mode", "open") or "open"
    founder_display_name = response.get("founder_display_name", "") or ""
    symkey_envelope_b64 = response.get("symkey_envelope_b64", "") or ""
    symkey_envelope = (
        base64.b64decode(symkey_envelope_b64.encode("ascii"))
        if symkey_envelope_b64
        else b""
    )

    # Persist the joiner-side view: group cached, self as member, the
    # grant blob retained for later challenge-response use. Replication
    # Of the full member roster + admin set arrives in.
    async with get_session() as session:
        existing_group = await session.get(Group, group_id)
        now = iso_now()
        if existing_group is None:
            session.add(
                Group(
                    id=group_id,
                    name=group_name,
                    founder_pubkey=founder_pubkey,
                    created_at=now,
                    deleted_at="",
                    privacy_mode=privacy_mode,
                    founder_address=body.admin_address,
                    group_symkey_enc=symkey_envelope or None,
                )
            )
            # Seed default roles locally so the UI can render them.
            for role_name, perms in DEFAULT_ROLES.items():
                session.add(
                    GroupRole(
                        group_id=group_id,
                        name=role_name,
                        permissions_json=encode_role_permissions(perms),
                        created_at=now,
                        updated_at=now,
                    )
                )
            # Stub the founder as a member with their advertised display
            # name so the joiner's Members tab isn't empty.
            session.add(
                GroupMember(
                    group_id=group_id,
                    pubkey=founder_pubkey,
                    joined_at=now,
                    last_heartbeat_at="",
                    # Seed online so the founder doesn't flash
                    # "offline" until their first beacon arrives.
                    last_seen_at=now,
                    display_name=founder_display_name,
                    peer_address=body.admin_address,
                )
            )
            session.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=founder_pubkey,
                    role_name="founder",
                    assigned_by_pubkey=founder_pubkey,
                    assigned_at=now,
                )
            )

        existing_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == me)
                )
            )
        ).scalar_one_or_none()
        if existing_member is None:
            session.add(
                GroupMember(
                    group_id=group_id,
                    pubkey=me,
                    joined_at=now,
                    last_heartbeat_at=now,
                    last_seen_at=now,
                    display_name=my_name,
                    member_x25519_pub=my_x25519,
                    node_id=get_or_create_node_uuid(),
                )
            )
            session.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=me,
                    role_name=default_role,
                    assigned_by_pubkey=founder_pubkey,
                    assigned_at=now,
                )
            )
            # /47: the joiner never receives a roster.update about
            # itself, so insert its own "joined the chat" line here (the
            # approval/decision path does this for private groups; the
            # open-join path landed here without it).
            _sid = f"sysjoin-{me}-{now}"
            if await session.get(GroupMessage, (group_id, _sid)) is None:
                session.add(GroupMessage(
                    group_id=group_id, msg_id=_sid,
                    sender_pubkey="system", sender_name="",
                    body=f"{my_name or me[:8]} joined the chat",
                    sent_at=now, received_at=now,
                ))

        import secrets as _secrets

        session.add(
            GroupGrant(
                id=_secrets.token_hex(16),
                group_id=group_id,
                member_pubkey=me,
                issued_by_pubkey=founder_pubkey,
                issued_at=issued_at,
                expires_at=expires_at,
                nonce="",
                signature=grant_blob,
                roles_json='["' + default_role + '"]',
            )
        )
        await session.commit()

    # Live-refresh the joiner's chat so its own "joined" line shows
    # without a manual re-open.
    from nexus.runtime import event_bus

    await event_bus.publish({"type": "group.message", "group_id": group_id})

    await write_audit_event(
        action="group.join.accepted",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} admin_address={body.admin_address} "
            f"role={default_role}"
        ),
    )

    return {
        "group_id": group_id,
        "group_name": group_name,
        "founder_pubkey": founder_pubkey,
        "my_role": default_role,
        "expires_at": expires_at,
    }


# ---- roster refresh (post-ship) ----------------------------------------


@router.post(
    "/{group_id}/refresh_members",
    summary="Pull the authoritative roster from the founder and reconcile locally",
)
async def post_refresh_members(group_id: str) -> dict:
    """Replace local GroupMember + GroupMemberRole rows with the founder's view.

    Keeps the local node's own grant intact (the grant table is not
    touched). If the local node is the founder, the call is a no-op
    — the founder *is* the authoritative source.
    """
    async with get_session() as session:
        g = await _require_group_exists(session, group_id)
        me = await _require_perm(session, group_id, PERM_GROUP_READ)

        if (g.founder_pubkey or "") == me:
            return {"ok": True, "skipped": "this node is the founder", "members": 0}

        founder_address = g.founder_address or ""
        # The founder's node_id lets the roster pull route over
        # the relay when the founder is behind NAT.
        founder_member = (
            await session.execute(
                select(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == g.founder_pubkey)
                )
            )
        ).scalar_one_or_none()
        founder_node_id = (founder_member.node_id if founder_member else "") or ""
        if not founder_address and not founder_node_id:
            raise HTTPException(
                status_code=409,
                detail="no founder address cached — re-join the group to populate it",
            )

    status, response = await _post_to_admin(
        founder_address,
        "/peer/group/roster",
        {"group_id": group_id},
        founder_node_id,
        group_id=group_id,
    )
    if status != 200:
        raise HTTPException(
            status_code=status if status >= 400 else 502,
            detail=response.get("detail") or response.get("error") or response,
        )

    members = response.get("members") or []
    roles = response.get("roles") or []
    relays = response.get("relays") or []
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        now = iso_now()
        # Additive upsert only — no wholesale wipe. Removals
        # converge through the dedicated bus frames (symkey.rotate on
        # kick, roles.def delete, relay.update remove). Wiping here
        # raced with concurrent probe_group_relays / bind operations
        # (StaleDataError on commit) and thrashed the local DB on every
        # SSE-triggered call.
        for r in roles:
            name = str(r.get("name") or "")
            if not name:
                continue
            existing_role = await session.get(GroupRole, (group_id, name))
            perms_blob = encode_role_permissions(r.get("permissions") or [])
            if existing_role is None:
                session.add(
                    GroupRole(
                        group_id=group_id,
                        name=name,
                        permissions_json=perms_blob,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing_role.permissions_json = perms_blob
                existing_role.updated_at = now
        for m in members:
            pubkey = str(m.get("pubkey") or "")
            if not pubkey:
                continue
            existing_member = await session.get(GroupMember, (group_id, pubkey))
            if existing_member is None:
                session.add(
                    GroupMember(
                        group_id=group_id,
                        pubkey=pubkey,
                        joined_at=str(m.get("joined_at") or ""),
                        last_heartbeat_at="",
                        display_name=str(m.get("display_name") or ""),
                        peer_address=str(m.get("peer_address") or ""),
                        node_id=str(m.get("node_id") or ""),
                    )
                )
            else:
                if m.get("display_name"):
                    existing_member.display_name = str(m.get("display_name"))
                if m.get("peer_address"):
                    existing_member.peer_address = str(m.get("peer_address"))
                if m.get("node_id"):
                    existing_member.node_id = str(m.get("node_id"))
            for role_name in (m.get("roles") or []):
                rn = str(role_name)
                if not rn:
                    continue
                existing_assignment = await session.get(
                    GroupMemberRole, (group_id, pubkey, rn)
                )
                if existing_assignment is None:
                    session.add(
                        GroupMemberRole(
                            group_id=group_id,
                            member_pubkey=pubkey,
                            role_name=rn,
                            assigned_by_pubkey=response.get("founder_pubkey", ""),
                            assigned_at=now,
                        )
                    )
        for rb in relays:
            relay_url = str(rb.get("relay_url") or "").strip()
            if not relay_url:
                continue
            existing_binding = await session.get(
                GroupRelayBinding, (group_id, relay_url)
            )
            if existing_binding is None:
                session.add(
                    GroupRelayBinding(
                        group_id=group_id,
                        relay_url=relay_url,
                        operator_pubkey=str(rb.get("operator_pubkey") or ""),
                        registered_at=now,
                        last_seen_at="",
                        status="active",
                    )
                )
            else:
                existing_binding.status = "active"
        await session.commit()

    return {
        "ok": True,
        "members": len(members),
        "roles": len(roles),
        "relays": len(relays),
    }


# ---- 16.5 targeted invitation push --------------------------------------


async def _push_invitation_offer(
    *,
    peer_address: str,
    token: str,
    group_id: str,
    group_name: str,
    founder_pubkey: str,
    founder_address: str,
    target_peer_label: str,
) -> tuple[bool, str]:
    """POST an invitation offer to the recipient peer. Return (ok, detail)."""
    status, response = await _post_to_admin(
        peer_address,
        "/peer/group/invitation_offer",
        {
            "token": token,
            "group_id": group_id,
            "group_name": group_name,
            "founder_pubkey": founder_pubkey,
            "founder_address": founder_address,
            "target_peer_label": target_peer_label,
        },
    )
    if 200 <= status < 300:
        return True, "delivered"
    return False, (response.get("detail") or response.get("error") or f"http {status}")


@router.post(
    "/{group_id}/invite_friends",
    summary="Mint invite tokens for trusted peers and push them",
)
async def post_invite_friends(group_id: str, body: InviteFriendsBody) -> dict:
    if not body.peer_ips:
        raise HTTPException(status_code=400, detail="peer_ips must be non-empty")

    me_addr = get_node_identity()
    async with get_session() as session:
        group = await _require_group_exists(session, group_id)
        founder_pubkey = await _require_perm(session, group_id, PERM_GROUP_INVITE)

        # Look up peers by either their primary key OR their resolved_ip,
        # so the picker can pass whichever the UI is showing.
        matched = (
            await session.execute(
                select(Peer).where(
                    (Peer.ip.in_(body.peer_ips))
                    | (Peer.resolved_ip.in_(body.peer_ips))
                )
            )
        ).scalars().all()
        peers_by_ip: dict[str, Peer] = {}
        for p in matched:
            peers_by_ip[p.ip] = p
            if p.resolved_ip:
                peers_by_ip[p.resolved_ip] = p

        results: list[dict] = []
        for ip in body.peer_ips:
            peer = peers_by_ip.get(ip)
            if peer is None:
                results.append({
                    "peer_ip": ip,
                    "ok": False,
                    "detail": "peer not found in local DB",
                })
                continue
            status = peer.status or ""
            if not status.startswith("trusted"):
                results.append({
                    "peer_ip": ip,
                    "ok": False,
                    "detail": f"peer status is {status!r}, expected trusted*",
                })
                continue

            target_label = _peer_display_label(peer)
            target_addr = peer.resolved_ip or peer.ip

            # Skip duplicates: if a pending or accepted sender offer
            # already exists for this peer in this group, don't mint a
            # second token. The user gets a clear signal instead of a
            # silent duplicate.
            prior = (
                await session.execute(
                    select(GroupInvitationOffer).where(
                        (GroupInvitationOffer.role == "sender")
                        & (GroupInvitationOffer.group_id == group_id)
                        & (GroupInvitationOffer.target_peer_label == target_label)
                        & (GroupInvitationOffer.status.in_(("pending", "accepted")))
                    )
                )
            ).scalars().first()
            if prior is not None:
                detail = (
                    "already a member"
                    if prior.status == "accepted"
                    else "already invited (pending)"
                )
                results.append({
                    "peer_ip": ip,
                    "ok": False,
                    "detail": detail,
                    "target_peer_label": target_label,
                })
                continue

            invite = await group_invite.mint_invite(
                session=session,
                group_id=group_id,
                slot_cap=1,
                created_by_pubkey=founder_pubkey,
            )
            now = iso_now()
            session.add(
                GroupInvitationOffer(
                    token=invite.token,
                    role="sender",
                    group_id=group_id,
                    group_name=group.name or "",
                    founder_pubkey=founder_pubkey,
                    founder_address=me_addr,
                    target_peer_label=target_label,
                    status="pending",
                    created_at=now,
                    responded_at="",
                )
            )
            await session.flush()

            ok, detail = await _push_invitation_offer(
                peer_address=target_addr,
                token=invite.token,
                group_id=group_id,
                group_name=group.name or "",
                founder_pubkey=founder_pubkey,
                founder_address=me_addr,
                target_peer_label=target_label,
            )
            results.append({
                "peer_ip": ip,
                "ok": ok,
                "detail": detail,
                "token": invite.token,
                "target_peer_label": target_label,
            })

        await session.commit()

    await write_audit_event(
        action="group.invite.friends",
        actor="local",
        task_id="",
        details=(
            f"group_id={group_id} count={len(body.peer_ips)} "
            f"delivered={sum(1 for r in results if r['ok'])}"
        ),
    )
    return {"results": results}


@router.get(
    "/{group_id}/invitations/sent",
    summary="List targeted invitation offers this node sent",
)
async def list_sent_invitations(group_id: str) -> dict:
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        rows = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.group_id == group_id)
                    & (GroupInvitationOffer.role == "sender")
                )
            )
        ).scalars().all()
    return {"offers": [_offer_summary(r) for r in rows]}


@router.post(
    "/{group_id}/invitations/{token}/resend",
    summary="Resend an existing targeted invitation to the same peer",
)
async def post_resend_invitation(group_id: str, token: str) -> dict:
    me_addr = get_node_identity()
    async with get_session() as session:
        await _require_group_exists(session, group_id)
        await _require_perm(session, group_id, PERM_GROUP_INVITE)
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == token)
                    & (GroupInvitationOffer.role == "sender")
                    & (GroupInvitationOffer.group_id == group_id)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="invitation not found")

        # Resending re-opens the offer so the recipient can see it again.
        row.status = "pending"
        row.responded_at = ""

        peers = (
            await session.execute(
                select(Peer).where(
                    (Peer.display_name == row.target_peer_label)
                    | (Peer.ip == row.target_peer_label)
                )
            )
        ).scalars().all()
        target_addr = ""
        for p in peers:
            if (p.status or "") in (
                "trusted",
                "trusted_pending_in",
                "trusted_pending_out",
            ):
                target_addr = p.resolved_ip or p.ip
                break
        if not target_addr:
            await session.commit()
            return {"ok": False, "detail": "target peer no longer trusted"}

        ok, detail = await _push_invitation_offer(
            peer_address=target_addr,
            token=row.token,
            group_id=row.group_id,
            group_name=row.group_name or "",
            founder_pubkey=row.founder_pubkey or "",
            founder_address=me_addr,
            target_peer_label=row.target_peer_label or "",
        )
        await session.commit()

    return {"ok": ok, "detail": detail, "token": token}


# ---- 16.6 joiner-side accept/reject -------------------------------------


@invitations_router.get(
    "/incoming",
    summary="List pending targeted invitations sent to this node",
)
async def list_incoming_invitations() -> dict:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.role == "recipient")
                    & (GroupInvitationOffer.status == "pending")
                )
            )
        ).scalars().all()
    return {"offers": [_offer_summary(r) for r in rows]}


async def _load_recipient_offer(token: str) -> GroupInvitationOffer:
    async with get_session() as session:
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == token)
                    & (GroupInvitationOffer.role == "recipient")
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="invitation not found")
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"invitation already {row.status}",
            )
        return row


@invitations_router.post(
    "/{token}/accept",
    summary="Accept a targeted invitation and join the group",
)
async def post_accept_invitation(token: str) -> dict:
    row_snapshot = await _load_recipient_offer(token)
    founder_address = row_snapshot.founder_address or ""
    if not founder_address:
        raise HTTPException(
            status_code=400, detail="invitation has no founder address"
        )
    # Reuse the standard join path. It handles both open and private modes.
    join_result = await post_join_group(
        JoinGroupBody(
            admin_address=founder_address,
            invite_token=token,
            joiner_address=get_node_identity(),
        )
    )

    # Flip the local recipient row to 'accepted' regardless of mode —
    # the joiner has done their part. (Private mode then waits for the
    # admin's decision via /peer/group/join_decision.)
    async with get_session() as session:
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == token)
                    & (GroupInvitationOffer.role == "recipient")
                )
            )
        ).scalar_one()
        row.status = "accepted"
        row.responded_at = iso_now()
        await session.commit()

    # Notify the founder so their sender-side row flips out of 'pending'.
    # Best-effort: if the push fails, the founder will still see the
    # joiner appear in /local/groups/{id} once they refresh.
    pushed_ok = False
    if founder_address:
        try:
            push_status, _resp = await _post_to_admin(
                founder_address,
                "/peer/group/invitation_accept",
                {"token": token},
            )
            pushed_ok = 200 <= push_status < 300
        except Exception:
            pushed_ok = False

    return {"ok": True, "pushed_ok": pushed_ok, "join_result": join_result}


@invitations_router.post(
    "/{token}/reject",
    summary="Decline a targeted invitation and notify the founder",
)
async def post_reject_invitation(token: str) -> dict:
    row_snapshot = await _load_recipient_offer(token)
    founder_address = row_snapshot.founder_address or ""

    pushed_ok = False
    detail = ""
    if founder_address:
        status, response = await _post_to_admin(
            founder_address,
            "/peer/group/invitation_decline",
            {"token": token},
        )
        pushed_ok = 200 <= status < 300
        if not pushed_ok:
            detail = response.get("detail") or response.get("error") or f"http {status}"

    async with get_session() as session:
        row = (
            await session.execute(
                select(GroupInvitationOffer).where(
                    (GroupInvitationOffer.token == token)
                    & (GroupInvitationOffer.role == "recipient")
                )
            )
        ).scalar_one()
        row.status = "rejected"
        row.responded_at = iso_now()
        await session.commit()

    await write_audit_event(
        action="group.invitation.rejected",
        actor="local",
        task_id="",
        details=f"token={token[:8]} pushed_ok={pushed_ok}",
    )
    return {"ok": True, "pushed_ok": pushed_ok, "detail": detail}


__all__ = ["router", "invitations_router", "propagate_local_display_name"]


# ---- display-name propagation (post-ship) ------------------------------


async def propagate_local_display_name(new_name: str) -> dict:
    """Sweep local GroupMember rows + push the new name to each founder.

    Called from the settings update handler when ``user_display_name``
    changes. Best-effort on the remote leg — if the founder is offline
    the joiner's next roster refresh will overwrite the local row from
    the (stale) admin view, so this isn't load-bearing for correctness,
    but it keeps the cross-node view in sync when the founder is up.

    Returns a small per-group summary dict (mostly for tests + audit).
    """
    import base64 as _b64
    from nexus.security import group_grant
    from nexus.security.group_keys import get_local_group_privkey
    from nexus.api.group_peer import _display_name_nonce

    new_name = (new_name or "").strip()[:64]
    me = get_local_group_pubkey()
    privkey = get_local_group_privkey()
    summary: list[dict] = []

    async with get_session() as session:
        member_rows = (
            await session.execute(
                select(GroupMember).where(GroupMember.pubkey == me)
            )
        ).scalars().all()
        for m in member_rows:
            m.display_name = new_name
        await session.commit()

        # Re-query so we have stable group_ids to iterate without
        # tripping over expire_on_commit.
        my_groups = (
            await session.execute(
                select(GroupMember.group_id).where(GroupMember.pubkey == me)
            )
        ).fetchall()

    for (group_id,) in my_groups:
        async with get_session() as session:
            g = await session.get(Group, group_id)
            if g is None or g.deleted_at:
                continue
            founder_address = g.founder_address or ""
            grant = (
                await session.execute(
                    select(GroupGrant)
                    .where(
                        (GroupGrant.group_id == group_id)
                        & (GroupGrant.member_pubkey == me)
                    )
                    .order_by(GroupGrant.issued_at.desc())
                )
            ).scalars().first()

        # Founder == us: no remote push needed.
        if not founder_address or (g.founder_pubkey or "") == me:
            summary.append({"group_id": group_id, "pushed": False, "reason": "local"})
            continue
        if grant is None or not grant.signature:
            summary.append({"group_id": group_id, "pushed": False, "reason": "no_grant"})
            continue

        nonce = _display_name_nonce(new_name)
        signature = group_grant.sign_challenge(
            grant_blob=grant.signature,
            nonce=nonce,
            member_privkey=privkey,
        )
        status, _resp = await _post_to_admin(
            founder_address,
            "/peer/group/update_display_name",
            {
                "group_id": group_id,
                "member_pubkey": me,
                "display_name": new_name,
                "grant_blob_b64": _b64.b64encode(grant.signature).decode("ascii"),
                "signature_b64": _b64.b64encode(signature).decode("ascii"),
            },
        )
        summary.append({
            "group_id": group_id,
            "pushed": 200 <= status < 300,
            "status": status,
        })

    return {"updated_locally": len(my_groups), "results": summary}
