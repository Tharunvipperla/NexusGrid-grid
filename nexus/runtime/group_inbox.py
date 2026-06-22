"""Replicated pending-join-request inbox.

Today's flow before 

* Joiner POSTs ``/peer/group/join_request`` to one admin's node.
* That node parks the row in ``GroupPendingJoinRequest`` locally.
* No other admin sees it.

That's fine when the founder is the only admin. The moment a second
admin exists, only one of them can ever approve any given request —
whichever node received the HTTP call.

fixes that by *replicating* the row across every admin. The
receiving admin publishes a ``pending.request`` frame; every other
admin opens it and mirrors the row into their own DB. When any
admin approves, they publish a ``pending.decision`` frame so the
others flip their local row out of ``pending``.

routes a published frame through the group's **relay hosts**
(members holding ``relay:host``): the publisher hands the opaque
sealed frame to each relay host's ``/peer/group/publish``, and every
relay fans it out to the frame's audience. If no relay host is
reachable, :func:`publish_frame` falls back to direct fan-out to the
audience so a relay-less group still works.

Idempotency:

* Each frame carries a ``frame_id`` (UUID); a per-process
  :class:`FrameDedupeCache` skips exact replays.
* ``pending.request`` payload's ``request_id`` is the row PK; an
  ``INSERT OR IGNORE``-style guard skips re-insert if the row
  already exists locally.
* ``pending.decision``: a row already in a terminal state stays
  there — first-approver-wins.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Callable, Optional

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.runtime import event_bus
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_open,
    ecies_seal,
    mint_group_symkey,
)
from nexus.security.group_frame import (
    FrameDedupeCache,
    GroupFrame,
    OpenedFrame,
    seal_frame,
)
from nexus.security.group_grant import sign_grant  # noqa: F401  (re-exported context)
from nexus.security.group_keys import (
    get_local_group_privkey,
    get_local_group_pubkey,
)
from nexus.security.group_permissions import (
    PERM_GROUP_APPROVE,
    PERM_MEMBER_KICK,
    PERM_MEMBER_MUTE,
    PERM_RELAY_HOST,
    PERM_RELAY_SHARE_CONTENT,
    PERM_ROLE_ASSIGN,
    decode_role_permissions,
    encode_role_permissions,
    has_permission,
)
from nexus.storage import get_session
from nexus.storage.models import (
    Group,
    GroupFrameLog,
    GroupGrant,
    GroupMember,
    GroupMemberRole,
    GroupMessage,
    GroupPendingJoinRequest,
    GroupRelayBinding,
    GroupRelayCode,
    GroupRole,
)
from nexus.utils.time import iso_now


_log = logging.getLogger("nexus.runtime.group_inbox")


# Frame type discriminators on the wire.
FRAME_PENDING_REQUEST = "pending.request"
FRAME_PENDING_DECISION = "pending.decision"
FRAME_ROSTER_UPDATE = "roster.update"
FRAME_SYMKEY_ROTATE = "symkey.rotate"
FRAME_RELAY_UPDATE = "relay.update"
FRAME_ROLES_ASSIGN = "roles.assign"
FRAME_ROLES_DEF = "roles.def"
# Group chat.
FRAME_CHAT_MESSAGE = "chat.message"
FRAME_CHAT_MUTE = "chat.mute"
FRAME_CHAT_DELETE = "chat.delete"
# Member liveness beacon (ephemeral — not archived in the frame log).
FRAME_PRESENCE_BEACON = "presence.beacon"
# Counterparty-signed usage receipt (durable — archived for catch-up).
FRAME_USAGE_RECEIPT = "usage.receipt"
# Group metadata (avatar) — durable so late joiners catch up.
FRAME_GROUP_META = "group.meta"
# A group's canonical relay module source (durable — members copy it
# to host the group's relay; fingerprint-gated to the group's frozen build).
FRAME_RELAY_CODE = "relay.code"

# Map outbound frame types to UI event-bus event types so the
# *publisher's* own UI also updates (broadcasts exclude self, so the
# apply_* handlers don't fire on the originator).
_FRAME_TO_UI_EVENT = {
    FRAME_PENDING_REQUEST: "group.pending",
    FRAME_PENDING_DECISION: "group.pending",
    FRAME_ROSTER_UPDATE: "group.roster",
    FRAME_SYMKEY_ROTATE: "group.roster",
    FRAME_RELAY_UPDATE: "group.relays",
    FRAME_ROLES_ASSIGN: "group.roster",
    FRAME_ROLES_DEF: "group.roster",
    FRAME_CHAT_MESSAGE: "group.message",
    FRAME_CHAT_MUTE: "group.roster",
    FRAME_CHAT_DELETE: "group.message",
    FRAME_GROUP_META: "group.roster",
    FRAME_RELAY_CODE: "group.relays",
}


# Per-process dedupe cache. + will scope this per channel; for
# now one cache across all groups is sufficient (frame_ids are UUIDs
# so collisions across groups are negligible).
_DEDUPE = FrameDedupeCache(capacity=4096)


# ---- payload schemas ---------------------------------------------------


def _request_payload(row: GroupPendingJoinRequest) -> dict:
    return {
        "request_id": row.id,
        "group_id": row.group_id,
        "joiner_pubkey": row.joiner_pubkey,
        "joiner_address": row.joiner_address or "",
        "joiner_x25519_pub": row.joiner_x25519_pub or "",
        "joiner_node_id": row.joiner_node_id or "",
        "invite_token": row.invite_token,
        "message": row.message or "",
        "display_name": row.display_name or "",
        "created_at": row.created_at or "",
    }


def _decision_payload(row: GroupPendingJoinRequest) -> dict:
    return {
        "request_id": row.id,
        "group_id": row.group_id,
        "joiner_pubkey": row.joiner_pubkey,
        "status": row.status,
        "decided_at": row.decided_at or "",
        "decided_by_pubkey": row.decided_by_pubkey or "",
        "decision_reason": row.decision_reason or "",
    }


# ---- handlers ----------------------------------------------------------


async def apply_pending_request(opened: OpenedFrame) -> bool:
    """Mirror a published ``pending.request`` into local state.

    Returns True on insert, False on no-op (already present).
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    if str(payload.get("group_id") or "") != opened.channel:
        # Sanity check: the inner group_id must match the frame's channel.
        _log.warning("apply_pending_request: payload group_id mismatch; dropping")
        return False
    request_id = str(payload.get("request_id") or "")
    if not request_id:
        return False

    async with get_session() as session:
        existing = await session.get(GroupPendingJoinRequest, request_id)
        if existing is not None:
            return False
        session.add(
            GroupPendingJoinRequest(
                id=request_id,
                group_id=str(payload.get("group_id") or ""),
                joiner_pubkey=str(payload.get("joiner_pubkey") or ""),
                joiner_address=str(payload.get("joiner_address") or ""),
                joiner_x25519_pub=str(payload.get("joiner_x25519_pub") or ""),
                joiner_node_id=str(payload.get("joiner_node_id") or ""),
                invite_token=str(payload.get("invite_token") or ""),
                message=str(payload.get("message") or ""),
                display_name=str(payload.get("display_name") or ""),
                status="pending",
                created_at=str(payload.get("created_at") or iso_now()),
            )
        )
        await session.commit()
    await event_bus.publish({"type": "group.pending", "group_id": opened.channel})
    return True


async def apply_pending_decision(opened: OpenedFrame) -> bool:
    """Flip a local pending row to match a published decision.

    Returns True on update, False on no-op (row absent or already in
    terminal state matching the decision).
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    if str(payload.get("group_id") or "") != opened.channel:
        _log.warning("apply_pending_decision: payload group_id mismatch; dropping")
        return False
    request_id = str(payload.get("request_id") or "")
    if not request_id:
        return False
    incoming_status = str(payload.get("status") or "")
    if incoming_status not in ("approved", "rejected"):
        return False

    async with get_session() as session:
        row = await session.get(GroupPendingJoinRequest, request_id)
        if row is None:
            # We never saw the request — drop. The publishing admin
            # may have lost our address; the user can manually re-pull.
            return False
        if row.status != "pending":
            return False
        row.status = incoming_status
        row.decided_at = str(payload.get("decided_at") or iso_now())
        row.decided_by_pubkey = str(payload.get("decided_by_pubkey") or "")
        row.decision_reason = str(payload.get("decision_reason") or "")
        await session.commit()
    await event_bus.publish({"type": "group.pending", "group_id": opened.channel})
    return True


async def apply_roster_update(opened: OpenedFrame) -> bool:
    """Apply a ``roster.update`` add-delta — a member just joined.

    the delta describes one freshly-joined member, who
    always holds the default ``member`` role (``issue_member_grant``
    assigns nothing more at join). The handler upserts that member's
    ``GroupMember`` row + the ``member`` role assignment — it can never
    grant ``admin``/``founder``, so a forged frame can at worst add an
    inert phantom ``member`` (with no grant it cannot authenticate).

    Returns True on insert/update, False on a malformed/non-add frame.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_roster_update: group_id mismatch; dropping")
        return False
    if str(payload.get("action") or "") != "add":
        return False
    member = payload.get("member") or {}
    pubkey = str(member.get("pubkey") or "")
    if not pubkey:
        return False

    async with get_session() as session:
        row = await session.get(GroupMember, (group_id, pubkey))
        if row is None:
            session.add(
                GroupMember(
                    group_id=group_id,
                    pubkey=pubkey,
                    joined_at=str(member.get("joined_at") or iso_now()),
                    last_heartbeat_at="",
                    # A just-joined member is online — seed last_seen
                    # so they don't flash "offline" until their first beacon.
                    last_seen_at=iso_now(),
                    display_name=str(member.get("display_name") or ""),
                    peer_address=str(member.get("peer_address") or ""),
                    node_id=str(member.get("node_id") or ""),
                )
            )
            # "X joined the chat" system message (like other chat
            # apps). Deterministic msg_id dedupes across nodes.
            await _insert_join_system_message(
                session, group_id, pubkey,
                str(member.get("display_name") or "") or pubkey[:8],
                str(member.get("joined_at") or ""),
            )
        else:
            if member.get("display_name"):
                row.display_name = str(member.get("display_name"))
            if member.get("peer_address"):
                row.peer_address = str(member.get("peer_address"))
            if member.get("node_id"):
                row.node_id = str(member.get("node_id"))
        # New joiners always hold exactly the default ``member`` role.
        existing_role = await session.get(
            GroupMemberRole, (group_id, pubkey, "member")
        )
        if existing_role is None:
            session.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=pubkey,
                    role_name="member",
                    assigned_by_pubkey=opened.sender_pubkey,
                    assigned_at=iso_now(),
                )
            )
        await session.commit()
    await event_bus.publish({"type": "group.roster", "group_id": group_id})
    return True


