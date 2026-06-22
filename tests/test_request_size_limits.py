"""Request size limit tests (Wave 4 Step 2 / roadmap item 2.7).

Covers the helpers in ``nexus.security.limits``: header-based pre-check
and post-read fallback for chunked uploads. Endpoint-level wiring is
exercised via a tiny FastAPI app that mounts a single handler.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient

from nexus.core import LOCAL_SETTINGS
from nexus.security.limits import (
    enforce_actual_size,
    enforce_content_length,
    get_max_result_bytes,
    get_max_ws_frame_bytes,
)


def test_get_max_result_bytes_default():
    LOCAL_SETTINGS.pop("max_result_bytes", None)
    assert get_max_result_bytes() == 100 * 1024 * 1024


def test_get_max_result_bytes_floor():
    LOCAL_SETTINGS["max_result_bytes"] = 1024  # below 1 MB floor
    try:
        assert get_max_result_bytes() == 1024 * 1024
    finally:
        LOCAL_SETTINGS.pop("max_result_bytes", None)


def test_get_max_ws_frame_bytes_default():
    LOCAL_SETTINGS.pop("max_ws_frame_bytes", None)
    assert get_max_ws_frame_bytes() == 4 * 1024 * 1024


def test_enforce_actual_size_under_limit_passes():
    enforce_actual_size(b"x" * 100, 1024, label="t")


def test_enforce_actual_size_over_limit_raises():
    with pytest.raises(HTTPException) as exc:
        enforce_actual_size(b"x" * 2048, 1024, label="t")
    assert exc.value.status_code == 413
    assert "too large" in exc.value.detail


@pytest.fixture
def app_with_guard():
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request, file: UploadFile = File(...)):
        enforce_content_length(request, 1024, label="payload")
        body = await file.read()
        enforce_actual_size(body, 1024, label="payload")
        return {"size": len(body)}

    return app


def test_endpoint_rejects_oversized_via_content_length(app_with_guard):
    client = TestClient(app_with_guard)
    res = client.post(
        "/echo", files={"file": ("big.bin", b"\x00" * 4096, "application/octet-stream")}
    )
    assert res.status_code == 413
    assert "exceeds limit" in res.json()["detail"]


def test_endpoint_accepts_within_limit(app_with_guard):
    client = TestClient(app_with_guard)
    res = client.post(
        "/echo", files={"file": ("ok.bin", b"\x00" * 64, "application/octet-stream")}
    )
    assert res.status_code == 200
    assert res.json()["size"] == 64


def test_invalid_content_length_header():
    """Non-numeric Content-Length is a 400, not silently ignored."""
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "headers": [(b"content-length", b"not-a-number")],
    }
    req = StarletteRequest(scope)
    with pytest.raises(HTTPException) as exc:
        enforce_content_length(req, 1024)
    assert exc.value.status_code == 400


def test_normalize_clamps_settings():
    """Settings normalization clamps absurd values rather than rejecting."""
    from nexus.core.config import normalize_local_settings

    norm = normalize_local_settings({"max_result_bytes": -1, "max_ws_frame_bytes": -1})
    assert norm["max_result_bytes"] >= 1 * 1024 * 1024
    assert norm["max_ws_frame_bytes"] >= 64 * 1024

    norm = normalize_local_settings(
        {"max_result_bytes": 10 * 1024**4, "max_ws_frame_bytes": 10 * 1024**4}
    )
    assert norm["max_result_bytes"] <= 2 * 1024**3
    assert norm["max_ws_frame_bytes"] <= 64 * 1024 * 1024
