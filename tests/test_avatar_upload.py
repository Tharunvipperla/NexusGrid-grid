"""Avatar upload validation tests (Wave 4 Step 1 / roadmap item 2.10).

Verifies the magic-byte and size guards in ``nexus.ui.avatar.upload_avatar``.
Real uploads write into ``cache_dir(DEFAULT_HTTP_PORT)``; the fixture
patches that to a tmpdir so tests don't pollute the dev workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus import ui
from nexus.security.auth import verify_local_auth
from nexus.ui import avatar as avatar_mod


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_JPEG_HEADER = b"\xff\xd8\xff\xe0"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(avatar_mod, "cache_dir", lambda _port: Path(tmp_path))

    app = FastAPI()
    app.include_router(ui.avatar.router)
    app.dependency_overrides[verify_local_auth] = lambda: None
    with TestClient(app) as c:
        yield c


def test_png_upload_accepted(client):
    body = _PNG_HEADER + b"\x00" * 32
    res = client.post(
        "/local/upload_avatar",
        files={"file": ("logo.png", body, "image/png")},
    )
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_jpeg_upload_accepted(client):
    body = _JPEG_HEADER + b"\x00" * 32
    res = client.post(
        "/local/upload_avatar",
        files={"file": ("logo.jpg", body, "image/jpeg")},
    )
    assert res.status_code == 200


def test_renamed_text_file_rejected(client):
    body = b"<html><body>not an image</body></html>"
    res = client.post(
        "/local/upload_avatar",
        files={"file": ("logo.png", body, "image/png")},
    )
    assert res.status_code == 400
    assert "PNG or JPEG" in res.json()["detail"]


def test_oversized_upload_rejected(client):
    body = _PNG_HEADER + b"\x00" * (3 * 1024 * 1024)
    res = client.post(
        "/local/upload_avatar",
        files={"file": ("big.png", body, "image/png")},
    )
    assert res.status_code == 400
    assert "too large" in res.json()["detail"]


def test_empty_upload_rejected(client):
    res = client.post(
        "/local/upload_avatar",
        files={"file": ("empty.png", b"", "image/png")},
    )
    assert res.status_code == 400