async def apply_symkey_rotate(opened: OpenedFrame) -> bool:
    """Apply a ``symkey.rotate`` frame — a member was kicked.

    the kicker minted a fresh group symkey and sealed a copy
    to every *remaining* member's X25519 pubkey. The frame itself is
    AEAD-encrypted with the **old** symkey (which every current member,
    including the kicked one, still holds), but the per-member ECIES
    envelopes inside are opaque to everyone but their addressee — so
    the kicked member, who gets no envelope, never sees the new key.

    This handler (a) adopts the new symkey if an envelope is addressed
    to this node, and (b) drops the kicked member's local rows so the
    roster converges. The kicked member's *own* node keeps its rows —
    pre-kick history stays readable.

    The caller (:func:`dispatch_inbound_frame` / :func:`relay_inbound_frame`)
    has already verified the sender holds ``member:kick``.

    Returns True on a well-formed frame, False on malformed.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_symkey_rotate: group_id mismatch; dropping")
        return False
    kicked_pubkey = str(payload.get("kicked_pubkey") or "")
    envelopes = payload.get("envelopes") or {}
    if not isinstance(envelopes, dict):
        return False
    me = get_local_group_pubkey()

    async with get_session() as session:
        # (a) Adopt the new symkey if there's an envelope sealed to us.
        my_envelope_b64 = envelopes.get(me)
        if my_envelope_b64:
            try:
                new_symkey = ecies_open(
                    base64.b64decode(str(my_envelope_b64).encode("ascii")),
                    get_local_group_privkey(),
                )
                group = await session.get(Group, group_id)
                if group is not None:
                    group.group_symkey_enc = ecies_seal(
                        new_symkey,
                        derive_x25519_pubkey_hex(get_local_group_privkey()),
                    )
            except Exception:
                _log.warning(
                    "apply_symkey_rotate: envelope open failed", exc_info=True
                )

        # (b) Drop the kicked member's rows — unless it's us (a kicked
        # node keeps its own rows so pre-kick history stays readable).
        if kicked_pubkey and kicked_pubkey != me:
            gone = await session.get(GroupMember, (group_id, kicked_pubkey))
            gone_name = (gone.display_name if gone else "") or kicked_pubkey[:8]
            # "X left/was removed" system message. A self-published
            # rotation (sender == kicked) is a voluntary leave; otherwise a kick.
            left = opened.sender_pubkey == kicked_pubkey
            sys_id = f"sysleave-{kicked_pubkey}-{iso_now()[:19]}"
            if await session.get(GroupMessage, (group_id, sys_id)) is None:
                session.add(GroupMessage(
                    group_id=group_id, msg_id=sys_id, sender_pubkey="system",
                    sender_name="",
                    body=f"{gone_name} {'left the chat' if left else 'was removed from the chat'}",
                    sent_at=iso_now(), received_at=iso_now(),
                ))
            await session.execute(
                delete(GroupMember).where(
                    (GroupMember.group_id == group_id)
                    & (GroupMember.pubkey == kicked_pubkey)
                )
            )
            await session.execute(
                delete(GroupMemberRole).where(
                    (GroupMemberRole.group_id == group_id)
                    & (GroupMemberRole.member_pubkey == kicked_pubkey)
                )
            )
            await session.execute(
                delete(GroupGrant).where(
                    (GroupGrant.group_id == group_id)
                    & (GroupGrant.member_pubkey == kicked_pubkey)
                )
            )
        await session.commit()
    await event_bus.publish({"type": "group.roster", "group_id": group_id})
    return True


async def apply_relay_update(opened: OpenedFrame) -> bool:
    """Apply a ``relay.update`` delta — a relay binding was added/removed.

    ``GroupRelayBinding`` rows are otherwise node-local state.
    This delta replicates an add/remove to every member so the group's
    relay list converges — e.g. when a member's tunnel URL rotates and
    they re-bind. The caller has already verified the sender holds
    ``relay:host``, so a forged frame can't rewrite the relay set.

    Returns True on a well-formed frame, False on malformed.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_relay_update: group_id mismatch; dropping")
        return False
    action = str(payload.get("action") or "")
    relay_url = str(payload.get("relay_url") or "").strip()
    if action not in (
        "add", "remove", "config", "content_share", "content_revoke"
    ) or not relay_url:
        return False

    async with get_session() as session:
        row = await session.get(GroupRelayBinding, (group_id, relay_url))
        if action in ("content_share", "content_revoke"):
            # Replicate a consensual content-share authorization so
            # every member SEES which relay may read content. The gate
            # (relay:share_content) was checked before dispatch.
            if row is None:
                return False
            if action == "content_share":
                row.content_share = 1
                row.content_share_by = str(
                    payload.get("share_by") or opened.sender_pubkey
                )
                row.content_share_at = iso_now()
            else:
                row.content_share = 0
                row.content_share_by = ""
                row.content_share_at = ""
        elif action == "add":
            if row is None:
                session.add(
                    GroupRelayBinding(
                        group_id=group_id,
                        relay_url=relay_url,
                        operator_pubkey=str(
                            payload.get("operator_pubkey")
                            or opened.sender_pubkey
                        ),
                        registered_at=iso_now(),
                        last_seen_at="",
                        status="active",
                    )
                )
            else:
                row.status = "active"
        elif action == "config":
            # Replicate label/region/priority adjustments. Only
            # keys present in the payload are touched.
            if row is None:
                return False
            if "label" in payload:
                row.label = str(payload.get("label") or "")
            if "region" in payload:
                row.region = str(payload.get("region") or "")
            if "priority" in payload:
                row.priority = int(payload.get("priority") or 0)
        else:  # remove
            if row is None:
                return False
            row.status = "retired"
        await session.commit()
    await event_bus.publish({"type": "group.relays", "group_id": group_id})
    return True


# ---- group chat ------------------------------------------------


async def _insert_join_system_message(
    session: AsyncSession, group_id: str, pubkey: str, name: str,
    joined_at: str = "",
) -> None:
    """Insert a 'X joined the chat' system message (idempotent).

    ``joined_at`` is folded into the id so a member who leaves and rejoins
    gets a fresh 'joined' line (a new joined_at each time).
    """
    sys_id = f"sysjoin-{pubkey}-{joined_at or iso_now()}"
    if await session.get(GroupMessage, (group_id, sys_id)) is not None:
        return
    session.add(GroupMessage(
        group_id=group_id,
        msg_id=sys_id,
        sender_pubkey="system",
        sender_name="",
        body=f"{name} joined the chat",
        sent_at=iso_now(),
        received_at=iso_now(),
    ))


async def _member_is_muted(
    session: AsyncSession, group_id: str, pubkey: str
) -> bool:
    row = await session.get(GroupMember, (group_id, pubkey))
    return bool(row and int(row.muted or 0))


