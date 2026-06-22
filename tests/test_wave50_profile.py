"""Wave 50 — node profiles (about-me, advertised services, global usage)."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import LOCAL_SETTINGS, normalize_hosted_services, normalize_local_settings
from nexus.security import group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import TaskRecord, UsageReceipt


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "groups.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


# --- config normalization ----------------------------------------------------


_EMPTY = {"description": "", "version": "", "tags": [], "readme": "", "pump": "",
          "replicable": False, "run": {}, "components": [],
          "local_host": "127.0.0.1", "local_port": 0,
          "service_kind": "", "db_provider": {}}


def test_normalize_hosted_services_filters_and_caps():
    out = normalize_hosted_services([
        {"name": "Redis", "version": "7.2", "access": "free"},
        {"name": "PG", "access": "permission"},
        {"access": "free"},               # no name → dropped
        {"name": "X", "access": "bogus"},  # bad access → free
        "not-a-dict",                      # ignored
    ])
    assert out == [
        {**_EMPTY, "name": "Redis", "version": "7.2", "access": "free"},
        {**_EMPTY, "name": "PG", "access": "permission"},
        {**_EMPTY, "name": "X", "access": "free"},
    ]


def test_normalize_hosted_services_freeform():
    out = normalize_hosted_services([{
        "name": "Ollama", "version": "0.3", "access": "free",
        "description": "LLM server", "readme": "# Ollama\nrun it",
        "local_port": 11434,
    }])
    assert out[0]["description"] == "LLM server"
    assert out[0]["readme"] == "# Ollama\nrun it"
    assert out[0]["local_port"] == 11434


def test_normalize_about_me_capped():
    s = normalize_local_settings({"about_me": "a" * 5000})
    assert len(s["about_me"]) == 1000


# --- receipt-derived global usage --------------------------------------------


def _add_receipt(provider, consumer, kind, amount, rid):
    async def _go():
        async with get_session() as s:
            s.add(UsageReceipt(
                receipt_id=rid, group_id="g", provider_pubkey=provider,
                consumer_pubkey=consumer, kind=kind, ref_id="x",
                amount=amount, ts="2026-06-01T00:00:00Z", sig="",
            ))
            await s.commit()
    asyncio.run(_go())


def test_global_usage_summary_splits_by_role(isolated_db):
    from nexus.runtime.usage_receipts import global_usage_summary

    me = group_keys.get_local_group_pubkey()
    other = "other-pub"
    _add_receipt(me, other, "compute", 30, "r1")     # I provided compute
    _add_receipt(other, me, "compute", 12, "r2")     # I consumed compute
    _add_receipt(me, other, "storage", 1000, "r3")   # I hosted storage
    _add_receipt(other, other, "compute", 99, "r4")  # not me → ignored

    out = asyncio.run(global_usage_summary())
    assert out["compute_secs_contributed"] == 30
    assert out["compute_secs_consumed"] == 12
    assert out["storage_bytes_hosted"] == 1000
    assert out["storage_bytes_used"] == 0
    assert out["tasks_contributed"] == 1
    assert out["tasks_consumed"] == 1
    assert out["peers_helped"] == 1 and out["peers_used"] == 1


def test_reliability_with_peer_counts_outcomes(isolated_db):
    from nexus.api.local import _reliability_with_peer

    async def _seed():
        async with get_session() as s:
            for tid, status, worker in [
                ("t1", "completed", "peerX"),
                ("t2", "completed", "peerX"),
                ("t3", "failed", "peerX"),
                ("t4", "disrupted", "peerX"),
                ("t5", "cancelled", "peerX"),   # user-cancelled → not a failure
                ("t6", "completed", "other"),   # different worker → ignored
            ]:
                s.add(TaskRecord(id=tid, status=status, worker=worker, payload=b""))
            await s.commit()

    asyncio.run(_seed())
    r = asyncio.run(_reliability_with_peer("peerX"))
    assert r["ok"] == 2
    assert r["failed"] == 2  # failed + disrupted, not cancelled
    assert r["success_rate"] == 50


def test_reliability_with_peer_no_tasks(isolated_db):
    from nexus.api.local import _reliability_with_peer

    r = asyncio.run(_reliability_with_peer("nobody"))
    assert r == {"ok": 0, "failed": 0, "success_rate": None}


def test_exchange_with_pubkey_bilateral(isolated_db):
    from nexus.api.local import _exchange_with_pubkey

    me = group_keys.get_local_group_pubkey()
    friend = "friend-pub"
    _add_receipt(friend, me, "compute", 40, "e1")     # friend ran for me
    _add_receipt(me, friend, "compute", 7, "e2")      # I ran for friend
    _add_receipt(friend, me, "storage", 2048, "e3")   # friend hosted for me
    _add_receipt("z", "z", "compute", 99, "e4")       # unrelated → ignored

    x = asyncio.run(_exchange_with_pubkey(friend))
    assert x["they_gave_compute_secs"] == 40
    assert x["you_gave_compute_secs"] == 7
    assert x["they_hosted_bytes"] == 2048
    assert x["you_hosted_bytes"] == 0


# --- /local/profile round-trip -----------------------------------------------


class _FakeReq:
    def __init__(self, data):
        self._d = data

    async def json(self):
        return self._d


def test_profile_put_get_round_trip(isolated_db, monkeypatch):
    from nexus.api import local as local_api
    from nexus.storage.repositories import load_local_settings_from_db

    asyncio.run(load_local_settings_from_db())
    req = _FakeReq({
        "about_me": "hi there",
        "hosted_services": [{"name": "Redis", "version": "7", "access": "permission"}],
    })
    saved = asyncio.run(local_api.update_my_profile(req))
    assert saved["about_me"] == "hi there"
    assert saved["hosted_services"][0]["access"] == "permission"

    got = asyncio.run(local_api.get_my_profile())
    assert got["about_me"] == "hi there"
    assert got["hosted_services"] == [
        {**_EMPTY, "name": "Redis", "version": "7", "access": "permission"}
    ]
    assert "global_usage" in got

    # Persisted: reloading from DB keeps the values.
    LOCAL_SETTINGS["about_me"] = ""
    LOCAL_SETTINGS["hosted_services"] = []
    asyncio.run(load_local_settings_from_db())
    assert LOCAL_SETTINGS["about_me"] == "hi there"
