"""Wave 53 Phase A — service-access grant lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import LOCAL_SETTINGS, normalize_hosted_services
from nexus.runtime import service_grants as sg
from nexus.security import group_keys, tokens
from nexus.security.group_grant import generate_keypair
from nexus.security.usage_receipt import sign_statement
from nexus.storage import database, get_session
from nexus.storage.models import Group, GroupMember, ServiceGrant


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
    LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services([
        {"name": "FreeLLM", "access": "free", "local_port": 11434},
        {"name": "PermDB", "access": "permission", "local_port": 5432},
        {"name": "PaidGPU", "access": "paid", "local_port": 9000},
    ])
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    LOCAL_SETTINGS["hosted_services"] = []
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def _seed_known_consumer(consumer_uuid, consumer_pub):
    async def _go():
        async with get_session() as s:
            s.add(GroupMember(group_id="g1", pubkey=consumer_pub, node_id=consumer_uuid))
            await s.commit()
    asyncio.run(_go())


def _signed_request(service, consumer_priv, consumer_pub, consumer_uuid):
    provider = group_keys.get_local_group_pubkey()
    ts = "2026-06-01T00:00:00Z"
    payload = sg._req_payload(provider, service, consumer_pub, consumer_uuid, ts)
    sig = sign_statement(sg.STMT_REQUEST, payload, consumer_priv)
    return {"service": service, "consumer_pubkey": consumer_pub,
            "consumer_uuid": consumer_uuid, "ts": ts, "sig": sig}


def test_tags_normalized_lowercase():
    svc = normalize_hosted_services([{"name": "Cache", "tags": ["Redis", " GPU ", "redis"]}])
    assert svc[0]["tags"] == ["redis", "gpu", "redis"]  # lowercased, trimmed


def test_freeform_readme_kept():
    svc = normalize_hosted_services([{
        "name": "Stack", "description": "a stack", "version": "1.2",
        "readme": "# Stack\nrun `docker compose up`\nhttps://github.com/x/y",
    }])[0]
    assert svc["description"] == "a stack" and svc["version"] == "1.2"
    assert "docker compose up" in svc["readme"]
    # The old proliferation of fields is gone — just the lean set + readme.
    assert set(svc) == {"name", "description", "version", "access", "tags",
                        "readme", "pump", "components", "replicable", "run",
                        "local_host", "local_port", "service_kind", "db_provider"}


def test_discover_aggregates_peer_services(isolated_db, monkeypatch):
    async def _seed():
        async with get_session() as s:
            s.add(Group(id="g1", name="Team"))
            s.add(GroupMember(group_id="g1", pubkey="pk-bob", node_id="nexus_bob", display_name="Bob"))
            await s.commit()
    asyncio.run(_seed())

    async def _fake_addr(uid):
        return "1.2.3.4:8001" if uid == "nexus_bob" else ""

    async def _fake_post(addr, path, body, timeout=5.0):
        return {"status": 200, "body": {
            "display_name": "Bob", "pubkey": "pk-bob",
            "hosted_services": [{"name": "BobLLM", "access": "free", "tags": ["llm"]}],
        }}

    monkeypatch.setattr(sg, "resolve_peer_addr", _fake_addr)
    monkeypatch.setattr("nexus.networking.peer_http.peer_http_post", _fake_post)

    out = asyncio.run(sg.discover_services())
    assert out["peers_queried"] == 1
    assert len(out["services"]) == 1
    item = out["services"][0]
    assert item["provider_uuid"] == "nexus_bob"
    assert item["service"]["name"] == "BobLLM"
    assert "Team" in item["source"]


def test_public_services_strips_local_target():
    from nexus.core.config import public_services
    svc = normalize_hosted_services([{
        "name": "LLM", "access": "free", "readme": "point here",
        "local_host": "127.0.0.1", "local_port": 11434,
    }])
    pub = public_services(svc)[0]
    assert "local_host" not in pub and "local_port" not in pub
    assert pub["readme"] == "point here" and pub["access"] == "free"


def test_free_auto_approves(isolated_db):
    priv, pub = generate_keypair()
    _seed_known_consumer("nexus_c1", pub)
    res = asyncio.run(sg.handle_service_request(_signed_request("FreeLLM", priv, pub, "nexus_c1")))
    assert res["ok"] and res["grant"]["status"] == "approved"


def test_permission_pends_then_approves(isolated_db):
    priv, pub = generate_keypair()
    _seed_known_consumer("nexus_c1", pub)
    res = asyncio.run(sg.handle_service_request(_signed_request("PermDB", priv, pub, "nexus_c1")))
    assert res["ok"] and res["grant"]["status"] == "pending"
    gid = res["grant"]["grant_id"]

    inbox = asyncio.run(sg.list_pending_requests())
    assert any(r["grant_id"] == gid for r in inbox)

    decided = asyncio.run(sg.decide_request(gid, True))  # push is a no-op in tests
    assert decided["ok"] and decided["grant"]["status"] == "approved"


def test_paid_denied(isolated_db):
    priv, pub = generate_keypair()
    _seed_known_consumer("nexus_c1", pub)
    res = asyncio.run(sg.handle_service_request(_signed_request("PaidGPU", priv, pub, "nexus_c1")))
    assert res["ok"] and res["grant"]["status"] == "denied"


def test_bad_signature_rejected(isolated_db):
    priv, pub = generate_keypair()
    attacker_priv, _ = generate_keypair()
    _seed_known_consumer("nexus_c1", pub)
    req = _signed_request("FreeLLM", priv, pub, "nexus_c1")
    # Re-sign with a different key but keep the claimed consumer pubkey.
    req["sig"] = sign_statement(
        sg.STMT_REQUEST,
        sg._req_payload(group_keys.get_local_group_pubkey(), "FreeLLM", pub, "nexus_c1", req["ts"]),
        attacker_priv,
    )
    res = asyncio.run(sg.handle_service_request(req))
    assert res["ok"] is False and res["error"] == "bad_signature"


def test_unknown_peer_rejected(isolated_db):
    priv, pub = generate_keypair()  # NOT seeded as a known peer
    res = asyncio.run(sg.handle_service_request(_signed_request("FreeLLM", priv, pub, "nexus_stranger")))
    assert res["ok"] is False and res["error"] == "not_connected"


def test_uuid_spoof_with_attacker_key_rejected(isolated_db):
    """SECURITY F-004: trust must bind to the pubkey the signature proves, not
    to the (gossiped, non-secret) UUID. An attacker who knows a member's UUID but
    signs with its OWN keypair must NOT be accepted as that member."""
    # A legit member is known by its real pubkey + UUID.
    _legit_priv, legit_pub = generate_keypair()
    _seed_known_consumer("nexus_member", legit_pub)
    # Attacker reuses the known UUID but presents (and validly signs for) its own key.
    atk_priv, atk_pub = generate_keypair()
    assert atk_pub != legit_pub
    req = _signed_request("FreeLLM", atk_priv, atk_pub, "nexus_member")
    res = asyncio.run(sg.handle_service_request(req))
    assert res["ok"] is False and res["error"] == "not_connected"


def test_known_member_accepted_by_pubkey(isolated_db):
    """The legitimate member (matching pubkey) is still accepted after the
    pubkey-binding fix — even if its UUID field is blank/unknown."""
    priv, pub = generate_keypair()
    _seed_known_consumer("nexus_member", pub)
    res = asyncio.run(sg.handle_service_request(_signed_request("FreeLLM", priv, pub, "nexus_member")))
    assert res["ok"] and res["grant"]["status"] == "approved"


def _seed_trusted_peer(ip, group_pubkey):
    from nexus.storage.models import Peer
    async def _go():
        async with get_session() as s:
            s.add(Peer(ip=ip, status="trusted", peer_group_pubkey=group_pubkey))
            await s.commit()
    asyncio.run(_go())


def test_trusted_peer_uuid_spoof_rejected(isolated_db, monkeypatch):
    """SECURITY F-005: the trusted-peer fallback must also bind the signed pubkey
    to the peer's recorded group identity — not just the gossiped UUID."""
    monkeypatch.setattr(sg, "resolve_uuid_to_ip",
                        lambda u: "10.0.0.5:8000" if u == "nexus_tp" else u)
    _legit_priv, legit_pub = generate_keypair()
    _seed_trusted_peer("10.0.0.5:8000", legit_pub)
    atk_priv, atk_pub = generate_keypair()
    assert atk_pub != legit_pub
    res = asyncio.run(sg.handle_service_request(
        _signed_request("FreeLLM", atk_priv, atk_pub, "nexus_tp")))
    assert res["ok"] is False and res["error"] == "not_connected"


