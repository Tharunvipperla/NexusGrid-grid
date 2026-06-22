"""Wave 15.4 — /local/groups/* API endpoints.

Integration tests against the real router + DB. Auth is bypassed via
``dependency_overrides`` because the tests don't care about the local
bearer-token surface — they exercise the group-level permission logic
the router enforces internally.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_permissions import (
    PERM_GROUP_INVITE,
    PERM_ROLE_ASSIGN,
)
from nexus.storage import database


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


@pytest.fixture
def client(isolated_db):
    app = FastAPI()
    app.include_router(groups_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def _create_group(client, name: str = "GPU Machines") -> dict:
    res = client.post("/local/groups", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()


# ---- create / list / detail ---------------------------------------------


def test_create_group_returns_id_and_founder_pubkey(client):
    out = _create_group(client)
    assert out["name"] == "GPU Machines"
    assert len(out["founder_pubkey"]) == 64
    assert out["id"]


def test_list_groups_returns_only_groups_this_node_is_in(client):
    a = _create_group(client, "Alpha")
    b = _create_group(client, "Beta")
    res = client.get("/local/groups")
    assert res.status_code == 200
    groups = res.json()["groups"]
    ids = {g["id"] for g in groups}
    assert {a["id"], b["id"]} <= ids
    # Founder is in the my_roles for every group they created.
    for g in groups:
        assert "founder" in g["my_roles"]


def test_get_group_detail_includes_default_roles_and_founder_member(client):
    g = _create_group(client)
    res = client.get(f"/local/groups/{g['id']}")
    assert res.status_code == 200
    body = res.json()

    role_names = {r["name"] for r in body["roles"]}
    assert {"founder", "admin", "member"} <= role_names

    members = {m["pubkey"]: m for m in body["members"]}
    assert g["founder_pubkey"] in members
    assert "founder" in members[g["founder_pubkey"]]["roles"]


def test_get_group_detail_404_for_unknown_group(client):
    res = client.get("/local/groups/no-such-group")
    assert res.status_code == 404


# ---- invites ------------------------------------------------------------


def test_mint_invite_returns_token_and_cap(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 5}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["slot_cap"] == 5
    assert body["slots_filled"] == 0
    assert body["active"] is True
    assert body["token"]


def test_rotate_invite_kills_old_returns_new(client):
    g = _create_group(client)
    minted = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 3}
    ).json()
    res = client.post(
        f"/local/groups/{g['id']}/invites/{minted['token']}/rotate"
    )
    assert res.status_code == 200
    new = res.json()
    assert new["token"] != minted["token"]
    assert new["slot_cap"] == 3
    assert new["slots_filled"] == 0


def test_rotate_invite_unknown_token_404(client):
    g = _create_group(client)
    res = client.post(f"/local/groups/{g['id']}/invites/bogus/rotate")
    assert res.status_code == 404


def test_reopen_invite_raises_cap(client):
    g = _create_group(client)
    minted = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    ).json()
    res = client.post(
        f"/local/groups/{g['id']}/invites/{minted['token']}/reopen",
        json={"new_slot_cap": 10},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["slot_cap"] == 10
    assert body["active"] is True


def test_reopen_invite_404_for_rotated(client):
    g = _create_group(client)
    minted = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 5}
    ).json()
    client.post(f"/local/groups/{g['id']}/invites/{minted['token']}/rotate")
    res = client.post(
        f"/local/groups/{g['id']}/invites/{minted['token']}/reopen",
        json={},
    )
    assert res.status_code == 404


def test_delete_invite_removes_row(client):
    g = _create_group(client)
    minted = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 3}
    ).json()
    res = client.delete(
        f"/local/groups/{g['id']}/invites/{minted['token']}"
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    # Re-opening a deleted token should 404 — row is gone, not rotated.
    res2 = client.post(
        f"/local/groups/{g['id']}/invites/{minted['token']}/reopen",
        json={},
    )
    assert res2.status_code == 404


def test_delete_invite_404_for_unknown_token(client):
    g = _create_group(client)
    res = client.delete(f"/local/groups/{g['id']}/invites/bogus")
    assert res.status_code == 404


# ---- roles --------------------------------------------------------------


def test_upsert_role_creates_new_custom_role(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/roles",
        json={
            "name": "db-readers",
            "permissions": ["service:use:postgres-prod", "group:read"],
        },
    )
    assert res.status_code == 200
    detail = client.get(f"/local/groups/{g['id']}").json()
    db_role = next(r for r in detail["roles"] if r["name"] == "db-readers")
    assert set(db_role["permissions"]) == {
        "service:use:postgres-prod",
        "group:read",
    }


def test_upsert_role_updates_existing_perms(client):
    g = _create_group(client)
    client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "db-readers", "permissions": ["service:use:a"]},
    )
    client.post(
        f"/local/groups/{g['id']}/roles",
        json={
            "name": "db-readers",
            "permissions": ["service:use:a", "service:use:b"],
        },
    )
    detail = client.get(f"/local/groups/{g['id']}").json()
    db_role = next(r for r in detail["roles"] if r["name"] == "db-readers")
    assert set(db_role["permissions"]) == {"service:use:a", "service:use:b"}


def test_upsert_role_refuses_founder_role(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "founder", "permissions": []},
    )
    assert res.status_code == 409


def test_upsert_role_refuses_member_role(client):
    # Post-ship rule: ``member`` is the baseline-read floor and its
    # perms must not be editable, mirroring the founder lock.
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "member", "permissions": []},
    )
    assert res.status_code == 409


def test_delete_role_removes_custom_role_and_detaches_assignments(client):
    g = _create_group(client)
    client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "db-readers", "permissions": ["service:use:a"]},
    )
    res = client.delete(f"/local/groups/{g['id']}/roles/db-readers")
    assert res.status_code == 200

    detail = client.get(f"/local/groups/{g['id']}").json()
    role_names = {r["name"] for r in detail["roles"]}
    assert "db-readers" not in role_names


def test_delete_role_refuses_default_roles(client):
    g = _create_group(client)
    for name in ("founder", "admin", "member"):
        res = client.delete(f"/local/groups/{g['id']}/roles/{name}")
        assert res.status_code == 409, f"expected 409 deleting {name}"


def test_delete_role_404_for_unknown_role(client):
    g = _create_group(client)
    res = client.delete(f"/local/groups/{g['id']}/roles/nonesuch")
    assert res.status_code == 404


# ---- member-role assignment ---------------------------------------------


def test_assign_roles_to_founder_keeps_founder_role(client):
    g = _create_group(client)
    # Adding the admin role is fine; founder must stay too. ``member`` is
    # always auto-included (post-ship rule — baseline read floor).
    res = client.post(
        f"/local/groups/{g['id']}/members/{g['founder_pubkey']}/roles",
        json={"roles": ["founder", "admin"]},
    )
    assert res.status_code == 200
    assert set(res.json()["roles"]) == {"founder", "admin", "member"}


def test_assign_roles_cannot_strip_founder_role_from_founder(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/members/{g['founder_pubkey']}/roles",
        json={"roles": ["admin"]},
    )
    assert res.status_code == 409


def test_assign_roles_rejects_unknown_role(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/members/{g['founder_pubkey']}/roles",
        json={"roles": ["founder", "ghost-role"]},
    )
    assert res.status_code == 400
    assert "ghost-role" in res.text


def test_assign_roles_404_for_unknown_member(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/members/{'a' * 64}/roles",
        json={"roles": ["member"]},
    )
    assert res.status_code == 404


# ---- 15.8 founder pre-delegate prompt ----------------------------------


def test_skip_predelegate_writes_audit_event(client, monkeypatch):
    captured: list[dict] = []

    async def fake_write(**kw):
        captured.append(kw)

    monkeypatch.setattr("nexus.api.groups.write_audit_event", fake_write)

    g = _create_group(client, "Audit-checked")
    res = client.post(f"/local/groups/{g['id']}/skip_predelegate")
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    skip_events = [e for e in captured if e["action"] == "group.predelegate.skipped"]
    assert len(skip_events) == 1
    assert g["id"] in skip_events[0]["details"]


def test_skip_predelegate_404_for_unknown_group(client):
    res = client.post("/local/groups/no-such/skip_predelegate")
    assert res.status_code == 404


# ---- permission gating --------------------------------------------------


def test_member_without_invite_perm_gets_403(client, isolated_db):
    """Demote the local founder to a plain member (no invite perm) and
    confirm that mint_invite is then rejected by the perm check."""
    g = _create_group(client)
    # Make a 'plain' role with only group:read.
    client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "plain", "permissions": ["group:read"]},
    )
    # Bypass the founder-protection by editing roles directly in the DB.
    # We can't strip founder via the API (it 409s), so we go raw.
    async def _swap_to_plain():
        from sqlalchemy import delete
        from nexus.storage import get_session
        from nexus.storage.models import GroupMemberRole

        async with get_session() as s:
            await s.execute(
                delete(GroupMemberRole).where(
                    GroupMemberRole.group_id == g["id"]
                )
            )
            s.add(
                GroupMemberRole(
                    group_id=g["id"],
                    member_pubkey=g["founder_pubkey"],
                    role_name="plain",
                    assigned_by_pubkey=g["founder_pubkey"],
                    assigned_at="2026-05-19T00:00:00+00:00",
                )
            )
            await s.commit()

    asyncio.run(_swap_to_plain())

    res = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": 1}
    )
    assert res.status_code == 403
    assert PERM_GROUP_INVITE in res.text


def test_member_without_role_assign_cannot_upsert_role(client, isolated_db):
    g = _create_group(client)
    client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "plain", "permissions": ["group:read"]},
    )

    async def _swap_to_plain():
        from sqlalchemy import delete
        from nexus.storage import get_session
        from nexus.storage.models import GroupMemberRole

        async with get_session() as s:
            await s.execute(
                delete(GroupMemberRole).where(
                    GroupMemberRole.group_id == g["id"]
                )
            )
            s.add(
                GroupMemberRole(
                    group_id=g["id"],
                    member_pubkey=g["founder_pubkey"],
                    role_name="plain",
                    assigned_by_pubkey=g["founder_pubkey"],
                    assigned_at="2026-05-19T00:00:00+00:00",
                )
            )
            await s.commit()

    asyncio.run(_swap_to_plain())

    res = client.post(
        f"/local/groups/{g['id']}/roles",
        json={"name": "new", "permissions": []},
    )
    assert res.status_code == 403
    assert PERM_ROLE_ASSIGN in res.text


# ---- input validation ---------------------------------------------------


def test_create_group_rejects_blank_name(client):
    res = client.post("/local/groups", json={"name": ""})
    assert res.status_code == 422


def test_mint_invite_rejects_negative_cap(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/invites", json={"slot_cap": -1}
    )
    assert res.status_code == 422


# ---- Wave 24: relay-binding reachability --------------------------------


def _create_group_with_relays(client, urls, name="Relayed"):
    res = client.post(
        "/local/groups", json={"name": name, "relay_urls": urls}
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_probe_relays_marks_unreachable(client):
    # 127.0.0.1:1 — nothing listens; the probe fails fast.
    g = _create_group_with_relays(client, ["wss://127.0.0.1:1"])
    res = client.post(f"/local/groups/{g['id']}/relays/probe")
    assert res.status_code == 200, res.text
    relays = res.json()["relays"]
    assert len(relays) == 1
    assert relays[0]["reachable"] is False
    assert relays[0]["status"] == "unreachable"


def test_probe_relays_marks_active(client, monkeypatch):
    # Wave 36.A: _probe_relay_url now returns (reachable, rtt_ms_or_None).
    async def _always_reachable(url):
        return True, 42

    monkeypatch.setattr(
        "nexus.api.groups._probe_relay_url", _always_reachable
    )
    g = _create_group_with_relays(client, ["wss://relay.example"])
    res = client.post(f"/local/groups/{g['id']}/relays/probe")
    assert res.status_code == 200, res.text
    relays = res.json()["relays"]
    assert relays[0]["status"] == "active"
    assert relays[0]["reachable"] is True
    assert relays[0]["last_seen_at"]
    assert relays[0]["last_rtt_ms"] == 42


def test_probe_relays_persists_status_into_detail(client, monkeypatch):
    async def _unreachable(url):
        return False, None

    monkeypatch.setattr("nexus.api.groups._probe_relay_url", _unreachable)
    g = _create_group_with_relays(client, ["wss://relay.example"])
    client.post(f"/local/groups/{g['id']}/relays/probe")
    detail = client.get(f"/local/groups/{g['id']}").json()
    assert detail["relays"][0]["status"] == "unreachable"


def test_probe_relays_404_for_unknown_group(client):
    res = client.post("/local/groups/no-such-group/relays/probe")
    assert res.status_code == 404


def test_list_groups_reports_relay_counts(client, monkeypatch):
    async def _reachable(url):
        return True, 17

    monkeypatch.setattr("nexus.api.groups._probe_relay_url", _reachable)
    g = _create_group_with_relays(
        client, ["wss://a.example", "wss://b.example"]
    )
    client.post(f"/local/groups/{g['id']}/relays/probe")
    groups = {x["id"]: x for x in client.get("/local/groups").json()["groups"]}
    assert groups[g["id"]]["relay_count"] == 2
    assert groups[g["id"]]["relay_active_count"] == 2


def test_group_detail_member_includes_node_id(client):
    g = _create_group(client)
    detail = client.get(f"/local/groups/{g['id']}").json()
    # The founder's own row carries a node_id key (Wave 24 surfaced it
    # for the Members-tab Connect button).
    assert all("node_id" in m for m in detail["members"])


# ---- Wave 25: relay binding management ----------------------------------


def _relay_urls_in_detail(client, group_id) -> set:
    detail = client.get(f"/local/groups/{group_id}").json()
    return {r["relay_url"] for r in detail["relays"]}


def test_add_relay_binds_to_group(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/relays",
        json={"relay_url": "wss://relay.example.com"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "active"
    assert "wss://relay.example.com" in _relay_urls_in_detail(client, g["id"])


def test_add_relay_is_idempotent(client):
    g = _create_group(client)
    for _ in range(2):
        client.post(
            f"/local/groups/{g['id']}/relays",
            json={"relay_url": "wss://relay.example.com"},
        )
    detail = client.get(f"/local/groups/{g['id']}").json()
    matching = [
        r for r in detail["relays"]
        if r["relay_url"] == "wss://relay.example.com"
    ]
    assert len(matching) == 1


def test_add_relay_reactivates_a_removed_binding(client):
    g = _create_group(client)
    url = "wss://relay.example.com"
    client.post(f"/local/groups/{g['id']}/relays", json={"relay_url": url})
    client.delete(
        f"/local/groups/{g['id']}/relays", params={"relay_url": url}
    )
    assert url not in _relay_urls_in_detail(client, g["id"])
    # Re-adding the same URL flips the retired row back to active.
    res = client.post(
        f"/local/groups/{g['id']}/relays", json={"relay_url": url}
    )
    assert res.status_code == 200, res.text
    assert url in _relay_urls_in_detail(client, g["id"])


def test_add_relay_rejects_schemeless_url(client):
    g = _create_group(client)
    res = client.post(
        f"/local/groups/{g['id']}/relays",
        json={"relay_url": "relay.example.com"},
    )
    assert res.status_code == 400


def test_add_relay_404_for_unknown_group(client):
    res = client.post(
        "/local/groups/no-such-group/relays",
        json={"relay_url": "wss://relay.example.com"},
    )
    assert res.status_code == 404


def test_remove_relay_retires_binding(client):
    g = _create_group(client)
    url = "wss://relay.example.com"
    client.post(f"/local/groups/{g['id']}/relays", json={"relay_url": url})
    res = client.delete(
        f"/local/groups/{g['id']}/relays", params={"relay_url": url}
    )
    assert res.status_code == 200, res.text
    assert url not in _relay_urls_in_detail(client, g["id"])


def test_remove_relay_404_for_unbound_url(client):
    g = _create_group(client)
    res = client.delete(
        f"/local/groups/{g['id']}/relays",
        params={"relay_url": "wss://nope.example.com"},
    )
    assert res.status_code == 404


def test_add_relay_403_without_relay_host(client):
    g = _create_group(client)

    async def _swap_to_member():
        from sqlalchemy import delete
        from nexus.storage import get_session
        from nexus.storage.models import GroupMemberRole

        async with get_session() as s:
            await s.execute(
                delete(GroupMemberRole).where(
                    GroupMemberRole.group_id == g["id"]
                )
            )
            s.add(
                GroupMemberRole(
                    group_id=g["id"],
                    member_pubkey=g["founder_pubkey"],
                    role_name="member",
                    assigned_by_pubkey=g["founder_pubkey"],
                    assigned_at="2026-05-20T00:00:00+00:00",
                )
            )
            await s.commit()

    asyncio.run(_swap_to_member())
    res = client.post(
        f"/local/groups/{g['id']}/relays",
        json={"relay_url": "wss://relay.example.com"},
    )
    assert res.status_code == 403


def test_create_group_records_relay_bindings(client):
    out = _create_group_with_relays(
        client, ["wss://a.example.com", "wss://b.example.com"]
    )
    urls = _relay_urls_in_detail(client, out["id"])
    assert {"wss://a.example.com", "wss://b.example.com"} <= urls
