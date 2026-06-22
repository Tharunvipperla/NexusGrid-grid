"""Relay binding state machine.

Each ``GroupRelayBinding`` row carries a ``state`` column that traces
the relay's lifecycle for the UI and for the self-heal / takeover
logic.

States::

      starting ─► validating ─► syncing ─► online (steady)
                              │             │
                              ▼             ▼
                            offline ◄──────┘  (probe fails / catch-up fails)
                              │
                              ▼
                          reconnecting ─► syncing ─► online
                              │
                              ▼
                            offline   (retry exhausted)

    retired is terminal (operator stepped down or takeover replaced
    this binding).

Transitions are validated up front so a buggy caller can't put a
binding into an impossible state combination. Every transition writes
an audit event so the relay:host telemetry view can render a transition
timeline.
"""

from __future__ import annotations

import logging
from typing import Iterable

from nexus.storage.models import GroupRelayBinding
from nexus.telemetry import write_audit_event
from nexus.utils.time import iso_now

_log = logging.getLogger("nexus.runtime.relay_state")


STATE_STARTING = "starting"
STATE_VALIDATING = "validating"
STATE_SYNCING = "syncing"
STATE_ONLINE = "online"
STATE_OFFLINE = "offline"
STATE_RECONNECTING = "reconnecting"
STATE_RETIRED = "retired"


VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_STARTING: frozenset({STATE_VALIDATING, STATE_OFFLINE, STATE_RETIRED}),
    STATE_VALIDATING: frozenset({STATE_SYNCING, STATE_OFFLINE, STATE_RETIRED}),
    STATE_SYNCING: frozenset({STATE_ONLINE, STATE_OFFLINE, STATE_RETIRED}),
    STATE_ONLINE: frozenset({STATE_OFFLINE, STATE_RETIRED}),
    STATE_OFFLINE: frozenset({STATE_RECONNECTING, STATE_RETIRED}),
    STATE_RECONNECTING: frozenset({STATE_SYNCING, STATE_OFFLINE, STATE_RETIRED}),
    STATE_RETIRED: frozenset(),
}
"""Adjacency map: from each state, the set of legal next states."""


ALL_STATES: frozenset[str] = frozenset(VALID_TRANSITIONS.keys())


class IllegalRelayStateTransition(ValueError):
    """Raised when a caller asks for a transition not in :data:`VALID_TRANSITIONS`."""


def can_transition(current: str, target: str) -> bool:
    """Pure predicate — useful for the UI's button-enable logic."""
    return target in VALID_TRANSITIONS.get(current, frozenset())


async def transition(
    binding: GroupRelayBinding,
    new_state: str,
    *,
    reason: str = "",
) -> bool:
    """Move *binding* to *new_state* in-place; schedule a deferred audit.

    No-op (returns ``False``) when ``binding.state == new_state``.
    Raises :class:`IllegalRelayStateTransition` if the transition isn't
    in :data:`VALID_TRANSITIONS` so caller fixes its logic instead of
    silently corrupting the state column.

    Audit-write strategy: ``write_audit_event`` opens its own session.
    If we called it inline, that nested session would deadlock against
    the caller's still-open transaction (the binding row is locked).
    Instead we spawn a background task that fires on the next event-loop
    tick — by then the caller has typically committed and released the
    lock. The audit recorder swallows any remaining lock-contention error,
    so a missed audit never breaks the state-change itself.

    Side effect on entry into :data:`STATE_ONLINE`: ``consecutive_probe_failures``
    is zeroed.
    """
    current = (binding.state or "").strip().lower()
    target = (new_state or "").strip().lower()
    if target == current:
        return False
    if target not in ALL_STATES:
        raise IllegalRelayStateTransition(
            f"unknown target state {target!r}"
        )
    if not can_transition(current, target):
        raise IllegalRelayStateTransition(
            f"illegal transition {current!r} -> {target!r}"
        )

    binding.state = target
    binding.last_state_change_at = iso_now()
    if target == STATE_ONLINE:
        binding.consecutive_probe_failures = 0

    details = (
        f"group_id={binding.group_id} "
        f"relay_url={binding.relay_url} "
        f"{current}->{target} "
        f"reason={reason!r}"
    )
    # Spawn the audit write so it lands AFTER the caller commits.
    import asyncio as _asyncio

    async def _deferred_audit():
        # One event-loop tick + brief delay so the caller's commit
        # releases the write lock before we open a sibling session.
        await _asyncio.sleep(0)
        await write_audit_event(
            action="relay.state_change",
            actor="local",
            task_id="",
            details=details,
        )
    try:
        _asyncio.create_task(_deferred_audit())
    except RuntimeError:
        # No running loop (unlikely under FastAPI / async tests) — fall
        # back to sync await, accepting the rare lock-contention drop.
        await write_audit_event(
            action="relay.state_change",
            actor="local",
            task_id="",
            details=details,
        )
    return True


def reachable_targets(current: str) -> Iterable[str]:
    """All legal next states for *current* — useful in tests + UI hints."""
    return sorted(VALID_TRANSITIONS.get(current, frozenset()))


__all__ = [
    "STATE_STARTING",
    "STATE_VALIDATING",
    "STATE_SYNCING",
    "STATE_ONLINE",
    "STATE_OFFLINE",
    "STATE_RECONNECTING",
    "STATE_RETIRED",
    "VALID_TRANSITIONS",
    "ALL_STATES",
    "IllegalRelayStateTransition",
    "can_transition",
    "reachable_targets",
    "transition",
]