def test_trusted_peer_accepted_by_pubkey(isolated_db, monkeypatch):
    monkeypatch.setattr(sg, "resolve_uuid_to_ip",
                        lambda u: "10.0.0.6:8000" if u == "nexus_tp2" else u)
    priv, pub = generate_keypair()
    _seed_trusted_peer("10.0.0.6:8000", pub)
    res = asyncio.run(sg.handle_service_request(
        _signed_request("FreeLLM", priv, pub, "nexus_tp2")))
    assert res["ok"] and res["grant"]["status"] == "approved"


def test_replicable_defaults_false_and_public(isolated_db):
    from nexus.core.config import public_services
    off = normalize_hosted_services([{"name": "X"}])[0]
    on = normalize_hosted_services([{"name": "Y", "replicable": True}])[0]
    assert off["replicable"] is False and on["replicable"] is True
    # The opt-in flag travels to peers (it gates the copy affordance there).
    assert public_services([on])[0]["replicable"] is True


def test_replicate_cookbook_requires_optin(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)

    async def _addr(uid):
        return "1.2.3.4:8001"
    monkeypatch.setattr(sg, "resolve_peer_addr", _addr)

    def _profile(replicable):
        async def _post(addr, path, body, timeout=5.0):
            return {"status": 200, "body": {
                "display_name": "Dee",
                "hosted_services": normalize_hosted_services([{
                    "name": "WebStack", "access": "free", "tags": ["stack"],
                    "readme": "run `docker compose up`", "replicable": replicable,
                }]),
            }}
        return _post

    # Provider did NOT opt in → refused, nothing written.
    monkeypatch.setattr("nexus.networking.peer_http.peer_http_post", _profile(False))
    res = asyncio.run(sg.replicate_cookbook("nexus_dee", "WebStack"))
    assert res["ok"] is False and res["error"] == "not_replicable"
    assert sg.list_cookbooks()["cookbooks"] == []

    # Provider opted in → recipe written to a local file.
    monkeypatch.setattr("nexus.networking.peer_http.peer_http_post", _profile(True))
    res = asyncio.run(sg.replicate_cookbook("nexus_dee", "WebStack"))
    assert res["ok"] and "docker compose up" in res["content"]
    listed = sg.list_cookbooks()["cookbooks"]
    assert len(listed) == 1 and listed[0]["filename"] == res["filename"]


