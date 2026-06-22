"""Wave 6.5 — local API endpoints for cloud credentials + cloud eviction."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.core import STATE
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.security.cred_crypto import (
    EVICTION_NONCE_BYTES,
    unwrap_credential_blob,
    unwrap_from_transit,
)
from nexus.storage import database
from nexus.storage.cloud import PROVIDERS
from nexus.storage.cloud.base import CloudProvider, ThrottleAcquire


GDRIVE_PAYLOAD = {
    "type": "service_account",
    "client_email": "sa@proj.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
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


@pytest.fixture
def client(isolated_db, monkeypatch):
    captured: list[tuple[str, dict]] = []

    async def _fake_send(peer_id: str, frame: dict) -> bool:
        captured.append((peer_id, frame))
        return True

    monkeypatch.setattr(
        "nexus.networking.tunnel._send_to_peer", _fake_send
    )

    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        c._captured = captured  # type: ignore[attr-defined]
        yield c


# ---------------------------------------------------------------------------
# /cloud_credentials
# ---------------------------------------------------------------------------

def test_create_then_list_then_delete_gdrive_credential(client):
    res = client.post(
        "/local/foreign_storage/cloud_credentials",
        json={
            "provider": "gdrive",
            "label": "personal-drive",
            "credential_json": json.dumps(GDRIVE_PAYLOAD),
            "default_folder": "folder-abc",
        },
    )
    assert res.status_code == 200, res.text
    cred_id = res.json()["id"]

    res = client.get("/local/foreign_storage/cloud_credentials")
    assert res.status_code == 200
    creds = res.json()["credentials"]
    assert len(creds) == 1
    row = creds[0]
    assert row["id"] == cred_id
    assert row["provider"] == "gdrive"
    assert row["label"] == "personal-drive"
    assert row["default_folder"] == "folder-abc"
    # Never leak the encrypted blob.
    assert "encrypted_blob" not in row
    assert "credential_json" not in row

    res = client.delete(
        f"/local/foreign_storage/cloud_credentials/{cred_id}"
    )
    assert res.status_code == 200

    res = client.get("/local/foreign_storage/cloud_credentials")
    assert res.json()["credentials"] == []


def test_create_rejects_unknown_provider(client):
    res = client.post(
        "/local/foreign_storage/cloud_credentials",
        json={"provider": "nope", "credential_json": "{}"},
    )
    assert res.status_code == 400


def test_create_rejects_invalid_gdrive_json(client):
    res = client.post(
        "/local/foreign_storage/cloud_credentials",
        json={"provider": "gdrive", "credential_json": "not json"},
    )
    assert res.status_code == 400


def test_delete_unknown_id_returns_404(client):
    res = client.delete(
        "/local/foreign_storage/cloud_credentials/missing-id"
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# /evict_to_cloud
# ---------------------------------------------------------------------------

class _NoopProvider(CloudProvider):
    name = "noop-test"

    @classmethod
    def from_credential_json(cls, raw: bytes) -> "_NoopProvider":
        return cls()

    async def upload_stream(
        self, dest, object_name, chunks, total_bytes, throttle_acquire,
    ) -> str:
        return "noop"


@pytest.fixture
def noop_provider():
    PROVIDERS["noop-test"] = _NoopProvider
    yield
    PROVIDERS.pop("noop-test", None)


def test_evict_to_cloud_sends_transit_wrapped_creds(
    client, noop_provider
):
    """The on-the-wire frame must carry creds wrapped with the host's
    signing_key, recoverable host-side via :func:`unwrap_from_transit`."""
    from nexus.storage import (
        ForeignStorageDeposit,
        Peer,
        get_session,
    )

    HOST = "10.0.0.5:9000"
    HOST_KEY = "host-signing-secret-xyz"
    DEPOSIT = "dep-evict-1"
    creds_plain = json.dumps(GDRIVE_PAYLOAD).encode()

    async def _seed():
        async with get_session() as db:
            db.add(Peer(ip=HOST, status="trusted", signing_key=HOST_KEY))
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEPOSIT,
                    role="depositor",
                    depositor_uuid="self",
                    host_uuid=HOST,
                    status="stored",
                    total_bytes=10,
                    chunk_count=1,
                    transport="stream",
                )
            )
            await db.commit()

    asyncio.run(_seed())

    # Seed a credential.
    res = client.post(
        "/local/foreign_storage/cloud_credentials",
        json={
            "provider": "noop-test",
            "credential_json": creds_plain.decode(),
            "default_folder": "default-folder-xyz",
        },
    )
    assert res.status_code == 200, res.text
    cred_id = res.json()["id"]

    # Trigger evict_to_cloud.
    res = client.post(
        f"/local/foreign_storage/evict_to_cloud/{DEPOSIT}",
        json={"credential_id": cred_id},
    )
    assert res.status_code == 200, res.text

    sent = client._captured  # type: ignore[attr-defined]
    assert len(sent) == 1
    peer_id, frame = sent[0]
    assert peer_id == HOST
    assert frame["type"] == "storage_eviction_response"
    assert frame["action"] == "cloud"
    assert frame["cloud_provider"] == "noop-test"
    assert frame["cloud_dest"] == "default-folder-xyz"

    nonce = base64.b64decode(frame["cloud_eviction_nonce_b64"])
    assert len(nonce) == EVICTION_NONCE_BYTES
    transit = base64.b64decode(frame["cloud_credential_blob_b64"])
    recovered = unwrap_from_transit(HOST_KEY, nonce, transit)
    assert recovered == creds_plain


def test_evict_to_cloud_rejects_when_no_signing_key(client, noop_provider):
    from nexus.storage import (
        ForeignStorageDeposit,
        Peer,
        get_session,
    )

    HOST = "10.0.0.6:9000"
    DEPOSIT = "dep-no-key"

    async def _seed():
        async with get_session() as db:
            db.add(Peer(ip=HOST, status="trusted", signing_key=None))
            db.add(
                ForeignStorageDeposit(
                    deposit_id=DEPOSIT,
                    role="depositor",
                    depositor_uuid="self",
                    host_uuid=HOST,
                    status="stored",
                    total_bytes=10,
                    chunk_count=1,
                    transport="stream",
                )
            )
            await db.commit()

    asyncio.run(_seed())

    res = client.post(
        "/local/foreign_storage/cloud_credentials",
        json={
            "provider": "noop-test",
            "credential_json": "{}",
        },
    )
    cred_id = res.json()["id"]

    res = client.post(
        f"/local/foreign_storage/evict_to_cloud/{DEPOSIT}",
        json={"credential_id": cred_id},
    )
    assert res.status_code == 409
    assert "signing_key" in res.json()["detail"]