async def apply_chat_message(opened: OpenedFrame) -> bool:
    """Store an inbound ``chat.message`` (dedupe on ``msg_id``).

    Dropped if the sender is muted in this group — defense in depth on
    top of the sender's own local block.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    msg_id = str(payload.get("msg_id") or "")
    if group_id != opened.channel or not msg_id:
        return False
    async with get_session() as session:
        if await _member_is_muted(session, group_id, opened.sender_pubkey):
            return False
        existing = await session.get(GroupMessage, (group_id, msg_id))
        if existing is not None:
            return True  # dedupe — re-apply is a no-op
        session.add(GroupMessage(
            group_id=group_id,
            msg_id=msg_id,
            sender_pubkey=opened.sender_pubkey,
            sender_name=str(payload.get("sender_name") or ""),
            body=str(payload.get("body") or ""),
            sent_at=str(payload.get("sent_at") or iso_now()),
            received_at=iso_now(),
            reply_to=str(payload.get("reply_to") or ""),
            reply_snippet=str(payload.get("reply_snippet") or ""),
            reply_sender=str(payload.get("reply_sender") or ""),
            attach_kind=str(payload.get("attach_kind") or ""),
            attach_name=str(payload.get("attach_name") or ""),
            attach_mime=str(payload.get("attach_mime") or ""),
            attach_size=int(payload.get("attach_size") or 0),
            attach_data=str(payload.get("attach_data") or ""),
        ))
        await session.commit()
    # If this message replies to OUR message, surface a direct
    # "you were replied to" notification (like a WhatsApp reply/mention).
    replied_to_me = (
        str(payload.get("reply_to_pubkey") or "") == get_local_group_pubkey()
    )
    await event_bus.publish({
        "type": "group.message",
        "group_id": group_id,
        "reply_to_me": replied_to_me,
        "reply_sender": str(payload.get("sender_name") or ""),
    })
    # A >5MB attachment isn't in the frame — pull it from the sender
    # in the background, then re-emit so the UI shows it.
    if str(payload.get("attach_kind") or "") == "foreign":
        asyncio.create_task(
            _pull_foreign_attachment(group_id, msg_id, opened.sender_pubkey)
        )
    return True


async def _pull_foreign_attachment(
    group_id: str, msg_id: str, sender_pubkey: str
) -> None:
    """Fetch a sender-hosted attachment, unseal with the group symkey, and
    cache it locally so the download/preview endpoints can serve it."""
    import base64

    from nexus.networking.peer_http import peer_http_post
    from nexus.runtime.chat_attachments import (
        has_blob,
        open_with_symkey,
        store_blob,
    )

    if has_blob(msg_id):
        return
    async with get_session() as session:
        m = await session.get(GroupMember, (group_id, sender_pubkey))
        addr = (m.peer_address or m.node_id or "") if m else ""
        symkey = await _local_symkey(session, group_id)
    if not addr or symkey is None:
        return
    try:
        res = await peer_http_post(
            addr, "/peer/group/attachment_pull",
            {"group_id": group_id, "msg_id": msg_id}, timeout=120.0,
        )
    except Exception:
        _log.debug("foreign attachment pull failed for %s", msg_id, exc_info=True)
        return
    if int(res.get("status") or 0) != 200:
        return
    sealed_b64 = (res.get("body") or {}).get("sealed_b64") or ""
    if not sealed_b64:
        return
    try:
        raw = open_with_symkey(symkey, base64.b64decode(sealed_b64))
    except Exception:
        _log.debug("foreign attachment unseal failed for %s", msg_id, exc_info=True)
        return
    store_blob(msg_id, raw)
    await event_bus.publish({"type": "group.message", "group_id": group_id})


async def apply_chat_mute(opened: OpenedFrame) -> bool:
    """Apply a ``chat.mute`` delta — set/clear a member's muted flag.

    The caller has already verified the sender holds ``member:mute``.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    target = str(payload.get("member_pubkey") or "")
    if group_id != opened.channel or not target:
        return False
    muted = 1 if payload.get("muted") else 0
    async with get_session() as session:
        row = await session.get(GroupMember, (group_id, target))
        if row is None:
            return False
        row.muted = muted
        await session.commit()
    await event_bus.publish({"type": "group.roster", "group_id": group_id})
    return True


async def apply_chat_delete(opened: OpenedFrame) -> bool:
    """Apply a ``chat.delete`` delta — tombstone a message.

    Honored when the sender is the message author OR holds ``member:kick``
    (admin moderation). Idempotent.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    msg_id = str(payload.get("msg_id") or "")
    if group_id != opened.channel or not msg_id:
        return False
    async with get_session() as session:
        row = await session.get(GroupMessage, (group_id, msg_id))
        if row is None:
            return True  # nothing to delete — idempotent
        # Only the author may delete their own message.
        if row.sender_pubkey != opened.sender_pubkey:
            return False
        row.deleted = 1
        row.body = ""
        await session.commit()
    await event_bus.publish({"type": "group.message", "group_id": group_id})
    return True


async def apply_presence_beacon(opened: OpenedFrame) -> bool:
    """Apply a ``presence.beacon`` — bump the sender's ``last_seen_at``.

    Ephemeral: no SSE per beacon (the Members pane polls presence on a
    timer) and never archived in the frame log. The timestamp only moves
    forward and is clamped to now so a skewed clock can't fake the future.
    """
    now = iso_now()
    ts = str(json.loads(opened.payload.decode("utf-8")).get("ts") or now)
    if ts > now:
        ts = now
    async with get_session() as session:
        row = await session.get(
            GroupMember, (opened.channel, opened.sender_pubkey)
        )
        if row is None:
            return False
        if (row.last_seen_at or "") >= ts:
            return True
        row.last_seen_at = ts
        await session.commit()
    return True


async def apply_usage_receipt(opened: OpenedFrame) -> bool:
    """Apply a ``usage.receipt`` frame — fold a counterparty-signed receipt
    into the derived pool ledger.

    . The frame is member-signed (any member may relay a receipt), but
    the *content* is verified against the receipt's own ``consumer_pubkey`` in
    ``store_and_apply`` — so a relayer can't forge or alter the numbers. Durable:
    archived in the frame log so a catch-up peer rebuilds the same ledger.
    """
    from nexus.runtime.usage_receipts import store_and_apply

    payload = json.loads(opened.payload.decode("utf-8"))
    receipt = payload.get("receipt") or {}
    sig = str(payload.get("sig") or "")
    applied = await store_and_apply(receipt, sig)
    if applied:
        await event_bus.publish(
            {"type": "group.pool_stats", "group_id": opened.channel}
        )
    return True


async def apply_roles_assign(opened: OpenedFrame) -> bool:
    """Apply a ``roles.assign`` delta — replace a member's role set.

    ``GroupMemberRole`` rows were otherwise node-local state, so
    a founder/admin granting a role only updated their own DB. This
    delta replicates the new role set to every member so the target
    member's UI (and every other admin's view) converges.

    Authorization: the caller (:func:`dispatch_inbound_frame` /
    :func:`relay_inbound_frame`) has already verified the sender holds
    ``role:assign`` — without that gate a forged frame could grant
    arbitrary roles to anyone.

    Defenses inside this handler:

    * Force ``member`` into the final set (the baseline that prevents
      ``group:read`` lockout).
    * Force ``founder`` if the target *is* the founder (mirrors the
      endpoint's "founder can't be stripped of founder" rule).
    * Drop role names this node doesn't know — a sender on a slightly
      newer schema can't conjure roles into this group.
    * Reject an empty/no-overlap set — at least ``member`` must remain.

    Returns True on a well-formed frame, False on malformed.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_roles_assign: group_id mismatch; dropping")
        return False
    member_pubkey = str(payload.get("member_pubkey") or "")
    raw_roles = payload.get("roles") or []
    if not member_pubkey or not isinstance(raw_roles, list):
        return False

    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return False
        # Only act on a known member of the group. Without this, a
        # roles.assign for a never-seen pubkey would silently create
        # role rows for a member that has no GroupMember row.
        member_row = await session.get(GroupMember, (group_id, member_pubkey))
        if member_row is None:
            return False

        requested = {str(r) for r in raw_roles if isinstance(r, str)}
        requested.add("member")
        if member_pubkey == (g.founder_pubkey or ""):
            requested.add("founder")

        known_rows = (
            await session.execute(
                select(GroupRole.name).where(GroupRole.group_id == group_id)
            )
        ).fetchall()
        known = {row[0] for row in known_rows}
        requested &= known
        if not requested:
            return False

        await session.execute(
            delete(GroupMemberRole).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.member_pubkey == member_pubkey)
            )
        )
        assigned_at = str(payload.get("assigned_at") or iso_now())
        for role_name in requested:
            session.add(
                GroupMemberRole(
                    group_id=group_id,
                    member_pubkey=member_pubkey,
                    role_name=role_name,
                    assigned_by_pubkey=opened.sender_pubkey,
                    assigned_at=assigned_at,
                )
            )
        await session.commit()
    await event_bus.publish({"type": "group.roster", "group_id": group_id})
    return True


# ---- frame-log capture for catch-up ---------------------------


# Default retention window for GroupFrameLog rows. The catch-up endpoint
# only returns frames newer than the requester's high-watermark, so the
# window bounds the "missed while offline" recovery span.
FRAME_LOG_RETENTION_HOURS = 14 * 24  # 14 days


async def capture_frame_to_log(
    *,
    group_id: str,
    frame_id: str,
    envelope: dict,
    frame_type: str,
) -> None:
    """Append one sealed envelope to the local frame log (idempotent).

    Called from every ``apply_*`` handler after a successful commit AND
    from ``publish_frame`` after we seal-and-send a new frame, so every
    node that handles a frame keeps a copy. A node serving
    ``/peer/group/catchup`` returns frames from this log; replaying
    them at the receiver goes back through ``dispatch_inbound_frame``
    + ``FrameDedupeCache`` so re-applies are no-ops.
    """
    if not group_id or not frame_id:
        return
    # Presence beacons are ephemeral liveness pings — never replay
    # A stale "I was online 3 days ago" to a catch-up peer. (usage
    # receipts ARE durable facts and intentionally fall through to the log.)
    if frame_type == FRAME_PRESENCE_BEACON:
        return
    try:
        async with get_session() as session:
            existing = await session.get(
                GroupFrameLog, (group_id, frame_id)
            )
            if existing is not None:
                return
            session.add(
                GroupFrameLog(
                    group_id=group_id,
                    frame_id=frame_id,
                    envelope_json=json.dumps(
                        envelope, separators=(",", ":")
                    ),
                    frame_type=str(frame_type or ""),
                    captured_at=iso_now(),
                )
            )
            await session.commit()
    except Exception:
        _log.debug(
            "capture_frame_to_log(%s/%s) failed", group_id, frame_id[:8],
            exc_info=True,
        )


