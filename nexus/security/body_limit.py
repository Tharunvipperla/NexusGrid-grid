"""Global request-body size limit (DoS guard).

Security F-010: most ``/peer/*`` handlers are unauthenticated (their auth is
in-body: a signed statement) and read the whole body with ``await
request.json()`` before any check runs. Without a ceiling, a peer can ship a
multi-gigabyte body and exhaust memory before the handler rejects it. The
per-endpoint :func:`enforce_content_length` helpers only cover the few upload
routes that opted in; this middleware enforces one ceiling for *every* HTTP
request, including the unauthenticated peer surface.

The cap is the node's existing top upload bound (``get_max_result_bytes``, the
limit the largest legit bodies — task results, workflow/backup zips — already
respect), so nothing within the normal envelope is affected. WebSocket frames
have their own bound (``get_max_ws_frame_bytes``) and are not touched here.

Both vectors are covered: a declared ``Content-Length`` over the cap is rejected
before the body is read, and a chunked stream with no/wrong length is counted as
it arrives and aborted once it crosses the cap.
"""

from __future__ import annotations


class _BodyTooLarge(Exception):
    """Raised inside the wrapped receive when the streamed body exceeds the cap."""


class BodySizeLimitMiddleware:
    """Pure-ASGI body-size ceiling. ``max_bytes`` may be an int or a 0-arg
    callable (read at request time so a settings change takes effect live)."""

    def __init__(self, app, max_bytes):
        self.app = app
        self._max_bytes = max_bytes

    def _cap(self) -> int:
        return int(self._max_bytes() if callable(self._max_bytes) else self._max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        cap = self._cap()

        # 1) Cheap up-front check: a declared Content-Length over the cap is
        #    refused before a single body byte is read.
        for k, v in scope.get("headers") or []:
            if k == b"content-length":
                try:
                    if int(v) > cap:
                        return await self._reject(send, cap)
                except ValueError:
                    pass
                break

        # 2) Defensive count for chunked / misdeclared streams.
        total = 0
        started = False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > cap:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            # Only safe to write our own response if the app hasn't started one.
            if not started:
                await self._reject(send, cap)

    @staticmethod
    async def _reject(send, cap: int) -> None:
        body = b'{"detail":"Request body too large."}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


__all__ = ["BodySizeLimitMiddleware"]
