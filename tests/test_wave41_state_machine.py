"""Wave 41 — relay binding state machine."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core import STATE
from nexus.runtime import relay_state as rs
from nexus.security import tokens
from nexus.storage import database, get_session
from nexus.storage.models import GroupRelayBinding
from nexus.utils.time import iso_now


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url


def _make_binding(state: str = rs.STATE_ONLINE) -> str:
    """Insert a binding in *state* and return ``(group_id, relay_url)``."""
    async def _go():
        async with get_session() as s:
            s.add(GroupRelayBinding(
                group_id="g1",
                relay_url="wss://r.example",
                operator_pubkey="op",
                registered_at=iso_now(),
                state=state,
                consecutive_probe_failures=0,
            ))
            await s.commit()
    asyncio.run(_go())


def _apply(target: str, *, reason: str = "", initial: str = rs.STATE_ONLINE,
           failures: int = 0) -> tuple[bool, str, int]:
    async def _go():
        async with get_session() as s:
            row = await s.get(GroupRelayBinding, ("g1", "wss://r.example"))
            if failures:
                row.consecutive_probe_failures = failures
            changed = await rs.transition(row, target, reason=reason)
            await s.commit()
            return changed, row.state, row.consecutive_probe_failures
    return asyncio.run(_go())


# ---------- predicates ----------

def test_can_transition_allows_diagram_edges():
    for src, dests in rs.VALID_TRANSITIONS.items():
        for dst in dests:
            assert rs.can_transition(src, dst), f"{src}->{dst}"


def test_can_transition_blocks_unknown_targets():
    assert not rs.can_transition(rs.STATE_ONLINE, "vibes")
    assert not rs.can_transition("starting", "online")  # not adjacent
    assert not rs.can_transition(rs.STATE_RETIRED, rs.STATE_ONLINE)  # terminal


def test_retired_is_terminal():
    assert rs.reachable_targets(rs.STATE_RETIRED) == []


# ---------- happy-path transitions ----------

@pytest.mark.parametrize("src,dst", [
    (rs.STATE_STARTING, rs.STATE_VALIDATING),
    (rs.STATE_VALIDATING, rs.STATE_SYNCING),
    (rs.STATE_SYNCING, rs.STATE_ONLINE),
    (rs.STATE_ONLINE, rs.STATE_OFFLINE),
    (rs.STATE_OFFLINE, rs.STATE_RECONNECTING),
    (rs.STATE_RECONNECTING, rs.STATE_SYNCING),
])
def test_every_diagram_edge_applies(isolated_db, src, dst):
    _make_binding(state=src)
    changed, state, _ = _apply(dst)
    assert changed
    assert state == dst


def test_self_transition_is_noop(isolated_db):
    _make_binding(state=rs.STATE_ONLINE)
    changed, state, _ = _apply(rs.STATE_ONLINE)
    assert not changed
    assert state == rs.STATE_ONLINE


def test_illegal_transition_raises(isolated_db):
    _make_binding(state=rs.STATE_STARTING)
    with pytest.raises(rs.IllegalRelayStateTransition):
        _apply(rs.STATE_ONLINE)  # starting -> online is illegal (skips middle)


def test_unknown_target_raises(isolated_db):
    _make_binding(state=rs.STATE_ONLINE)
    with pytest.raises(rs.IllegalRelayStateTransition):
        _apply("vibes")


def test_retired_blocks_further_transitions(isolated_db):
    _make_binding(state=rs.STATE_ONLINE)
    _apply(rs.STATE_RETIRED)
    with pytest.raises(rs.IllegalRelayStateTransition):
        _apply(rs.STATE_OFFLINE)


# ---------- side effect: ONLINE entry zeros the failure counter ----------

def test_online_entry_resets_failure_counter(isolated_db):
    _make_binding(state=rs.STATE_SYNCING)
    _, state, fails = _apply(rs.STATE_ONLINE, failures=5)
    assert state == rs.STATE_ONLINE
    assert fails == 0


def test_non_online_entry_preserves_failure_counter(isolated_db):
    _make_binding(state=rs.STATE_ONLINE)
    _, state, fails = _apply(rs.STATE_OFFLINE, failures=3)
    assert state == rs.STATE_OFFLINE
    assert fails == 3