async def prune_frame_log(retention_hours: int = FRAME_LOG_RETENTION_HOURS) -> int:
    """Drop log rows older than *retention_hours*. Returns rows removed."""
    from datetime import datetime, timedelta, timezone

    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    ).isoformat()
    try:
        async with get_session() as session:
            res = await session.execute(
                delete(GroupFrameLog).where(
                    GroupFrameLog.captured_at < cutoff_iso
                )
            )
            await session.commit()
            return int(res.rowcount or 0)
    except Exception:
        _log.warning("prune_frame_log failed", exc_info=True)
        return 0


async def fetch_log_since(
    group_id: str, since_iso: str, limit: int = 200
) -> list[dict]:
    """Return up to *limit* envelopes captured after ``since_iso``.

    Caller sends them back as a JSON list to a member calling
    ``/peer/group/catchup``. Ordered oldest-first so the requester
    replays them in chronological order — keeps subsequent
    dedupe-cache hits deterministic.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupFrameLog)
                .where(
                    (GroupFrameLog.group_id == group_id)
                    & (GroupFrameLog.captured_at > (since_iso or ""))
                )
                .order_by(GroupFrameLog.captured_at)
                .limit(int(limit))
            )
        ).scalars().all()
    out: list[dict] = []
    for r in rows:
        try:
            env = json.loads(r.envelope_json or "{}")
        except Exception:
            continue
        out.append({
            "frame_id": r.frame_id,
            "frame_type": r.frame_type or "",
            "captured_at": r.captured_at or "",
            "envelope": env,
        })
    return out


AVATAR_MAX_CHARS = 65536  # ~48 KB of image after base64 overhead


def _avatar_valid(avatar: str) -> bool:
    """Empty clears the avatar; otherwise a small image data URL only."""
    if avatar == "":
        return True
    return (
        len(avatar) <= AVATAR_MAX_CHARS
        and avatar.startswith("data:image/")
        and ";base64," in avatar[:64]
    )


async def apply_group_meta(opened: OpenedFrame) -> bool:
    """Apply a ``group.meta`` delta — currently the group avatar.

    Authorization: caller has already verified the sender holds
    ``role:assign`` (same gate as ``roles.def``). The avatar is a small
    ``data:image/...;base64,`` URL, size-capped so a hostile admin can't
    bloat members' databases.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_group_meta: group_id mismatch; dropping")
        return False
    if "avatar" not in payload:
        return False
    avatar = str(payload.get("avatar") or "")
    if not _avatar_valid(avatar):
        return False
    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return False
        g.avatar = avatar
        await session.commit()
    return True


async def apply_relay_code(opened: OpenedFrame) -> bool:
    """Apply a ``relay.code`` frame — store the group's canonical relay
    module source.

    Authorization: the caller has already verified the sender holds
    ``role:assign`` (the founder/admin governance gate, same as ``group.meta``
    / ``roles.def``). Integrity: the source is stored ONLY if its recomputed
    fingerprint equals the group's frozen ``relay_code_fingerprint`` — so even
    an authorized-but-mistaken publisher can't poison the copy with code that
    would later fail the W63 bind check.

    Returns True on store, False on malformed / fingerprint mismatch.
    """
    from nexus.runtime.relay_codeprint import fingerprint_for_bytes

    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_relay_code: group_id mismatch; dropping")
        return False
    source = str(payload.get("source") or "")
    if not source.strip():
        return False
    # Normalize line endings exactly as import_module_source will when the
    # source is written to disk, so this gate's fingerprint == the fingerprint
    # W63 checks at bind time.
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    fp = fingerprint_for_bytes(source.encode("utf-8"))

    async with get_session() as session:
        g = await session.get(Group, group_id)
        if g is None:
            return False
        frozen = (g.relay_code_fingerprint or "").strip()
        if not frozen or fp != frozen:
            _log.warning(
                "apply_relay_code: fingerprint mismatch (got %s, frozen %s); dropping",
                fp, frozen,
            )
            return False
        row = await session.get(GroupRelayCode, group_id)
        if row is None:
            session.add(GroupRelayCode(
                group_id=group_id,
                source=source,
                fingerprint=fp,
                published_by=opened.sender_pubkey,
                published_at=str(payload.get("published_at") or iso_now()),
            ))
        else:
            row.source = source
            row.fingerprint = fp
            row.published_by = opened.sender_pubkey
            row.published_at = str(payload.get("published_at") or iso_now())
        await session.commit()
    await event_bus.publish({"type": "group.relays", "group_id": group_id})
    return True


