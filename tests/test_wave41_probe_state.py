"""Wave 41 — probe-failure → state-machine integration."""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import relay_latency, relay_state
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


def _seed(state: str = relay_state.STATE_ONLINE, failures: int = 0,
          group_id: str = "g1", url: str = "wss://r.example") -> None:
    async def _go():
        async with get_session() as s:
            s.add(GroupRelayBinding(
                group_id=group_id, relay_url=url,
                operator_pubkey="op",
                registered_at=iso_now(),
                state=state,
                consecutive_probe_failures=failures,
            ))
            await s.commit()
    asyncio.run(_go())


def _row(group_id: str = "g1", url: str = "wss://r.example") -> GroupRelayBinding:
    async def _go():
        async with get_session() as s:
            return await s.get(GroupRelayBinding, (group_id, url))
    return asyncio.run(_go())


def _patch_probe(monkeypatch, *, reachable: bool, rtt_ms: int | None = 42):
    async def fake(url: str):
        return reachable, (rtt_ms if reachable else None)
    monkeypatch.setattr("nexus.api.groups._probe_relay_url", fake)


def _stub_publish_frame(monkeypatch, captured: list):
    async def fake_publish(*, session, group_id, frame_type, payload_dict,
                          exclude_pubkeys=None, poster=None):
        captured.append({
            "group_id": group_id,
            "frame_type": frame_type,
            "payload": payload_dict,
        })
        return {"via": "test-stub"}
    monkeypatch.setattr(
        "nexus.runtime.group_inbox.publish_frame", fake_publish
    )


# ---------- failure paths ----------

def test_single_failure_bumps_counter_no_transition(isolated_db, monkeypatch):
    _seed(state=relay_state.STATE_ONLINE)
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=False)
    asyncio.run(relay_latency._probe_once("wss://r.example"))
    r = _row()
    assert r.state == relay_state.STATE_ONLINE
    assert r.consecutive_probe_failures == 1
    assert captured == []


def test_threshold_failures_flip_to_offline(isolated_db, monkeypatch):
    _seed(state=relay_state.STATE_ONLINE,
          failures=relay_latency.OFFLINE_FAILURE_THRESHOLD - 1)
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=False)
    asyncio.run(relay_latency._probe_once("wss://r.example"))
    r = _row()
    assert r.state == relay_state.STATE_OFFLINE
    assert r.consecutive_probe_failures == relay_latency.OFFLINE_FAILURE_THRESHOLD
    # Going offline is silent — traffic auto-routes through surviving
    # relays; no special frame is published.
    assert captured == []


def test_failure_on_already_offline_no_publish(isolated_db, monkeypatch):
    _seed(state=relay_state.STATE_OFFLINE, failures=5)
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=False)
    asyncio.run(relay_latency._probe_once("wss://r.example"))
    r = _row()
    # Counter keeps climbing — informative — but state doesn't flip
    # again and nothing is published.
    assert r.state == relay_state.STATE_OFFLINE
    assert r.consecutive_probe_failures == 6
    assert captured == []


# ---------- recovery paths ----------

def test_probe_success_from_offline_walks_to_online(isolated_db, monkeypatch):
    _seed(state=relay_state.STATE_OFFLINE, failures=4)
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=True)
    asyncio.run(relay_latency._probe_once("wss://r.example"))
    r = _row()
    # The probe walks the binding all the way through reconnecting →
    # syncing → online in a single tick because each leg is gated only
    # on a successful probe.
    assert r.state == relay_state.STATE_ONLINE
    # STATE_ONLINE entry resets the counter.
    assert r.consecutive_probe_failures == 0
    assert r.last_rtt_ms == 42


def test_probe_success_while_already_online_is_noop(isolated_db, monkeypatch):
    _seed(state=relay_state.STATE_ONLINE, failures=0)
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=True, rtt_ms=17)
    asyncio.run(relay_latency._probe_once("wss://r.example"))
    r = _row()
    assert r.state == relay_state.STATE_ONLINE
    assert r.last_rtt_ms == 17
    assert captured == []


def test_multiple_bindings_for_same_url_all_transition(isolated_db, monkeypatch):
    # Two groups each bound to the same relay URL — the probe runs once
    # but the state machine bumps both rows independently.
    _seed(state=relay_state.STATE_ONLINE,
          failures=relay_latency.OFFLINE_FAILURE_THRESHOLD - 1,
          group_id="g1", url="wss://shared")
    _seed(state=relay_state.STATE_ONLINE,
          failures=relay_latency.OFFLINE_FAILURE_THRESHOLD - 1,
          group_id="g2", url="wss://shared")
    captured = []
    _stub_publish_frame(monkeypatch, captured)
    _patch_probe(monkeypatch, reachable=False)
    asyncio.run(relay_latency._probe_once("wss://shared"))
    r1 = _row("g1", "wss://shared")
    r2 = _row("g2", "wss://shared")
    assert r1.state == relay_state.STATE_OFFLINE
    assert r2.state == relay_state.STATE_OFFLINE
    assert captured == []
