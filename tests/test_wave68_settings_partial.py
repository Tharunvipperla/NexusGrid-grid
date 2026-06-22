"""Wave 68 — JSON partial settings update for the v3 UI.

``POST /local/settings_partial`` changes only the keys present in the
body; everything else is re-submitted with its current value through the
classic ``local_update_settings`` handler, so clamping and side effects
stay identical for both UIs. The ``"***"`` Drive-key mask never
overwrites the stored key.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.local import router as local_router
from nexus.core import LOCAL_SETTINGS
from nexus.security import tokens
from nexus.security.auth import verify_local_auth
from nexus.storage import database


SEED = {
    "mode": "user",
    "max_ram_pct": 80,
    "gdrive_key": "real-secret-key",
    "node_online": True,
    "sharing_mode": "shared",
    "user_display_name": "node-test",
    "allowed_images": ["python:3.11-slim"],
    "node_tags": ["gpu", "fast"],
    "security_profile": "maximum",
    "max_gpu_pct": 80,
    "storage_max_total_gb": 5,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    db_path = tmp_path / "test.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))

    saved = {k: LOCAL_SETTINGS.get(k) for k in SEED}
    LOCAL_SETTINGS.update(SEED)

    app = FastAPI()
    app.include_router(local_router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c

    for k, v in saved.items():
        if v is None:
            LOCAL_SETTINGS.pop(k, None)
        else:
            LOCAL_SETTINGS[k] = v

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()


def test_partial_changes_only_provided_keys(client):
    r = client.post("/local/settings_partial", json={"max_ram": 55})
    assert r.status_code == 200
    assert LOCAL_SETTINGS["max_ram_pct"] == 55
    # untouched fields keep their values
    assert LOCAL_SETTINGS["user_display_name"] == "node-test"
    assert LOCAL_SETTINGS["gdrive_key"] == "real-secret-key"
    assert LOCAL_SETTINGS["sharing_mode"] == "shared"
    assert LOCAL_SETTINGS["node_tags"] == ["gpu", "fast"]


def test_masked_gdrive_key_never_overwrites(client):
    r = client.post(
        "/local/settings_partial",
        json={"gdrive_key": "***", "max_ram": 60},
    )
    assert r.status_code == 200
    assert LOCAL_SETTINGS["gdrive_key"] == "real-secret-key"
    assert LOCAL_SETTINGS["max_ram_pct"] == 60


def test_explicit_gdrive_key_updates(client):
    r = client.post("/local/settings_partial", json={"gdrive_key": "new-key"})
    assert r.status_code == 200
    assert LOCAL_SETTINGS["gdrive_key"] == "new-key"


def test_unknown_field_rejected(client):
    r = client.post("/local/settings_partial", json={"not_a_setting": 1})
    assert r.status_code == 400
    assert "not_a_setting" in r.json()["detail"]


def test_csv_fields_accept_lists_and_strings(client):
    r = client.post(
        "/local/settings_partial",
        json={"allowed_images": ["python:3.12", "node:22"]},
    )
    assert r.status_code == 200
    assert LOCAL_SETTINGS["allowed_images"] == ["python:3.12", "node:22"]
    r = client.post(
        "/local/settings_partial", json={"node_tags": "a, b , c"}
    )
    assert r.status_code == 200
    assert LOCAL_SETTINGS["node_tags"] == ["a", "b", "c"]


def test_auto_rescue_fields_round_trip(client):
    # Regression: the auto-rescue settings must be in the partial allow-list
    # (toggling them off in the UI was 400ing as "unknown settings field").
    r = client.post("/local/settings_partial", json={"fs_auto_rescue": False})
    assert r.status_code == 200
    assert LOCAL_SETTINGS["fs_auto_rescue"] is False

    r = client.post("/local/settings_partial", json={
        "fs_auto_rescue": True,
        "fs_auto_rescue_trigger": "days",
        "fs_auto_rescue_days": 999,                       # clamped to 30
        "fs_auto_rescue_dir": "D:/rescued",
        "fs_auto_rescue_rclone_targets": ["g:nx", "w:bk"],
    })
    assert r.status_code == 200
    assert LOCAL_SETTINGS["fs_auto_rescue"] is True
    assert LOCAL_SETTINGS["fs_auto_rescue_trigger"] == "days"
    assert LOCAL_SETTINGS["fs_auto_rescue_days"] == 30
    assert LOCAL_SETTINGS["fs_auto_rescue_dir"] == "D:/rescued"
    assert LOCAL_SETTINGS["fs_auto_rescue_rclone_targets"] == ["g:nx", "w:bk"]


def test_clamps_from_classic_handler_apply(client):
    r = client.post("/local/settings_partial", json={"max_gpu_pct": 999})
    assert r.status_code == 200
    assert LOCAL_SETTINGS["max_gpu_pct"] == 95  # clamped by the form handler
    r = client.post(
        "/local/settings_partial", json={"security_profile": "bogus"}
    )
    assert r.status_code == 200
    assert LOCAL_SETTINGS["security_profile"] == "maximum"