async def apply_roles_def(opened: OpenedFrame) -> bool:
    """Apply a ``roles.def`` delta — a role definition was created/updated/deleted.

    ``GroupRole`` rows (the role *definitions*, distinct from
    's member-role *assignments*) were otherwise node-local, so a
    role created on the founder's node never appeared in any other
    member's UI. This delta replicates the upsert/delete to every member.

    Authorization: caller has already verified the sender holds
    ``role:assign``. Defenses inside:

    * ``upsert`` of ``founder`` / ``member`` is refused — mirrors the
      endpoint's immutability rule (member is the baseline-read floor;
      stripping its perms would silently revoke ``group:read``).
    * ``delete`` of any default role (founder/admin/member) is refused —
      mirrors the endpoint's ``_PROTECTED_ROLE_NAMES`` guard.
    * ``delete`` cascades into ``GroupMemberRole`` rows for that role, so
      the roster doesn't keep stale assignments to a deleted role.

    Returns True on a well-formed frame, False on malformed.
    """
    payload = json.loads(opened.payload.decode("utf-8"))
    group_id = str(payload.get("group_id") or "")
    if group_id != opened.channel:
        _log.warning("apply_roles_def: group_id mismatch; dropping")
        return False
    op = str(payload.get("op") or "")
    role_name = str(payload.get("role_name") or "").strip()
    if op not in ("upsert", "delete") or not role_name:
        return False

    async with get_session() as session:
        if op == "upsert":
            if role_name in ("founder", "member"):
                return False
            raw_perms = payload.get("permissions") or []
            if not isinstance(raw_perms, list):
                return False
            perms_blob = encode_role_permissions(
                [str(p) for p in raw_perms if isinstance(p, str)]
            )
            now = str(payload.get("updated_at") or iso_now())
            existing = await session.get(GroupRole, (group_id, role_name))
            if existing is None:
                session.add(
                    GroupRole(
                        group_id=group_id,
                        name=role_name,
                        permissions_json=perms_blob,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.permissions_json = perms_blob
                existing.updated_at = now
        else:  # delete
            if role_name in ("founder", "admin", "member"):
                return False
            existing = await session.get(GroupRole, (group_id, role_name))
            if existing is None:
                return False
            await session.execute(
                delete(GroupMemberRole).where(
                    (GroupMemberRole.group_id == group_id)
                    & (GroupMemberRole.role_name == role_name)
                )
            )
            await session.delete(existing)
        await session.commit()
    await event_bus.publish({"type": "group.roster", "group_id": group_id})
    return True


# ---- fanout ------------------------------------------------------------


# Type for the per-member POST function: (peer_address, node_id, path,
# body) -> (status, json). Pulled out so tests can inject a recording
# stub instead of hitting the network.
PosterFn = Callable[[str, str, str, dict], "Awaitable[tuple[int, dict]]"]  # noqa: F821


async def _default_poster(
    peer_address: str, node_id: str, path: str, body: dict,
    *, group_id: str | None = None,
) -> tuple[int, dict]:
    """Deliver a frame to one member.

    try a direct HTTP POST first (LAN / publicly reachable —
    the fast path) with a short timeout so an unreachable peer fails
    over quickly. On failure, fall back to the generic WS relay routed
    by ``node_id`` — the cross-region / behind-NAT path.

    ``group_id`` (when provided) restricts the relay-WS
    fallback to relays bound to that group, guaranteeing the receiver
    is subscribed. ``publish_frame`` always passes it; test posters
    accept it as an ignored kwarg via ``**_kw`` if they don't use it.
    """
    if peer_address:
        for scheme in ("https", "http"):
            try:
                async with httpx.AsyncClient(verify=False, timeout=4.0) as client:
                    res = await client.post(
                        f"{scheme}://{peer_address}{path}", json=body
                    )
                    try:
                        return res.status_code, res.json()
                    except ValueError:
                        return res.status_code, {"error": "non-JSON response"}
            except httpx.HTTPError:
                continue

    if node_id:
        try:
            from nexus.networking.relay_client import relay_http_request

            resp = await relay_http_request(
                node_id, "POST", path, body, group_id=group_id
            )
            return int(resp.get("status", 502)), (resp.get("body") or {})
        except Exception:
            _log.debug("relay_http_request to %s failed", node_id, exc_info=True)

    return 503, {"error": "peer unreachable"}


async def _resolve_perm_holders(
    session: AsyncSession,
    group_id: str,
    perm: str,
    exclude_pubkeys: set[str],
) -> list[tuple[str, str, str]]:
    """Return [(member_pubkey, peer_address, node_id)] for every member
    holding *perm*.

    Any pubkey in ``exclude_pubkeys`` (always includes the local node)
    and members with neither a ``peer_address`` nor a ``node_id`` — no
    way to reach them — are filtered out.
    """
    role_rows = (
        await session.execute(
            select(GroupMemberRole, GroupRole.permissions_json)
            .join(
                GroupRole,
                (GroupRole.group_id == GroupMemberRole.group_id)
                & (GroupRole.name == GroupMemberRole.role_name),
            )
            .where(GroupMemberRole.group_id == group_id)
        )
    ).all()
    holders: set[str] = set()
    for mr, perms_json in role_rows:
        if perm in decode_role_permissions(perms_json):
            holders.add(mr.member_pubkey)
    if not holders:
        return []

    member_rows = (
        await session.execute(
            select(GroupMember).where(
                (GroupMember.group_id == group_id)
                & (GroupMember.pubkey.in_(holders))
            )
        )
    ).scalars().all()
    targets: list[tuple[str, str, str]] = []
    for m in member_rows:
        if m.pubkey in exclude_pubkeys:
            continue
        peer_address = (m.peer_address or "").strip()
        node_id = (m.node_id or "").strip()
        if not peer_address and not node_id:
            continue
        targets.append((m.pubkey, peer_address, node_id))
    return targets


async def _resolve_admin_targets(
    session: AsyncSession, group_id: str, exclude_pubkeys: set[str]
) -> list[tuple[str, str, str]]:
    """``group:approve`` holders — the audience for ``pending.*`` frames."""
    return await _resolve_perm_holders(
        session, group_id, PERM_GROUP_APPROVE, exclude_pubkeys
    )


async def _resolve_relay_host_targets(
    session: AsyncSession, group_id: str, exclude_pubkeys: set[str]
) -> list[tuple[str, str, str]]:
    """``relay:host`` holders — member nodes that fan out group frames."""
    return await _resolve_perm_holders(
        session, group_id, PERM_RELAY_HOST, exclude_pubkeys
    )


async def _resolve_all_member_targets(
    session: AsyncSession, group_id: str, exclude_pubkeys: set[str]
) -> list[tuple[str, str, str]]:
    """Every reachable group member — the audience for whole-roster
    frames (``roster.update``, ``symkey.rotate``, ``relay.update``)."""
    rows = (
        await session.execute(
            select(GroupMember).where(GroupMember.group_id == group_id)
        )
    ).scalars().all()
    targets: list[tuple[str, str, str]] = []
    for m in rows:
        if m.pubkey in exclude_pubkeys:
            continue
        peer_address = (m.peer_address or "").strip()
        node_id = (m.node_id or "").strip()
        if not peer_address and not node_id:
            continue
        targets.append((m.pubkey, peer_address, node_id))
    return targets


async def _resolve_audience(
    session: AsyncSession,
    group_id: str,
    frame_type: str,
    exclude_pubkeys: set[str],
) -> list[tuple[str, str, str]]:
    """Fan-out audience for a frame by type: ``roster.update``,
    ``symkey.rotate``, ``relay.update``, ``roles.assign`` and
    ``roles.def`` reach every member; ``pending.*`` reaches only admins."""
    if frame_type in (
        FRAME_ROSTER_UPDATE, FRAME_SYMKEY_ROTATE, FRAME_RELAY_UPDATE,
        FRAME_ROLES_ASSIGN, FRAME_ROLES_DEF,
        FRAME_CHAT_MESSAGE, FRAME_CHAT_MUTE, FRAME_CHAT_DELETE,
        FRAME_PRESENCE_BEACON, FRAME_USAGE_RECEIPT, FRAME_GROUP_META,
        FRAME_RELAY_CODE,
    ):
        return await _resolve_all_member_targets(
            session, group_id, exclude_pubkeys
        )
    return await _resolve_admin_targets(session, group_id, exclude_pubkeys)


async def _local_symkey(session: AsyncSession, group_id: str) -> Optional[bytes]:
    """Open this node's symkey envelope; return ``None`` if not yet minted."""
    group = await session.get(Group, group_id)
    if group is None or not group.group_symkey_enc:
        return None
    try:
        return ecies_open(bytes(group.group_symkey_enc), get_local_group_privkey())
    except Exception:
        _log.debug("local symkey open failed for %s", group_id, exc_info=True)
        return None


async def _local_grant_blob(session: AsyncSession, group_id: str) -> Optional[bytes]:
    me = get_local_group_pubkey()
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
    if grant is None or not grant.signature:
        return None
    return bytes(grant.signature)


async def _local_founder_grant_blob(
    session: AsyncSession, group_id: str
) -> Optional[bytes]:
    """For the founder, who has no admin-issued grant of their own, fall
    back to fabricating a self-grant on the fly using the founder's
    private key. The founder is in the admin set, so verify_grant
    accepts a grant they sign for themselves."""
    me = get_local_group_pubkey()
    g = await session.get(Group, group_id)
    if g is None or (g.founder_pubkey or "") != me:
        return None
    from datetime import datetime, timedelta, timezone
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=86400)
    ).isoformat()
    import secrets as _secrets
    return sign_grant(
        group_id=group_id,
        member_pubkey=me,
        roles=("founder",),
        admin_privkey=get_local_group_privkey(),
        issued_at=iso_now(),
        expires_at=expires_at,
        nonce=_secrets.token_hex(16),
    )


async def publish_frame(
    *,
    session: AsyncSession,
    group_id: str,
    frame_type: str,
    payload_dict: dict,
    exclude_pubkeys: set[str],
    poster: PosterFn = _default_poster,
) -> dict:
    """Seal a frame and route it into the group channel.

    prefer the group's **relay hosts** — members holding
    ``relay:host`` — POSTing the opaque sealed frame to each one's
    ``/peer/group/publish``; every relay fans it out to the frame's
    audience. If no relay host accepts the frame (none exist, or all
    are unreachable) fall back to direct fan-out to the audience so a
    relay-less group still works.

    Returns a summary dict ``{published, skipped_no_symkey,
    skipped_no_grant, via, relays, delivered}``.
    """
    summary: dict = {
        "published": 0,
        "skipped_no_symkey": False,
        "skipped_no_grant": False,
        "skipped_paused": False,
        "via": "",
        "relays": [],
        "delivered": 0,
    }

    # If this node has paused the group, skip outbound
    # publishing entirely so we look offline to the rest of the group.
    group_row = await session.get(Group, group_id)
    if group_row is not None and getattr(group_row, "paused", 0):
        summary["skipped_paused"] = True
        return summary

    symkey = await _local_symkey(session, group_id)
    if symkey is None:
        summary["skipped_no_symkey"] = True
        return summary

    grant_blob = await _local_grant_blob(session, group_id)
    if grant_blob is None:
        grant_blob = await _local_founder_grant_blob(session, group_id)
    if grant_blob is None:
        summary["skipped_no_grant"] = True
        return summary

    payload_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    frame = seal_frame(
        channel=group_id,
        frame_type=frame_type,
        payload=payload_bytes,
        symkey=symkey,
        sender_grant_blob=grant_blob,
        sender_privkey_hex=get_local_group_privkey(),
    )
    envelope = frame.to_dict()
    summary["published"] = 1

    # Capture every published frame to the local log so members
    # who were offline can catch up via /peer/group/catchup. Captured on
    # the publisher side so the founder/admins always have a complete
    # record even when the audience set is empty (no relay hosts yet).
    await capture_frame_to_log(
        group_id=group_id,
        frame_id=frame.frame_id,
        envelope=envelope,
        frame_type=frame_type,
    )

    # Poke our own UI. apply_* runs on the recipients, but the
    # publisher excludes itself from the broadcast, so without this push
    # the originating node's UI would never see its own state change
    # without a manual refresh.
    ui_event_type = _FRAME_TO_UI_EVENT.get(frame_type)
    if ui_event_type:
        await event_bus.publish(
            {"type": ui_event_type, "group_id": group_id}
        )

    # Preferred path — hand the opaque frame to each relay host.
    relay_hosts = await _resolve_relay_host_targets(
        session, group_id, exclude_pubkeys
    )
    for member_pubkey, addr, node_id in relay_hosts:
        summary["relays"].append({"pubkey": member_pubkey, "address": addr})
        try:
            # Pass group_id so the relay-WS fallback picks
            # a relay both ends are subscribed to. Test posters that
            # don't accept it should declare ``**_kw``.
            status, _resp = await poster(
                addr, node_id, "/peer/group/publish", envelope,
                group_id=group_id,
            )
            if 200 <= status < 300:
                summary["delivered"] += 1
        except TypeError:
            # Back-compat: legacy test poster without group_id kwarg.
            status, _resp = await poster(
                addr, node_id, "/peer/group/publish", envelope
            )
            if 200 <= status < 300:
                summary["delivered"] += 1
        except Exception:
            _log.debug(
                "relay publish to %s (%s) failed",
                member_pubkey[:8], addr, exc_info=True,
            )
    if summary["delivered"] > 0:
        summary["via"] = "relay"
        return summary

    # Fallback — no relay host accepted the frame; fan out directly to
    # The audience ourselves (behavior).
    summary["via"] = "direct-fallback" if relay_hosts else "direct"
    for member_pubkey, addr, node_id in await _resolve_audience(
        session, group_id, frame_type, exclude_pubkeys
    ):
        try:
            status, _resp = await poster(
                addr, node_id, "/peer/group/event", envelope,
                group_id=group_id,
            )
            if 200 <= status < 300:
                summary["delivered"] += 1
        except TypeError:
            status, _resp = await poster(
                addr, node_id, "/peer/group/event", envelope
            )
            if 200 <= status < 300:
                summary["delivered"] += 1
        except Exception:
            _log.debug(
                "direct fan-out to %s (%s) failed",
                member_pubkey[:8], addr, exc_info=True,
            )
    return summary


