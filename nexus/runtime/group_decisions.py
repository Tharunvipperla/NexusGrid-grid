"""Admin-side delivery of join decisions to joiners.

When an admin approves or rejects a private-group join request, the
decision must reach the joiner so:

* On ``approved`` they can materialize the group/membership/grant
  locally and start using the group.
* On ``rejected`` they see the reason instead of being left hanging.

Delivery is best-effort over HTTPS-then-HTTP (no cert pinning yet —
the grant signature itself is the trust mechanism). If the joiner is
offline at decision time, a periodic scheduler tick retries until
either the call succeeds (``delivered_at`` is stamped) or the row
ages past the 30-minute window. An in-memory attempt counter caps
the per-row retry budget at 5 — restarting the admin process resets
the counter but the row's age still bounds total retry duration.

For tests, :func:`attempt_deliver_one` is the single delivery
primitive; the scheduler loop just calls it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.storage import get_session
from nexus.storage.models import GroupPendingJoinRequest


_log = logging.getLogger("nexus.runtime.group_decisions")


GROUP_DECISION_RETRY_WINDOW_S = 30 * 60  # 30 min
GROUP_DECISION_MAX_ATTEMPTS = 5
GROUP_DECISION_LOOP_INTERVAL_S = 360  # 6 min → 5 attempts inside 30 min

# Module-level in-memory attempt tracker. Keyed by request_id. Resets
# on process restart, which is fine — the 30 min wall-clock cap still
# applies via ``created_at``.
_attempts: dict[str, int] = defaultdict(int)


async def _post_to_joiner(
    joiner_address: str,
    joiner_node_id: str,
    body: dict,
    *,
    group_id: str | None = None,
) -> tuple[int, dict]:
    """Deliver a join decision — HTTPS, then HTTP, then the relay.

    a connection failure on the direct path falls through to
    the WS relay (keyed on the joiner's node UUID) so a NAT'd joiner
    still receives the decision.

    ``group_id`` restricts relay candidates to that group's
    bindings (joiner is subscribed once the admin's approval lands).
    """
    if joiner_address:
        for scheme in ("https", "http"):
            try:
                async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                    res = await client.post(
                        f"{scheme}://{joiner_address}/peer/group/join_decision",
                        json=body,
                    )
                    try:
                        return res.status_code, res.json()
                    except ValueError:
                        return res.status_code, {"error": "non-JSON response"}
            except httpx.HTTPError:
                continue
    if joiner_node_id:
        try:
            from nexus.networking.relay_client import relay_http_request

            resp = await relay_http_request(
                joiner_node_id, "POST", "/peer/group/join_decision", body,
                group_id=group_id,
            )
            return int(resp.get("status", 502)), (resp.get("body") or {})
        except Exception:
            _log.debug(
                "relay decision delivery to %s failed",
                joiner_node_id, exc_info=True,
            )
    return 503, {"error": "joiner unreachable"}


def _build_decision_body(
    row: GroupPendingJoinRequest,
    *,
    group_name: str,
    founder_pubkey: str,
    grant_blob_b64: Optional[str] = None,
    default_role: Optional[str] = None,
    issued_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    privacy_mode: str = "open",
    founder_display_name: str = "",
    founder_address: str = "",
    symkey_envelope_b64: str = "",
) -> dict:
    from nexus.core.identity import get_or_create_node_uuid

    body: dict = {
        "request_id": row.id,
        "group_id": row.group_id,
        "group_name": group_name,
        "founder_pubkey": founder_pubkey,
        "decision": row.status,
        "reason": row.decision_reason or "",
        "privacy_mode": privacy_mode or "open",
        "founder_display_name": founder_display_name or "",
        "founder_address": founder_address or "",
        # The deciding node's UUID so the joiner can DM us back.
        "founder_node_id": get_or_create_node_uuid() or "",
    }
    if row.status == "approved" and grant_blob_b64:
        body["grant_blob_b64"] = grant_blob_b64
        body["default_role"] = default_role or "member"
        body["issued_at"] = issued_at or ""
        body["expires_at"] = expires_at or ""
        body["symkey_envelope_b64"] = symkey_envelope_b64 or ""
    return body


async def attempt_deliver_one(
    session: AsyncSession,
    row: GroupPendingJoinRequest,
    *,
    group_name: str,
    founder_pubkey: str,
    grant_blob_b64: Optional[str] = None,
    default_role: Optional[str] = None,
    issued_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    now_iso: Optional[str] = None,
    privacy_mode: str = "open",
    founder_display_name: str = "",
    founder_address: str = "",
    symkey_envelope_b64: str = "",
) -> bool:
    """Try once to deliver the row's decision; stamp ``delivered_at`` on success.

    Returns ``True`` on success, ``False`` otherwise. Increments the
    in-memory attempt counter on failure so the scheduler loop can
    enforce the 5-attempt cap.

    Callers are expected to commit the session after this returns.
    """
    if not row.joiner_address and not (row.joiner_node_id or ""):
        # No address and no node_id means the joiner never told us where
        # to call — delivery isn't possible. Don't increment the counter
        # so the row simply stays undelivered until the joiner polls.
        return False

    body = _build_decision_body(
        row,
        group_name=group_name,
        founder_pubkey=founder_pubkey,
        grant_blob_b64=grant_blob_b64,
        default_role=default_role,
        issued_at=issued_at,
        expires_at=expires_at,
        privacy_mode=privacy_mode,
        founder_display_name=founder_display_name,
        founder_address=founder_address,
        symkey_envelope_b64=symkey_envelope_b64,
    )
    status, _resp = await _post_to_joiner(
        row.joiner_address or "", row.joiner_node_id or "", body,
        group_id=row.group_id,
    )
    if 200 <= status < 300:
        row.delivered_at = now_iso or _iso_now()
        await session.flush()
        _attempts.pop(row.id, None)
        return True
    _attempts[row.id] += 1
    return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_still_in_window(created_at_iso: str, now: datetime) -> bool:
    if not created_at_iso:
        return False
    try:
        created = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created) <= timedelta(seconds=GROUP_DECISION_RETRY_WINDOW_S)


async def sweep_pending_decisions(
    session: AsyncSession,
    *,
    now: Optional[datetime] = None,
    lookup_group_meta=None,
    lookup_grant_meta=None,
) -> int:
    """Try to deliver every still-eligible undelivered decision.

    Eligible = ``status != pending`` AND ``delivered_at == ''`` AND
    in-memory attempts ``< 5`` AND ``created_at`` within the 30-minute
    window.

    ``lookup_group_meta(group_id) -> (group_name, founder_pubkey)``
    and ``lookup_grant_meta(row) -> (b64, role, issued_at, expires_at)``
    are injectable so tests don't need the full group/grant fixture
    chain. Production wires them to live DB lookups via the helpers
    in :mod:`nexus.api.group_peer` and :mod:`nexus.storage.models`.

    Returns the number of rows successfully delivered this tick.
    """
    now = now or datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(GroupPendingJoinRequest).where(
                (GroupPendingJoinRequest.status != "pending")
                & (GroupPendingJoinRequest.delivered_at == "")
            )
        )
    ).scalars().all()

    delivered = 0
    for row in rows:
        if _attempts.get(row.id, 0) >= GROUP_DECISION_MAX_ATTEMPTS:
            continue
        if not _is_still_in_window(row.created_at or "", now):
            continue
        meta = lookup_group_meta(row.group_id) if lookup_group_meta else ("", "")
        group_name, founder_pubkey = meta
        grant_kw: dict = {}
        if row.status == "approved" and lookup_grant_meta is not None:
            blob_b64, role, issued_at, expires_at = lookup_grant_meta(row)
            grant_kw = dict(
                grant_blob_b64=blob_b64,
                default_role=role,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        ok = await attempt_deliver_one(
            session,
            row,
            group_name=group_name,
            founder_pubkey=founder_pubkey,
            now_iso=now.isoformat(),
            **grant_kw,
        )
        if ok:
            delivered += 1
    if delivered:
        await session.commit()
    else:
        await session.rollback()
    return delivered


# ---- live group/grant lookups used by the production scheduler ---------


async def _live_group_meta(
    session: AsyncSession, group_id: str
) -> tuple[str, str, str, str]:
    """Read group.name + founder_pubkey + privacy_mode + founder display_name."""
    from nexus.storage.models import Group, GroupMember
    g = await session.get(Group, group_id)
    if g is None:
        return ("", "", "open", "")
    founder_member = (
        await session.execute(
            select(GroupMember).where(
                (GroupMember.group_id == group_id)
                & (GroupMember.pubkey == (g.founder_pubkey or ""))
            )
        )
    ).scalar_one_or_none()
    founder_name = (founder_member.display_name if founder_member else "") or ""
    return (g.name or "", g.founder_pubkey or "", g.privacy_mode or "open", founder_name)


async def _live_grant_meta(
    session: AsyncSession, row: GroupPendingJoinRequest
) -> tuple[str, str, str, str]:
    """Read the most recent grant for this joiner in this group."""
    from nexus.storage.models import GroupGrant
    grant = (
        await session.execute(
            select(GroupGrant)
            .where(
                (GroupGrant.group_id == row.group_id)
                & (GroupGrant.member_pubkey == row.joiner_pubkey)
            )
            .order_by(GroupGrant.issued_at.desc())
        )
    ).scalars().first()
    if grant is None:
        return ("", "member", "", "")
    blob_b64 = base64.b64encode(grant.signature or b"").decode("ascii")
    role = "member"
    try:
        import json
        parsed = json.loads(grant.roles_json or "[]")
        if isinstance(parsed, list) and parsed:
            role = str(parsed[0])
    except (ValueError, TypeError):
        pass
    return (blob_b64, role, grant.issued_at or "", grant.expires_at or "")


async def _live_symkey_envelope(
    session: AsyncSession, row: GroupPendingJoinRequest
) -> str:
    """Re-seal a fresh symkey envelope for the joiner on a retry delivery.

    Returns the base64-encoded envelope, or empty string if the
    joiner didn't advertise an X25519 pubkey or this node hasn't
    minted a symkey yet.
    """
    from nexus.storage.models import Group, GroupMember
    from nexus.security.group_ecies import ecies_open, ecies_seal
    from nexus.security.group_keys import get_local_group_privkey

    if not row.joiner_x25519_pub:
        return ""
    group = await session.get(Group, row.group_id)
    if group is None or not group.group_symkey_enc:
        return ""
    try:
        symkey = ecies_open(bytes(group.group_symkey_enc), get_local_group_privkey())
        envelope = ecies_seal(symkey, row.joiner_x25519_pub)
    except Exception:
        return ""
    return base64.b64encode(envelope).decode("ascii")


async def group_decision_delivery_loop(
    poll_seconds: float = float(GROUP_DECISION_LOOP_INTERVAL_S),
) -> None:
    """Forever: every ``poll_seconds`` sweep undelivered decisions."""
    while True:
        try:
            async with get_session() as session:
                # Bind live-DB lookups by closing over the session.
                async def _meta(gid):
                    return await _live_group_meta(session, gid)
                async def _grant(row):
                    return await _live_grant_meta(session, row)
                # The synchronous-signature lookups in sweep_pending_decisions
                # are awaited by the helper itself in production by reading
                # the data eagerly here.
                # Simplest: pre-fetch nothing, do the lookups inline below.
                rows = (
                    await session.execute(
                        select(GroupPendingJoinRequest).where(
                            (GroupPendingJoinRequest.status != "pending")
                            & (GroupPendingJoinRequest.delivered_at == "")
                        )
                    )
                ).scalars().all()
                now = datetime.now(timezone.utc)
                delivered = 0
                for row in rows:
                    if _attempts.get(row.id, 0) >= GROUP_DECISION_MAX_ATTEMPTS:
                        continue
                    if not _is_still_in_window(row.created_at or "", now):
                        continue
                    group_name, founder_pubkey, privacy_mode, founder_name = await _meta(row.group_id)
                    from nexus.core.identity import get_node_identity
                    founder_address = get_node_identity()
                    grant_kw: dict = {}
                    if row.status == "approved":
                        blob_b64, role, issued_at, expires_at = await _grant(row)
                        grant_kw = dict(
                            grant_blob_b64=blob_b64,
                            default_role=role,
                            issued_at=issued_at,
                            expires_at=expires_at,
                            symkey_envelope_b64=await _live_symkey_envelope(session, row),
                        )
                    ok = await attempt_deliver_one(
                        session,
                        row,
                        group_name=group_name,
                        founder_pubkey=founder_pubkey,
                        privacy_mode=privacy_mode,
                        founder_display_name=founder_name,
                        founder_address=founder_address,
                        now_iso=now.isoformat(),
                        **grant_kw,
                    )
                    if ok:
                        delivered += 1
                if delivered:
                    await session.commit()
                    _log.debug("group_decision_delivery: delivered=%d", delivered)
                else:
                    await session.rollback()
        except Exception:
            _log.debug("group_decision_delivery tick failed", exc_info=True)
        await asyncio.sleep(poll_seconds)


__all__ = [
    "GROUP_DECISION_RETRY_WINDOW_S",
    "GROUP_DECISION_MAX_ATTEMPTS",
    "GROUP_DECISION_LOOP_INTERVAL_S",
    "attempt_deliver_one",
    "sweep_pending_decisions",
    "group_decision_delivery_loop",
]
