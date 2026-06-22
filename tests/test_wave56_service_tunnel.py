"""Wave 56 — service data-plane tunnel gating + service-usage accounting."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import LOCAL_SETTINGS, normalize_hosted_services
from nexus.runtime import service_tunnel as st
from nexus.security import group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import ServiceGrant, UsageReceipt


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
    LOCAL_SETTINGS["hosted_services"] = []
    st._streams.clear()
    st._consumer.clear()
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def _seed_grant(status, consumer_uuid="nexus_consumer", service="LlamaServe"):
    async def _go():
        async with get_session() as s:
            s.add(ServiceGrant(
                grant_id="g1", service_name=service,
                provider_pubkey=group_keys.get_local_group_pubkey(),
                consumer_pubkey="pk-consumer", provider_uuid="nexus_me",
                consumer_uuid=consumer_uuid, status=status, access="permission",
            ))
            await s.commit()
    asyncio.run(_go())


def test_provider_denies_unapproved_grant(isolated_db, monkeypatch):
    sent = []
    async def _capture(peer, frame): sent.append(frame); return True
    monkeypatch.setattr(st, "_send_to_peer", _capture)

    _seed_grant("pending")  # not approved
    asyncio.run(st._handle_open("nexus_consumer", {"tunnel_id": "t1", "grant_id": "g1"}))
    assert any(f.get("type") == "svc_close" and f.get("reason") == "denied" for f in sent)
    assert "t1" not in st._streams  # no stream opened


def test_provider_denies_wrong_consumer(isolated_db, monkeypatch):
    sent = []
    async def _capture(peer, frame): sent.append(frame); return True
    monkeypatch.setattr(st, "_send_to_peer", _capture)

    _seed_grant("approved", consumer_uuid="nexus_alice")
    # A DIFFERENT peer tries to ride Alice's grant.
    asyncio.run(st._handle_open("nexus_attacker", {"tunnel_id": "t1", "grant_id": "g1"}))
    assert any(f.get("reason") == "denied" for f in sent)
    assert "t1" not in st._streams


def test_provider_no_target_when_port_unset(isolated_db, monkeypatch):
    sent = []
    async def _capture(peer, frame): sent.append(frame); return True
    monkeypatch.setattr(st, "_send_to_peer", _capture)

    _seed_grant("approved")
    LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services(
        [{"name": "LlamaServe", "access": "permission"}]  # no local_port
    )
    asyncio.run(st._handle_open("nexus_consumer", {"tunnel_id": "t1", "grant_id": "g1"}))
    assert any(f.get("reason") == "no_target" for f in sent)


def test_custom_pump_transforms_and_default_passthrough():
    # Default forwards bytes unchanged.
    assert st._get_transform("")("to_consumer", b"hello") == b"hello"
    assert st._get_transform("default")("to_provider", b"x") == b"x"

    # A registered custom pump shapes the bytes.
    def _upper_factory():
        def _t(direction, chunk):
            return chunk.upper() if direction == "to_consumer" else chunk
        return _t
    st.register_pump("upper", _upper_factory)
    t = st._get_transform("upper")
    assert t("to_consumer", b"hello") == b"HELLO"
    assert t("to_provider", b"hello") == b"hello"

    # An unknown pump name safely falls back to the default.
    assert st._get_transform("nope")("to_consumer", b"z") == b"z"


def test_components_normalized_and_target_stripped():
    from nexus.core.config import public_services
    svc = normalize_hosted_services([{
        "name": "WebStack", "access": "permission",
        "components": [
            {"name": "postgres", "protocol": "sql", "local_port": 5432, "tags": ["DB"]},
            {"name": "redis", "local_port": 6379},
            {"local_port": 1},  # no name → dropped
        ],
    }])[0]
    assert [c["name"] for c in svc["components"]] == ["postgres", "redis"]
    assert svc["components"][0]["tags"] == ["db"]
    # Public view strips each component's host-only local target.
    pub = public_services([svc])[0]
    for c in pub["components"]:
        assert "local_host" not in c and "local_port" not in c
        assert "name" in c and "protocol" in c


def test_provider_unknown_component_denied(isolated_db, monkeypatch):
    sent = []
    async def _capture(peer, frame): sent.append(frame); return True
    monkeypatch.setattr(st, "_send_to_peer", _capture)

    _seed_grant("approved", service="WebStack")
    LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services([{
        "name": "WebStack", "access": "permission",
        "components": [{"name": "redis", "local_port": 6379}],
    }])
    asyncio.run(st._handle_open("nexus_consumer",
                {"tunnel_id": "t1", "grant_id": "g1", "component": "postgres"}))
    assert any(f.get("reason") == "no_component" for f in sent)
    assert "t1" not in st._streams


def test_global_usage_counts_service(isolated_db):
    from nexus.runtime.usage_receipts import global_usage_summary

    me = group_keys.get_local_group_pubkey()

    async def _seed():
        async with get_session() as s:
            # I served Alice (provider==me): secs + bytes
            s.add(UsageReceipt(receipt_id="r1", provider_pubkey=me, consumer_pubkey="alice",
                               kind="service", amount=120, ts="t"))
            s.add(UsageReceipt(receipt_id="r2", provider_pubkey=me, consumer_pubkey="alice",
                               kind="service_bytes", amount=4096, ts="t"))
            # I used Bob's service (consumer==me)
            s.add(UsageReceipt(receipt_id="r3", provider_pubkey="bob", consumer_pubkey=me,
                               kind="service", amount=30, ts="t"))
            await s.commit()
    asyncio.run(_seed())

    g = asyncio.run(global_usage_summary())
    assert g["service_secs_served"] == 120 and g["service_secs_used"] == 30
    assert g["service_bytes_served"] == 4096
    assert g["service_users"] == 1  # Alice