async def publish_pending_request(
    session: AsyncSession,
    row: GroupPendingJoinRequest,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Publish a new pending request to the group channel."""
    me = get_local_group_pubkey()
    return await publish_frame(
        session=session,
        group_id=row.group_id,
        frame_type=FRAME_PENDING_REQUEST,
        payload_dict=_request_payload(row),
        exclude_pubkeys={me, row.joiner_pubkey},
        poster=poster,
    )


async def publish_pending_decision(
    session: AsyncSession,
    row: GroupPendingJoinRequest,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Publish a request's approval/rejection to the group channel."""
    me = get_local_group_pubkey()
    return await publish_frame(
        session=session,
        group_id=row.group_id,
        frame_type=FRAME_PENDING_DECISION,
        payload_dict=_decision_payload(row),
        exclude_pubkeys={me, row.joiner_pubkey},
        poster=poster,
    )


async def publish_roster_update(
    session: AsyncSession,
    group_id: str,
    member_pubkey: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``roster.update`` add-delta for one member.

    Called right after a join is finalised so every member's roster —
    and crucially the new member's ``node_id`` — converges without a
    manual ``refresh_members`` pull. The delta is about a member the
    publisher just created, so any admin can publish it correctly
    without holding a complete view of the roster.
    """
    member = await session.get(GroupMember, (group_id, member_pubkey))
    if member is None:
        return {"published": 0, "reason": "member not found"}
    # The publisher (founder/admin) won't receive its own
    # roster.update, so insert the "joined the chat" system message here.
    join_name = member.display_name or member_pubkey[:8]
    await _insert_join_system_message(
        session, group_id, member_pubkey,
        join_name,
        member.joined_at or "",
    )
    # Commit the system message BEFORE publishing the live event. Callers
    # commit after us, so without this the founder's browser reloads the
    # chat before the "joined" row exists and sees nothing until a manual
    # re-open.
    await session.commit()
    await event_bus.publish(
        {"type": "group.message", "group_id": group_id, "join_name": join_name}
    )
    payload = {
        "group_id": group_id,
        "action": "add",
        "member": {
            "pubkey": member.pubkey,
            "display_name": member.display_name or "",
            "joined_at": member.joined_at or "",
            "peer_address": member.peer_address or "",
            "node_id": member.node_id or "",
        },
    }
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_ROSTER_UPDATE,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_relay_update(
    session: AsyncSession,
    group_id: str,
    relay_url: str,
    action: str,
    operator_pubkey: str,
    *,
    meta: dict | None = None,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``relay.update`` add/remove/config delta to every member.

    Called right after a relay binding changes (bind/unbind, a tunnel
    URL rotation, or a ``config`` adjustment) so the group's
    ``GroupRelayBinding`` set converges without a manual roster pull.
    """
    payload = {
        "group_id": group_id,
        "action": action,
        "relay_url": relay_url,
        "operator_pubkey": operator_pubkey,
    }
    if meta:
        payload.update(meta)
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_RELAY_UPDATE,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_chat_message(
    session: AsyncSession,
    group_id: str,
    payload: dict,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``chat.message`` to every member."""
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_CHAT_MESSAGE,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_chat_mute(
    session: AsyncSession,
    group_id: str,
    member_pubkey: str,
    muted: bool,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``chat.mute`` delta to every member."""
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_CHAT_MUTE,
        payload_dict={
            "group_id": group_id,
            "member_pubkey": member_pubkey,
            "muted": bool(muted),
        },
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_chat_delete(
    session: AsyncSession,
    group_id: str,
    msg_id: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``chat.delete`` tombstone to every member."""
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_CHAT_DELETE,
        payload_dict={"group_id": group_id, "msg_id": msg_id},
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_presence_beacon(
    session: AsyncSession,
    group_id: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``presence.beacon`` so every member marks us online."""
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_PRESENCE_BEACON,
        payload_dict={"group_id": group_id, "ts": iso_now()},
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_usage_receipt(
    session: AsyncSession,
    group_id: str,
    receipt: dict,
    sig: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a counterparty-signed usage receipt to the group.

    The frame carries ``{receipt, sig}``; every member verifies the inner
    consumer signature on apply, so the broadcaster can't tamper with it.
    """
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_USAGE_RECEIPT,
        payload_dict={"receipt": receipt, "sig": sig},
        exclude_pubkeys=set(),
        poster=poster,
    )


async def publish_roles_assign(
    session: AsyncSession,
    group_id: str,
    member_pubkey: str,
    roles: list[str],
    assigned_at: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``roles.assign`` delta — a member's role set was replaced.

    . Same envelope shape as ``relay.update`` but the audience
    cares about role chips rather than relay bindings. ``apply_*`` on the
    recipient side replaces the target's ``GroupMemberRole`` rows; the
    dispatcher checks the sender holds ``role:assign`` before forwarding.
    """
    payload = {
        "group_id": group_id,
        "member_pubkey": member_pubkey,
        "roles": [str(r) for r in roles],
        "assigned_at": assigned_at,
    }
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_ROLES_ASSIGN,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_roles_def(
    session: AsyncSession,
    group_id: str,
    op: str,
    role_name: str,
    permissions: list[str],
    updated_at: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``roles.def`` delta — role definition was created/updated/deleted.

    . Companion to ``roles.assign`` : assign replicates
    which roles a member holds; this replicates the *definitions*
    themselves (name + permissions list). For ``delete`` the
    ``permissions`` argument is ignored.
    """
    payload = {
        "group_id": group_id,
        "op": op,
        "role_name": role_name,
        "permissions": [str(p) for p in permissions],
        "updated_at": updated_at,
    }
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_ROLES_DEF,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_group_meta(
    session: AsyncSession,
    group_id: str,
    avatar: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``group.meta`` delta (the group avatar)."""
    payload = {
        "group_id": group_id,
        "avatar": avatar,
        "updated_at": iso_now(),
    }
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_GROUP_META,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_relay_code(
    session: AsyncSession,
    group_id: str,
    source: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Broadcast a ``relay.code`` frame carrying the group's canonical relay
    module source.

    The caller (the founder/admin endpoint) has already verified *source*
    matches the group's frozen fingerprint; recipients re-verify in
    :func:`apply_relay_code`."""
    payload = {
        "group_id": group_id,
        "source": source,
        "published_at": iso_now(),
    }
    return await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_RELAY_CODE,
        payload_dict=payload,
        exclude_pubkeys={get_local_group_pubkey()},
        poster=poster,
    )


async def publish_symkey_rotate(
    session: AsyncSession,
    group_id: str,
    kicked_pubkey: str,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Rotate the group symkey and broadcast it after a member kick.

    . Mints a fresh symkey, seals a copy to every *remaining*
    member's X25519 pubkey, and publishes a ``symkey.rotate`` frame
    carrying those per-member envelopes + the ``kicked_pubkey``.

    The frame is sealed with the **current (old)** symkey so every
    remaining member can still open it — :func:`publish_frame` reads
    ``Group.group_symkey_enc``, so this node's own copy must only be
    rotated *after* the frame is sealed. This node updates its own copy
    here directly; peers adopt theirs via :func:`apply_symkey_rotate`.

    Caller (the kick endpoint) must have already deleted the kicked
    member's local rows and must commit the session afterwards.
    """
    me = get_local_group_pubkey()

    # Remaining members reachable for an ECIES envelope: anyone with an
    # advertised X25519 pubkey, excluding this node (rotated directly
    # below) and the kicked member (gets no key — that's the point).
    member_rows = (
        await session.execute(
            select(GroupMember).where(GroupMember.group_id == group_id)
        )
    ).scalars().all()
    new_symkey = mint_group_symkey()
    envelopes: dict[str, str] = {}
    for m in member_rows:
        if m.pubkey in (me, kicked_pubkey):
            continue
        x25519 = (m.member_x25519_pub or "").strip()
        if not x25519:
            continue
        try:
            envelopes[m.pubkey] = base64.b64encode(
                ecies_seal(new_symkey, x25519)
            ).decode("ascii")
        except Exception:
            _log.warning(
                "publish_symkey_rotate: seal to %s failed",
                m.pubkey[:8], exc_info=True,
            )

    payload = {
        "group_id": group_id,
        "kicked_pubkey": kicked_pubkey,
        "envelopes": envelopes,
    }
    # Seal + route the frame while Group.group_symkey_enc still holds
    # the old key (publish_frame opens it to seal).
    summary = await publish_frame(
        session=session,
        group_id=group_id,
        frame_type=FRAME_SYMKEY_ROTATE,
        payload_dict=payload,
        exclude_pubkeys={me, kicked_pubkey},
        poster=poster,
    )

    # Now rotate this node's own copy to the new key.
    group = await session.get(Group, group_id)
    if group is not None:
        group.group_symkey_enc = ecies_seal(
            new_symkey, derive_x25519_pubkey_hex(get_local_group_privkey())
        )
    return summary


# ---- inbound dispatch --------------------------------------------------


async def dispatch_inbound_frame(
    envelope: dict,
) -> dict:
    """Top-level inbound handler called by ``/peer/group/event``.

    Steps:

    1. Parse the envelope shape; reject malformed.
    2. Frame-id dedupe via :data:`_DEDUPE`.
    3. Load this node's symkey for the channel + the admin pubkey set.
    4. ``open_frame`` for crypto/auth verification.
    5. Dispatch to the right handler by frame_type.

    Returns ``{"ok": bool, "applied": bool, "reason": str}``.
    """
    try:
        frame = GroupFrame.from_dict(envelope)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "applied": False, "reason": f"malformed: {exc}"}

    if _DEDUPE.seen(frame.frame_id):
        return {"ok": True, "applied": False, "reason": "duplicate"}

    async with get_session() as session:
        # Drop inbound frames for groups the local user has
        # paused. Symmetric with the publish_frame skip on the outbound
        # side — together they make us look offline to the group.
        g_row = await session.get(Group, frame.channel)
        if g_row is not None and getattr(g_row, "paused", 0):
            return {"ok": True, "applied": False, "reason": "group paused locally"}
        symkey = await _local_symkey(session, frame.channel)
        if symkey is None:
            return {"ok": False, "applied": False, "reason": "no symkey for channel"}
        admin_pubkeys = await _admin_pubkey_set(session, frame.channel)

    if not admin_pubkeys:
        return {"ok": False, "applied": False, "reason": "no admin pubkeys"}

    from nexus.security.group_frame import (
        FrameVerificationError,
        open_frame,
    )

    try:
        opened = open_frame(frame, symkey=symkey, group_admin_pubkeys=admin_pubkeys)
    except FrameVerificationError as exc:
        return {"ok": False, "applied": False, "reason": f"verify: {exc}"}

    if opened.frame_type == FRAME_PENDING_REQUEST:
        applied = await apply_pending_request(opened)
    elif opened.frame_type == FRAME_PENDING_DECISION:
        applied = await apply_pending_decision(opened)
    elif opened.frame_type == FRAME_ROSTER_UPDATE:
        applied = await apply_roster_update(opened)
    elif opened.frame_type == FRAME_SYMKEY_ROTATE:
        if not await _rotate_sender_authorized(opened):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks member:kick (and not a self-leave)",
            }
        applied = await apply_symkey_rotate(opened)
    elif opened.frame_type == FRAME_RELAY_UPDATE:
        if not await _relay_update_sender_authorized(opened):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks relay:host / relay:share_content",
            }
        applied = await apply_relay_update(opened)
    elif opened.frame_type == FRAME_ROLES_ASSIGN:
        if not await _sender_can_assign_roles(
            opened.channel, opened.sender_pubkey
        ):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks role:assign",
            }
        applied = await apply_roles_assign(opened)
    elif opened.frame_type == FRAME_ROLES_DEF:
        if not await _sender_can_assign_roles(
            opened.channel, opened.sender_pubkey
        ):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks role:assign",
            }
        applied = await apply_roles_def(opened)
    elif opened.frame_type == FRAME_CHAT_MESSAGE:
        applied = await apply_chat_message(opened)
    elif opened.frame_type == FRAME_CHAT_MUTE:
        if not await _sender_can_mute(opened.channel, opened.sender_pubkey):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks member:mute",
            }
        applied = await apply_chat_mute(opened)
    elif opened.frame_type == FRAME_CHAT_DELETE:
        applied = await apply_chat_delete(opened)
    elif opened.frame_type == FRAME_PRESENCE_BEACON:
        applied = await apply_presence_beacon(opened)
    elif opened.frame_type == FRAME_USAGE_RECEIPT:
        applied = await apply_usage_receipt(opened)
    elif opened.frame_type == FRAME_GROUP_META:
        if not await _sender_can_assign_roles(
            opened.channel, opened.sender_pubkey
        ):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks role:assign",
            }
        applied = await apply_group_meta(opened)
    elif opened.frame_type == FRAME_RELAY_CODE:
        if not await _sender_can_assign_roles(
            opened.channel, opened.sender_pubkey
        ):
            return {
                "ok": True,
                "applied": False,
                "reason": "sender lacks role:assign",
            }
        applied = await apply_relay_code(opened)
    else:
        return {
            "ok": True,
            "applied": False,
            "reason": f"unknown frame_type: {opened.frame_type}",
        }
    # Archive a copy on this node too so we can replay it for
    # others who were offline (any node can serve catch-up, not just
    # the original publisher). Capture happens regardless of `applied`
    # so we record frames we recognized but no-op'd (e.g., dedupe).
    await capture_frame_to_log(
        group_id=frame.channel,
        frame_id=frame.frame_id,
        envelope=envelope,
        frame_type=opened.frame_type,
    )
    return {"ok": True, "applied": applied, "reason": ""}


