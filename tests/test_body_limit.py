"""Security F-010 — global request-body size ceiling (DoS guard)."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from nexus.security.body_limit import BodySizeLimitMiddleware


def _app(cap: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=cap)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"n": len(body)}

    return app


def test_declared_oversize_rejected():
    client = TestClient(_app(100))
    r = client.post("/echo", content=b"x" * 500)  # Content-Length = 500 > 100
    assert r.status_code == 413
    assert "too large" in r.json()["detail"].lower()


def test_within_cap_passes():
    client = TestClient(_app(1000))
    r = client.post("/echo", content=b"x" * 200)
    assert r.status_code == 200 and r.json()["n"] == 200


def test_callable_cap_read_live():
    cap = {"v": 50}
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=lambda: cap["v"])

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len((await request.body()))}

    client = TestClient(app)
    assert client.post("/echo", content=b"x" * 80).status_code == 413
    cap["v"] = 1000
    assert client.post("/echo", content=b"x" * 80).status_code == 200


def test_chunked_stream_without_length_aborted():
    """A body streamed in chunks with no Content-Length must still be capped
    once the running total crosses the cap (the chunked-DoS vector)."""
    captured = {}

    async def fake_app(scope, receive, send):
        # Drain the body the way a real handler would.
        while True:
            msg = await receive()
            if msg["type"] == "http.request" and not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimitMiddleware(fake_app, max_bytes=100)

    # Three 60-byte chunks (=180 > 100), NO content-length header.
    chunks = [
        {"type": "http.request", "body": b"a" * 60, "more_body": True},
        {"type": "http.request", "body": b"b" * 60, "more_body": True},
        {"type": "http.request", "body": b"c" * 60, "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]

    scope = {"type": "http", "headers": []}
    asyncio.run(mw(scope, receive, send))
    assert captured["status"] == 413  # aborted before the handler answered 200


def test_non_http_scope_passthrough():
    """WebSocket / lifespan scopes are passed through untouched."""
    seen = {}

    async def fake_app(scope, receive, send):
        seen["type"] = scope["type"]

    mw = BodySizeLimitMiddleware(fake_app, max_bytes=10)
    asyncio.run(mw({"type": "websocket"}, None, None))
    assert seen["type"] == "websocket"
