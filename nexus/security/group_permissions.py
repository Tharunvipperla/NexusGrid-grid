"""Group permission constants + effective-permissions helper.

Permission strings are open and treated as opaque tags. The fixed set
below covers 's needs. Future waves will register additional
``service:use:<id>`` strings dynamically when new services are added
to a group; the model is forward-compatible since
``permissions_json`` on :class:`GroupRole` is just a JSON array.

A member's **effective permissions** = the union of permission sets
of every role assigned to them in the group. Caller checks like:

    perms = await effective_permissions(session, group_id, my_pubkey)
    if PERM_GROUP_INVITE not in perms:
        raise PermissionError

are the canonical guard at the API layer.
"""

from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.storage.models import GroupMemberRole, GroupRole


# ---- permission strings -------------------------------------------------

PERM_GROUP_READ = "group:read"
PERM_GROUP_INVITE = "group:invite"
PERM_GROUP_APPROVE = "group:approve"
PERM_MEMBER_KICK = "member:kick"
PERM_MEMBER_MUTE = "member:mute"
PERM_ROLE_ASSIGN = "role:assign"
PERM_SERVICE_LIST = "service:list"
PERM_SERVICE_HOST = "service:host"
PERM_RELAY_HOST = "relay:host"
PERM_RELAY_USE = "relay:use"
# Authorize a (content-aware) relay to read group content by sharing
# the group symkey with it. Deliberately NOT implied by relay:host — a relay
# operator must NOT be able to self-authorize reading everyone's messages; only
# founder/admin can make this visible group decision.
PERM_RELAY_SHARE_CONTENT = "relay:share_content"
# Receive group-scoped compute tasks (+ host group-scoped storage).
# Baseline like relay:use; revocable per-role to make a member observer-only.
PERM_TASK_RUN = "task:run"

# Service-use is parametric: ``service:use:<service-id>``. The bare
# prefix is exposed as a convenience for callers that want to filter.
SERVICE_USE_PREFIX = "service:use:"


# All non-parametric permissions a founder gets by default.
_ALL_NONPARAMETRIC = (
    PERM_GROUP_READ,
    PERM_GROUP_INVITE,
    PERM_GROUP_APPROVE,
    PERM_MEMBER_KICK,
    PERM_MEMBER_MUTE,
    PERM_ROLE_ASSIGN,
    PERM_SERVICE_LIST,
    PERM_SERVICE_HOST,
    PERM_RELAY_HOST,
    PERM_RELAY_USE,
    PERM_RELAY_SHARE_CONTENT,
    PERM_TASK_RUN,
)


# Default roles installed on group creation. Per the doc:
# - founder + admin: identical perms; rank difference is enforced at the
#   API (only a founder can delete the group / demote the founder).
# - member: least-privilege. Sees structure + may use relays the group is
#   bound to (relay:use is baseline like group:read; hosting a relay still
#   needs the separate relay:host).
DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "founder": _ALL_NONPARAMETRIC,
    "admin": _ALL_NONPARAMETRIC,
    "member": (PERM_GROUP_READ, PERM_RELAY_USE, PERM_TASK_RUN),
}


# ---- effective-permissions queries --------------------------------------


async def effective_permissions(
    session: AsyncSession,
    group_id: str,
    member_pubkey: str,
) -> frozenset[str]:
    """Return the union of permissions across every role the member holds."""
    assignments = await session.execute(
        select(GroupMemberRole.role_name).where(
            (GroupMemberRole.group_id == group_id)
            & (GroupMemberRole.member_pubkey == member_pubkey)
        )
    )
    role_names = [row[0] for row in assignments.fetchall()]
    if not role_names:
        return frozenset()
    role_rows = await session.execute(
        select(GroupRole.permissions_json).where(
            (GroupRole.group_id == group_id) & (GroupRole.name.in_(role_names))
        )
    )
    perms: set[str] = set()
    for (perms_json,) in role_rows.fetchall():
        try:
            decoded = json.loads(perms_json or "[]")
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, list):
            perms.update(p for p in decoded if isinstance(p, str))
    return frozenset(perms)


async def has_permission(
    session: AsyncSession,
    group_id: str,
    member_pubkey: str,
    permission: str,
) -> bool:
    """Shortcut: ``permission in effective_permissions(...)``."""
    perms = await effective_permissions(session, group_id, member_pubkey)
    return permission in perms


def encode_role_permissions(permissions: Iterable[str]) -> str:
    """Canonical JSON encoding for the ``permissions_json`` column."""
    return json.dumps(sorted(set(permissions)), separators=(",", ":"))


def decode_role_permissions(blob: str | None) -> tuple[str, ...]:
    """Parse ``permissions_json``; returns ``()`` on malformed input."""
    if not blob:
        return ()
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(p for p in data if isinstance(p, str))


__all__ = [
    "PERM_GROUP_READ",
    "PERM_GROUP_INVITE",
    "PERM_GROUP_APPROVE",
    "PERM_MEMBER_KICK",
    "PERM_ROLE_ASSIGN",
    "PERM_SERVICE_LIST",
    "PERM_SERVICE_HOST",
    "PERM_RELAY_HOST",
    "PERM_RELAY_USE",
    "PERM_RELAY_SHARE_CONTENT",
    "SERVICE_USE_PREFIX",
    "DEFAULT_ROLES",
    "effective_permissions",
    "has_permission",
    "encode_role_permissions",
    "decode_role_permissions",
]