async def _admin_pubkey_set(session: AsyncSession, group_id: str) -> list[str]:
    """Founder + admin pubkeys used as the verify_grant admin set.

    Matches the existing :func:`nexus.api.group_peer._admin_pubkeys`
    semantics (any pubkey holding founder or admin role).
    """
    rows = (
        await session.execute(
            select(GroupMemberRole.member_pubkey).where(
                (GroupMemberRole.group_id == group_id)
                & (GroupMemberRole.role_name.in_(("founder", "admin")))
            )
        )
    ).fetchall()
    return sorted({row[0] for row in rows})


async def _sender_can_kick(group_id: str, sender_pubkey: str) -> bool:
    """True if ``sender_pubkey`` holds ``member:kick`` in *group_id*.

    Gate for ``symkey.rotate`` frames: ``open_frame`` already proves the
    sender is a verified member, but only a ``member:kick`` holder may
    rotate the group key — a plain member must not be able to (or be
    relayed into) desyncing everyone's symkey.
    """
    async with get_session() as session:
        return await has_permission(
            session, group_id, sender_pubkey, PERM_MEMBER_KICK
        )


async def _rotate_sender_authorized(opened: OpenedFrame) -> bool:
    """Authorize a ``symkey.rotate`` frame.

    A kick (sender != kicked) needs ``member:kick``. A **voluntary leave**
    (sender == kicked, the leaver removing themselves) is always allowed —
    otherwise remaining members reject the frame and never drop the leaver
    from their roster.
    """
    try:
        kicked = str(
            json.loads(opened.payload.decode("utf-8")).get("kicked_pubkey") or ""
        )
    except Exception:
        kicked = ""
    if kicked and kicked == opened.sender_pubkey:
        return True
    return await _sender_can_kick(opened.channel, opened.sender_pubkey)


async def _sender_can_host_relay(group_id: str, sender_pubkey: str) -> bool:
    """True if ``sender_pubkey`` holds ``relay:host`` in *group_id*.

    Gate for ``relay.update`` frames — only a relay operator may change
    the group's relay bindings (same gate as the ``POST .../relays``
    endpoint), so a plain member can't rewrite or be relayed into
    rewriting the relay set.
    """
    async with get_session() as session:
        return await has_permission(
            session, group_id, sender_pubkey, PERM_RELAY_HOST
        )


async def _relay_update_sender_authorized(opened: OpenedFrame) -> bool:
    """Gate a ``relay.update`` frame by its action.

    ``content_share`` / ``content_revoke`` authorize a relay to read
    group content and so require ``relay:share_content`` (founder/admin) — a
    relay operator must NOT self-authorize reading everyone's messages.
    Every other action (add/remove/config) keeps the ``relay:host`` gate.
    """
    try:
        action = str(
            json.loads(opened.payload.decode("utf-8")).get("action") or ""
        )
    except (ValueError, TypeError):
        return False
    if action in ("content_share", "content_revoke"):
        async with get_session() as session:
            return await has_permission(
                session, opened.channel, opened.sender_pubkey,
                PERM_RELAY_SHARE_CONTENT,
            )
    return await _sender_can_host_relay(opened.channel, opened.sender_pubkey)


async def _sender_can_assign_roles(
    group_id: str, sender_pubkey: str
) -> bool:
    """Gate for ``roles.assign`` frames — only holders of ``role:assign``
    can replace a member's role set."""
    async with get_session() as session:
        return await has_permission(
            session, group_id, sender_pubkey, PERM_ROLE_ASSIGN
        )


async def _sender_can_mute(group_id: str, sender_pubkey: str) -> bool:
    """Gate for ``chat.mute`` frames — only holders of ``member:mute``."""
    async with get_session() as session:
        return await has_permission(
            session, group_id, sender_pubkey, PERM_MEMBER_MUTE
        )


# ---- relay-host fan-out --------------------------------------


