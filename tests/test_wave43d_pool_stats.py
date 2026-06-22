"""Wave 43.D3 — shared per-group compute-pool diagnostics."""

from __future__ import annotations

import asyncio

import pytest

from nexus.runtime import group_compute
from nexus.security import group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import GroupComputeStat, GroupMember


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


def _get_stat(group_id, pubkey):
    async def _go():
        async with get_session() as s:
            return await s.get(GroupComputeStat, (group_id, pubkey))
    return asyncio.run(_go())


def test_record_compute_stat_upserts_and_accumulates(isolated_db):
    asyncio.run(group_compute.record_compute_stat("g1", consumed=1))
    asyncio.run(group_compute.record_compute_stat("g1", contributed=2))
    me = group_keys.get_local_group_pubkey()
    row = _get_stat("g1", me)
    assert row.tasks_consumed == 1
    assert row.tasks_contributed == 2
    assert row.updated_at


def test_pool_stats_endpoint_lists_all_members(isolated_db, monkeypatch):
    async def _seed():
        async with get_session() as s:
            s.add(GroupMember(group_id="g1", pubkey="m1", display_name="Alice"))
            s.add(GroupMember(group_id="g1", pubkey="m2", display_name="Bob"))
            s.add(GroupComputeStat(
                group_id="g1", member_pubkey="m1",
                tasks_contributed=4, tasks_consumed=1,
            ))
            await s.commit()

    asyncio.run(_seed())

    from nexus.api import groups as groups_api

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(groups_api, "_require_group_exists", _ok)
    monkeypatch.setattr(groups_api, "_require_perm", _ok)

    out = asyncio.run(groups_api.group_pool_stats("g1"))
    by = {m["pubkey"]: m for m in out["members"]}
    assert by["m1"]["tasks_contributed"] == 4 and by["m1"]["tasks_consumed"] == 1
    # A member with no recorded activity shows zeros, not missing.
    assert by["m2"]["tasks_contributed"] == 0 and by["m2"]["tasks_consumed"] == 0
