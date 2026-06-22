"""Wave 18 — integration: lazy symkey mint, second joiner gets same key."""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.group_peer import router as peer_router
from nexus.api.groups import router as groups_router
from nexus.security import group_keys, tokens
from nexus.security.auth import verify_local_auth
from nexus.security.group_ecies import (
    derive_x25519_pubkey_hex,
    ecies_open,
)
from nexus.security.group_grant import generate_keypair
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
    app.include_router(peer_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def test_first_joiner_triggers_lazy_mint(client):
    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()

    # Joiner mints fresh ed25519 + advertises X25519 pubkey.
    joiner_priv, joiner_pub = generate_keypair()
    joiner_x25519_pub = derive_x25519_pubkey_hex(joiner_priv)

    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_x25519_pub": joiner_x25519_pub,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["symkey_envelope_b64"], "founder should seal symkey on first join"
    # Joiner can open the envelope with their X25519 key (derived from
    # their ed25519 privkey).
    envelope = base64.b64decode(body["symkey_envelope_b64"].encode("ascii"))
    symkey = ecies_open(envelope, joiner_priv)
    assert len(symkey) == 32


def test_second_joiner_gets_same_symkey(client):
    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()

    def _join_once():
        priv, pub = generate_keypair()
        x = derive_x25519_pubkey_hex(priv)
        res = client.post(
            "/peer/group/join_request",
            json={
                "invite_token": invite["token"],
                "joiner_pubkey": pub,
                "joiner_x25519_pub": x,
            },
        )
        assert res.status_code == 200, res.text
        env = base64.b64decode(res.json()["symkey_envelope_b64"].encode("ascii"))
        return ecies_open(env, priv)

    sym_a = _join_once()
    sym_b = _join_once()
    assert sym_a == sym_b, "all joiners must receive the same group symkey"


def test_legacy_joiner_without_x25519_pub_gets_no_envelope(client):
    """A joiner that omits joiner_x25519_pub (legacy client) still joins
    successfully but gets an empty envelope. Subsequent waves will
    require the field; this preserves backwards compatibility for now."""
    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    _, joiner_pub = generate_keypair()
    res = client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            # joiner_x25519_pub intentionally omitted
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["symkey_envelope_b64"] == ""


def test_founder_stores_self_sealed_copy(client):
    """After the first joiner triggers the mint, the founder's own
    Group.group_symkey_enc row should contain a self-sealed envelope
    they can open with their own ed25519 key."""
    from nexus.storage import get_session
    from nexus.storage.models import Group
    from nexus.security.group_keys import get_local_group_privkey
    from sqlalchemy import select as _select

    group = client.post("/local/groups", json={"name": "g"}).json()
    invite = client.post(
        f"/local/groups/{group['id']}/invites", json={"slot_cap": 5}
    ).json()
    joiner_priv, joiner_pub = generate_keypair()
    client.post(
        "/peer/group/join_request",
        json={
            "invite_token": invite["token"],
            "joiner_pubkey": joiner_pub,
            "joiner_x25519_pub": derive_x25519_pubkey_hex(joiner_priv),
        },
    )

    async def _check():
        async with get_session() as session:
            g = (await session.execute(
                _select(Group).where(Group.id == group["id"])
            )).scalar_one()
            assert g.group_symkey_enc, "founder must store a self-sealed symkey"
            sym = ecies_open(bytes(g.group_symkey_enc), get_local_group_privkey())
            assert len(sym) == 32

    asyncio.run(_check())
