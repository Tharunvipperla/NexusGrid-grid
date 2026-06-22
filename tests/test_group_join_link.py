"""Wave 17 — join-link encode/decode + create-group relay binding."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.groups import router as groups_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_join_link import (
    SCHEME,
    encode_join_link,
    parse_join_link,
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


# ---- encode / parse round-trip ------------------------------------------


def test_round_trip_with_relays_and_address():
    link = encode_join_link(
        relay_urls=["https://relay-a", "https://relay-b"],
        admin_address="192.168.1.42:8443",
        invite_token="t" * 32,
        group_id="g" * 32,
        admin_node_id="node-uuid-xyz",
        grid_key="grid-secret-xyz",
    )
    assert link.startswith(SCHEME)
    parsed = parse_join_link(link)
    assert parsed.relay_urls == ("https://relay-a", "https://relay-b")
    assert parsed.admin_address == "192.168.1.42:8443"
    assert parsed.admin_node_id == "node-uuid-xyz"
    assert parsed.grid_key == "grid-secret-xyz"
    assert parsed.invite_token == "t" * 32
    assert parsed.group_id == "g" * 32
    assert parsed.version == 1


def test_round_trip_without_admin_node_id_defaults_blank():
    link = encode_join_link(
        relay_urls=[],
        admin_address="host:1",
        invite_token="tok",
        group_id="gid",
    )
    parsed = parse_join_link(link)
    assert parsed.admin_node_id == ""
    assert parsed.grid_key == ""


def test_empty_relay_list_is_allowed():
    link = encode_join_link(
        relay_urls=[],
        admin_address="host:9000",
        invite_token="tok",
        group_id="gid",
    )
    parsed = parse_join_link(link)
    assert parsed.relay_urls == ()
    assert parsed.admin_address == "host:9000"


def test_empty_strings_in_relay_list_are_stripped():
    link = encode_join_link(
        relay_urls=["", "https://relay", ""],
        admin_address="",
        invite_token="tok",
        group_id="gid",
    )
    parsed = parse_join_link(link)
    assert parsed.relay_urls == ("https://relay",)


# ---- encoder validation -------------------------------------------------


def test_encoder_rejects_empty_token():
    with pytest.raises(ValueError):
        encode_join_link(
            relay_urls=[],
            admin_address="host:1",
            invite_token="",
            group_id="g",
        )


def test_encoder_rejects_empty_group_id():
    with pytest.raises(ValueError):
        encode_join_link(
            relay_urls=[],
            admin_address="host:1",
            invite_token="t",
            group_id="",
        )


# ---- parser validation --------------------------------------------------


def test_parser_rejects_empty_input():
    with pytest.raises(ValueError):
        parse_join_link("")


def test_parser_rejects_wrong_scheme():
    with pytest.raises(ValueError):
        parse_join_link("https://example/path")


def test_parser_rejects_payload_without_token():
    import base64
    import json
    bad = SCHEME + base64.urlsafe_b64encode(
        json.dumps({"g": "x", "v": 1}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    with pytest.raises(ValueError):
        parse_join_link(bad)


def test_parser_rejects_payload_without_group_id():
    import base64
    import json
    bad = SCHEME + base64.urlsafe_b64encode(
        json.dumps({"t": "x", "v": 1}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    with pytest.raises(ValueError):
        parse_join_link(bad)


def test_parser_rejects_garbage_base64():
    with pytest.raises(ValueError):
        parse_join_link(SCHEME + "@@@@@")


# ---- API integration ----------------------------------------------------


def test_create_group_inserts_relay_bindings(client):
    res = client.post(
        "/local/groups",
        json={
            "name": "g1",
            "privacy_mode": "open",
            "relay_urls": ["https://relay-a", "https://relay-b"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["relay_urls"] == ["https://relay-a", "https://relay-b"]

    detail = client.get(f"/local/groups/{body['id']}").json()
    urls = sorted(r["relay_url"] for r in detail["relays"])
    assert urls == ["https://relay-a", "https://relay-b"]


def test_create_group_falls_back_to_configured_relay(client, monkeypatch):
    from nexus.core import LOCAL_SETTINGS
    monkeypatch.setitem(LOCAL_SETTINGS, "relay_server_url", "https://fallback")
    res = client.post("/local/groups", json={"name": "g1", "privacy_mode": "open"})
    assert res.status_code == 200
    assert res.json()["relay_urls"] == ["https://fallback"]


def test_create_group_with_no_relay_urls_and_no_setting(client, monkeypatch):
    from nexus.core import LOCAL_SETTINGS
    monkeypatch.setitem(LOCAL_SETTINGS, "relay_server_url", "")
    res = client.post("/local/groups", json={"name": "g1", "privacy_mode": "open"})
    assert res.status_code == 200
    assert res.json()["relay_urls"] == []


def test_build_join_link_endpoint(client):
    mk = client.post(
        "/local/groups",
        json={"name": "g1", "privacy_mode": "open", "relay_urls": ["https://r"]},
    ).json()
    inv = client.post(
        f"/local/groups/{mk['id']}/invites", json={"slot_cap": 1}
    ).json()
    res = client.post(
        f"/local/groups/{mk['id']}/join_link",
        json={"invite_token": inv["token"]},
    )
    assert res.status_code == 200
    body = res.json()
    parsed = parse_join_link(body["join_link"])
    assert parsed.invite_token == inv["token"]
    assert parsed.group_id == mk["id"]
    assert parsed.relay_urls == ("https://r",)
    # Wave 29: founder's node UUID is included so a NAT'd admin can be
    # reached over the relay.
    assert body["admin_node_id"]
    assert parsed.admin_node_id == body["admin_node_id"]


def test_parse_join_link_endpoint(client):
    link = encode_join_link(
        relay_urls=["https://r1"],
        admin_address="a:1",
        invite_token="tok",
        group_id="gid",
        admin_node_id="node-abc",
        grid_key="grid-abc",
    )
    res = client.post(
        "/local/groups/parse_join_link", json={"join_link": link}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["invite_token"] == "tok"
    assert body["group_id"] == "gid"
    assert body["admin_address"] == "a:1"
    assert body["admin_node_id"] == "node-abc"
    assert body["grid_key"] == "grid-abc"
    assert body["relay_urls"] == ["https://r1"]


def test_parse_join_link_endpoint_rejects_bad_input(client):
    res = client.post(
        "/local/groups/parse_join_link", json={"join_link": "not a link"}
    )
    assert res.status_code == 400