def test_revoke_marks_grant(isolated_db):
    priv, pub = generate_keypair()
    _seed_known_consumer("nexus_c1", pub)
    gid = asyncio.run(sg.handle_service_request(_signed_request("FreeLLM", priv, pub, "nexus_c1")))["grant"]["grant_id"]
    res = asyncio.run(sg.revoke_grant(gid))
    assert res["ok"] and res["grant"]["status"] == "revoked"


def test_apply_grant_update_verifies_provider(isolated_db):
    # Local node is the CONSUMER here; a provider keypair signs the update.
    me = group_keys.get_local_group_pubkey()
    prov_priv, prov_pub = generate_keypair()

    async def _seed_held():
        async with get_session() as s:
            s.add(ServiceGrant(
                grant_id="g-held", service_name="FreeLLM", provider_pubkey=prov_pub,
                consumer_pubkey=me, provider_uuid="nexus_p", consumer_uuid="nexus_me",
                status="pending", access="permission",
            ))
            await s.commit()
    asyncio.run(_seed_held())

    grant = {"grant_id": "g-held", "provider_pubkey": prov_pub, "service_name": "FreeLLM",
             "consumer_pubkey": me, "status": "approved", "decided_at": "t"}
    good_sig = sign_statement(sg.STMT_GRANT_UPDATE, sg._grant_payload(
        "g-held", prov_pub, "FreeLLM", me, "approved", "t"), prov_priv)
    assert asyncio.run(sg.apply_grant_update({"grant": grant, "sig": good_sig}))["ok"]

    async def _status():
        async with get_session() as s:
            return (await s.get(ServiceGrant, "g-held")).status
    assert asyncio.run(_status()) == "approved"

    # A forged update (signed by someone other than the provider) is rejected.
    attacker_priv, _ = generate_keypair()
    grant2 = {**grant, "status": "revoked"}
    bad_sig = sign_statement(sg.STMT_GRANT_UPDATE, sg._grant_payload(
        "g-held", prov_pub, "FreeLLM", me, "revoked", "t"), attacker_priv)
    assert asyncio.run(sg.apply_grant_update({"grant": grant2, "sig": bad_sig}))["ok"] is False
    assert asyncio.run(_status()) == "approved"  # unchanged


def test_grant_update_replay_rejected(isolated_db):
    """SECURITY F-006: a captured, genuinely provider-signed *older* update must
    not be replayable to revert a grant to a stale status (monotonic decided_at)."""
    me = group_keys.get_local_group_pubkey()
    prov_priv, prov_pub = generate_keypair()

    async def _seed():
        async with get_session() as s:
            s.add(ServiceGrant(
                grant_id="g-rp", service_name="FreeLLM", provider_pubkey=prov_pub,
                consumer_pubkey=me, provider_uuid="nexus_p", consumer_uuid="nexus_me",
                status="pending", access="permission", decided_at=""))
            await s.commit()
    asyncio.run(_seed())

    def _update(status, decided):
        g = {"grant_id": "g-rp", "provider_pubkey": prov_pub, "service_name": "FreeLLM",
             "consumer_pubkey": me, "status": status, "decided_at": decided}
        sig = sign_statement(sg.STMT_GRANT_UPDATE, sg._grant_payload(
            "g-rp", prov_pub, "FreeLLM", me, status, decided), prov_priv)
        return asyncio.run(sg.apply_grant_update({"grant": g, "sig": sig}))

    async def _status():
        async with get_session() as s:
            return (await s.get(ServiceGrant, "g-rp")).status

    assert _update("approved", "2026-06-01T00:00:00Z")["ok"]
    assert _update("revoked", "2026-06-02T00:00:00Z")["ok"]
    # Replay the OLD approve frame — must be refused; status stays revoked.
    res = _update("approved", "2026-06-01T00:00:00Z")
    assert res["ok"] is False and res["error"] == "stale_update"
    assert asyncio.run(_status()) == "revoked"