async def relay_inbound_frame(
    envelope: dict,
    *,
    poster: PosterFn = _default_poster,
) -> dict:
    """Relay-host handler for ``/peer/group/publish``.

    The endpoint has already checked this node holds ``relay:host``
    for the channel. Here we verify the opaque frame, apply it locally
    if this node is itself in the frame's audience, and fan it out to
    the rest of the audience via ``/peer/group/event``.

    Returns ``{"ok": bool, "applied": bool, "relayed": int,
    "reason": str}``.
    """
    try:
        frame = GroupFrame.from_dict(envelope)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "applied": False, "relayed": 0,
                "reason": f"malformed: {exc}"}

    # Dedupe up front: a frame already seen (via another relay binding,
    # or applied via /peer/group/event) must not be re-fanned-out.
    if _DEDUPE.seen(frame.frame_id):
        return {"ok": True, "applied": False, "relayed": 0,
                "reason": "duplicate"}

    async with get_session() as session:
        # Refuse to relay or apply frames for a group we've
        # paused locally — both directions silenced.
        g_row = await session.get(Group, frame.channel)
        if g_row is not None and getattr(g_row, "paused", 0):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "group paused locally"}
        symkey = await _local_symkey(session, frame.channel)
        if symkey is None:
            return {"ok": False, "applied": False, "relayed": 0,
                    "reason": "no symkey for channel"}
        admin_pubkeys = await _admin_pubkey_set(session, frame.channel)
    if not admin_pubkeys:
        return {"ok": False, "applied": False, "relayed": 0,
                "reason": "no admin pubkeys"}

    from nexus.security.group_frame import (
        FrameVerificationError,
        open_frame,
    )

    try:
        opened = open_frame(
            frame, symkey=symkey, group_admin_pubkeys=admin_pubkeys
        )
    except FrameVerificationError as exc:
        # Never amplify a frame we can't verify.
        return {"ok": False, "applied": False, "relayed": 0,
                "reason": f"verify: {exc}"}

    # Refuse to relay a type whose audience we don't know.
    if opened.frame_type not in (
        FRAME_PENDING_REQUEST, FRAME_PENDING_DECISION,
        FRAME_ROSTER_UPDATE, FRAME_SYMKEY_ROTATE, FRAME_RELAY_UPDATE,
        FRAME_ROLES_ASSIGN, FRAME_ROLES_DEF,
        FRAME_CHAT_MESSAGE, FRAME_CHAT_MUTE, FRAME_CHAT_DELETE,
        FRAME_PRESENCE_BEACON, FRAME_USAGE_RECEIPT, FRAME_GROUP_META,
        FRAME_RELAY_CODE,
    ):
        return {"ok": True, "applied": False, "relayed": 0,
                "reason": f"unknown frame_type: {opened.frame_type}"}

    # Never amplify a privileged frame from an unauthorized sender —
    # relaying it would let a plain member desync the group.
    if opened.frame_type == FRAME_SYMKEY_ROTATE:
        if not await _rotate_sender_authorized(opened):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks member:kick (and not a self-leave)"}
    if opened.frame_type == FRAME_RELAY_UPDATE:
        if not await _relay_update_sender_authorized(opened):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks relay:host / relay:share_content"}
    if opened.frame_type == FRAME_ROLES_ASSIGN:
        if not await _sender_can_assign_roles(
            frame.channel, opened.sender_pubkey
        ):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks role:assign"}
    if opened.frame_type == FRAME_ROLES_DEF:
        if not await _sender_can_assign_roles(
            frame.channel, opened.sender_pubkey
        ):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks role:assign"}
    if opened.frame_type == FRAME_CHAT_MUTE:
        if not await _sender_can_mute(frame.channel, opened.sender_pubkey):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks member:mute"}
    if opened.frame_type == FRAME_GROUP_META:
        if not await _sender_can_assign_roles(
            frame.channel, opened.sender_pubkey
        ):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks role:assign"}
    if opened.frame_type == FRAME_RELAY_CODE:
        if not await _sender_can_assign_roles(
            frame.channel, opened.sender_pubkey
        ):
            return {"ok": True, "applied": False, "relayed": 0,
                    "reason": "sender lacks role:assign"}

    me = get_local_group_pubkey()
    async with get_session() as session:
        targets = await _resolve_audience(
            session, frame.channel, opened.frame_type,
            {opened.sender_pubkey, me},
        )
        # roster.update + symkey.rotate + relay.update + roles.assign
        # + roles.def reach every member; pending.* only admins.
        if opened.frame_type in (
            FRAME_ROSTER_UPDATE, FRAME_SYMKEY_ROTATE, FRAME_RELAY_UPDATE,
            FRAME_ROLES_ASSIGN, FRAME_ROLES_DEF,
            FRAME_CHAT_MESSAGE, FRAME_CHAT_MUTE, FRAME_CHAT_DELETE,
            FRAME_PRESENCE_BEACON, FRAME_USAGE_RECEIPT, FRAME_GROUP_META,
            FRAME_RELAY_CODE,
        ):
            i_am_audience = True
        else:
            i_am_audience = await has_permission(
                session, frame.channel, me, PERM_GROUP_APPROVE
            )

    # Apply locally if this node is itself in the frame's audience.
    applied = False
    if i_am_audience:
        if opened.frame_type == FRAME_PENDING_REQUEST:
            applied = await apply_pending_request(opened)
        elif opened.frame_type == FRAME_PENDING_DECISION:
            applied = await apply_pending_decision(opened)
        elif opened.frame_type == FRAME_ROSTER_UPDATE:
            applied = await apply_roster_update(opened)
        elif opened.frame_type == FRAME_SYMKEY_ROTATE:
            applied = await apply_symkey_rotate(opened)
        elif opened.frame_type == FRAME_RELAY_UPDATE:
            applied = await apply_relay_update(opened)
        elif opened.frame_type == FRAME_ROLES_ASSIGN:
            applied = await apply_roles_assign(opened)
        elif opened.frame_type == FRAME_ROLES_DEF:
            applied = await apply_roles_def(opened)
        elif opened.frame_type == FRAME_CHAT_MESSAGE:
            applied = await apply_chat_message(opened)
        elif opened.frame_type == FRAME_CHAT_MUTE:
            applied = await apply_chat_mute(opened)
        elif opened.frame_type == FRAME_PRESENCE_BEACON:
            applied = await apply_presence_beacon(opened)
        elif opened.frame_type == FRAME_USAGE_RECEIPT:
            applied = await apply_usage_receipt(opened)
        elif opened.frame_type == FRAME_GROUP_META:
            applied = await apply_group_meta(opened)
        elif opened.frame_type == FRAME_RELAY_CODE:
            applied = await apply_relay_code(opened)
        else:  # FRAME_CHAT_DELETE
            applied = await apply_chat_delete(opened)

    # Fan the opaque frame out to the rest of the audience.
    relayed = 0
    for member_pubkey, addr, node_id in targets:
        try:
            status, _resp = await poster(
                addr, node_id, "/peer/group/event", envelope
            )
            if 200 <= status < 300:
                relayed += 1
        except Exception:
            _log.debug(
                "relay fan-out to %s (%s) failed",
                member_pubkey[:8], addr, exc_info=True,
            )
    # Capture into local log so this relay-host can also serve
    # /peer/group/catchup. Mirrors dispatch_inbound_frame's capture.
    await capture_frame_to_log(
        group_id=frame.channel,
        frame_id=frame.frame_id,
        envelope=envelope,
        frame_type=opened.frame_type,
    )
    return {"ok": True, "applied": applied, "relayed": relayed, "reason": ""}


__all__ = [
    "FRAME_PENDING_REQUEST",
    "FRAME_PENDING_DECISION",
    "FRAME_ROSTER_UPDATE",
    "FRAME_SYMKEY_ROTATE",
    "FRAME_RELAY_UPDATE",
    "FRAME_ROLES_ASSIGN",
    "FRAME_ROLES_DEF",
    "FRAME_CHAT_MESSAGE",
    "FRAME_CHAT_MUTE",
    "FRAME_CHAT_DELETE",
    "FRAME_PRESENCE_BEACON",
    "FRAME_USAGE_RECEIPT",
    "FRAME_GROUP_META",
    "FRAME_RELAY_CODE",
    "apply_pending_request",
    "apply_pending_decision",
    "apply_roster_update",
    "apply_symkey_rotate",
    "apply_relay_update",
    "apply_roles_assign",
    "apply_roles_def",
    "apply_chat_message",
    "apply_chat_mute",
    "apply_chat_delete",
    "apply_presence_beacon",
    "apply_usage_receipt",
    "apply_group_meta",
    "apply_relay_code",
    "publish_frame",
    "publish_pending_request",
    "publish_pending_decision",
    "publish_roster_update",
    "publish_symkey_rotate",
    "publish_relay_update",
    "publish_roles_assign",
    "publish_roles_def",
    "publish_chat_message",
    "publish_chat_mute",
    "publish_chat_delete",
    "publish_presence_beacon",
    "publish_usage_receipt",
    "publish_group_meta",
    "publish_relay_code",
    "dispatch_inbound_frame",
    "relay_inbound_frame",
    "capture_frame_to_log",
    "fetch_log_since",
    "prune_frame_log",
]
