"""Cert-fingerprint pinning in peer_http_post (Wave 4 Step 6 / item 2.9)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from nexus.networking import peer_http


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def reset_cache():
    peer_http._reset_fingerprint_cache()
    yield
    peer_http._reset_fingerprint_cache()


def test_mismatched_fingerprint_returns_495():
    with patch.object(
        peer_http, "_lookup_expected_fingerprint", AsyncMock(return_value="aa" * 32)
    ), patch.object(
        peer_http, "_fetch_cached_fingerprint", AsyncMock(return_value="bb" * 32)
    ):
        result = _run(peer_http.peer_http_post("1.2.3.4:8000", "/peer/x", {}))
    assert result["status"] == 495
    assert "mismatch" in result["body"]["error"].lower()


def test_matched_fingerprint_proceeds_to_request():
    """When stored fp == served fp, we go ahead with the HTTPS request."""
    fp = "cc" * 32

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            return _FakeResponse()

    with patch.object(
        peer_http, "_lookup_expected_fingerprint", AsyncMock(return_value=fp)
    ), patch.object(
        peer_http, "_fetch_cached_fingerprint", AsyncMock(return_value=fp)
    ), patch.object(peer_http.httpx, "AsyncClient", _FakeClient):
        result = _run(peer_http.peer_http_post("1.2.3.4:8000", "/peer/x", {}))

    assert result == {"status": 200, "body": {"status": "ok"}}


def test_no_stored_fingerprint_skips_pinning():
    """First contact: no fingerprint stored, the request goes through."""

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            return _FakeResponse()

    fetch = AsyncMock()
    with patch.object(
        peer_http, "_lookup_expected_fingerprint", AsyncMock(return_value=None)
    ), patch.object(
        peer_http, "_fetch_cached_fingerprint", fetch
    ), patch.object(peer_http.httpx, "AsyncClient", _FakeClient):
        result = _run(peer_http.peer_http_post("1.2.3.4:8000", "/peer/x", {}))

    assert result["status"] == 200
    fetch.assert_not_awaited()


def test_split_host_port_default_port():
    assert peer_http._split_host_port("1.2.3.4", 8000) == ("1.2.3.4", 8000)


def test_split_host_port_with_port():
    assert peer_http._split_host_port("1.2.3.4:9001", 8000) == ("1.2.3.4", 9001)


def test_fingerprint_cache_ttl():
    """Repeated lookups within TTL hit the cache (one upstream call)."""
    import time

    calls = {"n": 0}

    def _fake_fetch(host, port, timeout=3.0):
        calls["n"] += 1
        return "dd" * 32

    with patch("nexus.security.tls.fetch_peer_fingerprint", _fake_fetch):
        fp1 = _run(peer_http._fetch_cached_fingerprint("1.2.3.4", 8000))
        fp2 = _run(peer_http._fetch_cached_fingerprint("1.2.3.4", 8000))
    assert fp1 == fp2 == "dd" * 32
    assert calls["n"] == 1
