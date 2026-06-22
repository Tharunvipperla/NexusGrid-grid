"""C4 — node-local secrets vault: at-rest crypto, CRUD, ref resolution, API."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import undefer

from nexus.runtime import secrets_vault as vault
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import Secret, database, get_session


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
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


# ---- name validation -------------------------------------------------------

@pytest.mark.parametrize("name,ok", [
    ("OPENAI_API_KEY", True), ("A", True), ("_X1", True),
    ("lowercase", False), ("1LEADING", False), ("has-dash", False),
    ("has space", False), ("", False),
])
def test_valid_name(name, ok):
    assert vault.valid_name(name) is ok


# ---- at-rest encryption ----------------------------------------------------

def test_value_encrypted_at_rest(isolated_db):
    asyncio.run(vault.set_secret("OPENAI_API_KEY", "sk-supersecret-123"))

    async def _raw():
        async with get_session() as db:
            row = (
                await db.execute(
                    select(Secret).options(undefer(Secret.encrypted_blob))
                    .filter(Secret.name == "OPENAI_API_KEY")
                )
            ).scalar_one()
            return bytes(row.encrypted_blob)

    blob = asyncio.run(_raw())
    assert b"sk-supersecret-123" not in blob          # not stored in clear
    assert asyncio.run(vault.get_value("OPENAI_API_KEY")) == "sk-supersecret-123"


def test_set_is_idempotent_replace(isolated_db):
    asyncio.run(vault.set_secret("TOK", "v1"))
    asyncio.run(vault.set_secret("TOK", "v2"))
    assert asyncio.run(vault.get_value("TOK")) == "v2"
    assert len(asyncio.run(vault.list_secrets())) == 1


def test_bad_name_rejected(isolated_db):
    with pytest.raises(vault.SecretError):
        asyncio.run(vault.set_secret("bad-name", "x"))


# ---- list never leaks values ----------------------------------------------

def test_list_omits_values(isolated_db):
    asyncio.run(vault.set_secret("A", "secretA", description="my key"))
    rows = asyncio.run(vault.list_secrets())
    assert rows[0]["name"] == "A"
    assert rows[0]["description"] == "my key"
    # No field anywhere carries the plaintext.
    assert "secretA" not in repr(rows)
    assert "value" not in rows[0]


# ---- delete ----------------------------------------------------------------

def test_delete(isolated_db):
    asyncio.run(vault.set_secret("A", "x"))
    assert asyncio.run(vault.delete_secret("A")) is True
    assert asyncio.run(vault.delete_secret("A")) is False
    assert asyncio.run(vault.get_value("A")) is None


# ---- reference resolution --------------------------------------------------

def test_resolve_refs_list_and_dict(isolated_db):
    asyncio.run(vault.set_secret("API_KEY", "sk-9"))

    out = asyncio.run(vault.resolve_refs(["API=secret://API_KEY", "MODE=fast"]))
    assert out == ["API=sk-9", "MODE=fast"]

    out_d = asyncio.run(vault.resolve_refs({"API": "secret://API_KEY", "M": "x"}))
    assert out_d == {"API": "sk-9", "M": "x"}


def test_resolve_unknown_ref_raises(isolated_db):
    with pytest.raises(vault.SecretError):
        asyncio.run(vault.resolve_refs(["A=secret://NOPE"]))


def test_resolve_passthrough_without_refs(isolated_db):
    env = ["A=1", "B=2"]
    assert asyncio.run(vault.resolve_refs(env)) == env


def test_get_value_bumps_last_used(isolated_db):
    asyncio.run(vault.set_secret("A", "x"))
    assert asyncio.run(vault.list_secrets())[0]["last_used_at"] == ""
    asyncio.run(vault.get_value("A"))
    assert asyncio.run(vault.list_secrets())[0]["last_used_at"] != ""


# ---- HTTP API --------------------------------------------------------------

@pytest.fixture
def client(isolated_db):
    from nexus.api.local import router as local_router

    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def test_api_set_list_delete(client):
    r = client.post("/local/secrets", json={"name": "DB_PASS", "value": "hunter2", "description": "db"})
    assert r.status_code == 200

    r = client.get("/local/secrets")
    assert r.status_code == 200
    body = r.json()["secrets"]
    assert body and body[0]["name"] == "DB_PASS"
    # The value must never appear in the API surface.
    assert "hunter2" not in r.text

    r = client.delete("/local/secrets/DB_PASS")
    assert r.status_code == 200
    assert client.get("/local/secrets").json()["secrets"] == []
    assert client.delete("/local/secrets/DB_PASS").status_code == 404


def test_api_rejects_bad_name(client):
    r = client.post("/local/secrets", json={"name": "bad name", "value": "x"})
    assert r.status_code == 400


def test_api_requires_value(client):
    r = client.post("/local/secrets", json={"name": "A"})
    assert r.status_code == 400
