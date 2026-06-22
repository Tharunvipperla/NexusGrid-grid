"""Group invite-link logic.

An invite is the entrypoint into a group. For it is bearer
auth — an unguessable random token stored in the issuer's
``group_invite_links`` row. Presenting the token back to the issuing
admin is sufficient proof; no signature is needed because the token
itself is the secret. will add replicated invite state across
group members; for now the issuer's local DB is authoritative.

Capacity model:

* ``slot_cap`` — advertised capacity. ``0`` means "no cap".
* ``slots_filled`` — incremented **only** on admitted joins via
  :func:`consume_invite`. Pending validation requests do not count,
  so a flood of unsubmitted-but-validated checks cannot starve real
  joiners.
* ``active`` — flips to ``False`` automatically when
  ``slots_filled == slot_cap`` (and ``slot_cap > 0``). The admin can
  flip it back to ``True`` and/or raise the cap with
  :func:`reopen_invite`.
* ``rotated_at`` — when set, the invite row is dead. A rotated row
  never validates, even if ``active`` is still ``1``. Use
  :func:`rotate_invite` to kill a leaked token.

This module is **state machinery**, not crypto — token randomness is
provided by :mod:`secrets`, but there is no signature on the token
itself. Admin authorization to mint / rotate / reopen is enforced at
the API layer (15.4) via the role permission set.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.storage.models import GroupInviteLink
from nexus.utils.time import iso_now


# 32 raw bytes -> ~43 URL-safe chars. Plenty of entropy; short enough to
# paste into chat messages or other share surfaces.
TOKEN_BYTES = 32


# Reason codes returned by validate / consume. Stable strings so tests
# and the UI can branch on them.
REASON_OK = "ok"
REASON_NOT_FOUND = "not_found"
REASON_ROTATED = "rotated"
REASON_INACTIVE = "inactive"
REASON_CAP_REACHED = "cap_reached"
REASON_WRONG_GROUP = "wrong_group"


@dataclass(frozen=True)
class InviteLink:
    """In-memory view of a ``group_invite_links`` row."""

    token: str
    group_id: str
    slot_cap: int
    slots_filled: int
    active: bool
    created_by_pubkey: str
    created_at: str
    rotated_at: str  # empty string means not rotated


@dataclass(frozen=True)
class InviteValidation:
    """Result of :func:`validate_invite`."""

    ok: bool
    reason: str
    invite: Optional[InviteLink] = None


@dataclass(frozen=True)
class InviteConsumeResult:
    """Result of :func:`consume_invite`."""

    ok: bool
    reason: str
    invite: Optional[InviteLink] = None
    auto_deactivated: bool = False


def _row_to_invite(row: GroupInviteLink) -> InviteLink:
    return InviteLink(
        token=row.token,
        group_id=row.group_id,
        slot_cap=int(row.slot_cap or 0),
        slots_filled=int(row.slots_filled or 0),
        active=bool(row.active),
        created_by_pubkey=row.created_by_pubkey or "",
        created_at=row.created_at or "",
        rotated_at=row.rotated_at or "",
    )


def _generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


# ---- mint ---------------------------------------------------------------


async def mint_invite(
    *,
    session: AsyncSession,
    group_id: str,
    slot_cap: int,
    created_by_pubkey: str,
) -> InviteLink:
    """Insert a fresh invite row and return its in-memory view.

    ``slot_cap == 0`` means uncapped. Token randomness comes from
    :mod:`secrets`; collisions across the lifetime of the project are
    not realistic.
    """
    if slot_cap < 0:
        raise ValueError("slot_cap must be >= 0")
    token = _generate_token()
    row = GroupInviteLink(
        token=token,
        group_id=group_id,
        slot_cap=int(slot_cap),
        slots_filled=0,
        active=1,
        created_by_pubkey=created_by_pubkey,
        created_at=iso_now(),
        rotated_at="",
    )
    session.add(row)
    await session.flush()
    return _row_to_invite(row)


# ---- validate -----------------------------------------------------------


async def validate_invite(
    *,
    session: AsyncSession,
    token: str,
    group_id: Optional[str] = None,
) -> InviteValidation:
    """Read-only check. Does **not** increment ``slots_filled``.

    Optional ``group_id`` pins which group the caller expected (useful
    when the wire envelope already names the group; rejects a
    cross-group token mix-up).
    """
    row = await session.get(GroupInviteLink, token)
    if row is None:
        return InviteValidation(False, REASON_NOT_FOUND)
    if group_id is not None and row.group_id != group_id:
        return InviteValidation(False, REASON_WRONG_GROUP, _row_to_invite(row))
    if row.rotated_at:
        return InviteValidation(False, REASON_ROTATED, _row_to_invite(row))
    if not bool(row.active):
        return InviteValidation(False, REASON_INACTIVE, _row_to_invite(row))
    cap = int(row.slot_cap or 0)
    filled = int(row.slots_filled or 0)
    if cap > 0 and filled >= cap:
        return InviteValidation(False, REASON_CAP_REACHED, _row_to_invite(row))
    return InviteValidation(True, REASON_OK, _row_to_invite(row))


# ---- consume ------------------------------------------------------------


async def consume_invite(
    *,
    session: AsyncSession,
    token: str,
    group_id: Optional[str] = None,
) -> InviteConsumeResult:
    """Atomically reserve one slot.

    Validates first (same checks as :func:`validate_invite`). On
    success, increments ``slots_filled`` and — if the cap is now
    reached — flips ``active`` to ``0``. The caller commits the
    surrounding transaction; this function only flushes.
    """
    pre = await validate_invite(session=session, token=token, group_id=group_id)
    if not pre.ok:
        return InviteConsumeResult(False, pre.reason, pre.invite)

    row = await session.get(GroupInviteLink, token)
    # Re-fetch defensively; the validate path returned a snapshot, but
    # the row object may have been detached. ``session.get`` re-reads.
    assert row is not None  # invariant: validate just confirmed presence
    new_filled = int(row.slots_filled or 0) + 1
    cap = int(row.slot_cap or 0)
    auto_off = cap > 0 and new_filled >= cap
    row.slots_filled = new_filled
    if auto_off:
        row.active = 0
    await session.flush()
    return InviteConsumeResult(
        True,
        REASON_OK,
        _row_to_invite(row),
        auto_deactivated=auto_off,
    )


# ---- rotate -------------------------------------------------------------


async def rotate_invite(
    *,
    session: AsyncSession,
    token: str,
    group_id: str,
    created_by_pubkey: str,
) -> Optional[InviteLink]:
    """Kill ``token`` and mint a replacement with the same cap.

    The old row's ``rotated_at`` is set to ``iso_now()``. A new row is
    inserted carrying the same ``slot_cap`` but a fresh
    ``slots_filled = 0`` (the replacement is its own bucket; the old
    fills are preserved on the dead row for audit). Returns the new
    invite, or ``None`` if the old token did not exist.
    """
    old = await session.get(GroupInviteLink, token)
    if old is None or old.group_id != group_id:
        return None
    if not old.rotated_at:
        old.rotated_at = iso_now()
    new_token = _generate_token()
    new = GroupInviteLink(
        token=new_token,
        group_id=group_id,
        slot_cap=int(old.slot_cap or 0),
        slots_filled=0,
        active=1,
        created_by_pubkey=created_by_pubkey,
        created_at=iso_now(),
        rotated_at="",
    )
    session.add(new)
    await session.flush()
    return _row_to_invite(new)


# ---- reopen / cap edit --------------------------------------------------


async def reopen_invite(
    *,
    session: AsyncSession,
    token: str,
    group_id: str,
    new_slot_cap: Optional[int] = None,
) -> Optional[InviteLink]:
    """Flip ``active`` back to ``1`` and optionally raise the cap.

    Returns ``None`` if the token is unknown, belongs to a different
    group, or is rotated (a rotated invite is dead — mint a new one
    instead).
    """
    row = await session.get(GroupInviteLink, token)
    if row is None or row.group_id != group_id or row.rotated_at:
        return None
    if new_slot_cap is not None:
        if new_slot_cap < 0:
            raise ValueError("new_slot_cap must be >= 0")
        row.slot_cap = int(new_slot_cap)
    row.active = 1
    await session.flush()
    return _row_to_invite(row)


async def delete_invite(
    *,
    session: AsyncSession,
    token: str,
    group_id: str,
) -> bool:
    """Hard-delete an invite row. Returns ``True`` if a row was removed.

    Unlike :func:`rotate_invite` (which preserves the dead row for
    audit), this drops the row entirely. The audit log still carries
    the mint event, so the trail isn't lost.
    """
    row = await session.get(GroupInviteLink, token)
    if row is None or row.group_id != group_id:
        return False
    await session.delete(row)
    await session.flush()
    return True


__all__ = [
    "TOKEN_BYTES",
    "REASON_OK",
    "REASON_NOT_FOUND",
    "REASON_ROTATED",
    "REASON_INACTIVE",
    "REASON_CAP_REACHED",
    "REASON_WRONG_GROUP",
    "InviteLink",
    "InviteValidation",
    "InviteConsumeResult",
    "mint_invite",
    "validate_invite",
    "consume_invite",
    "rotate_invite",
    "reopen_invite",
    "delete_invite",
]
