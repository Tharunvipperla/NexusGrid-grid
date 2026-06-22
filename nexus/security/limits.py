"""Request size guards for upload-heavy endpoints.

Endpoints that buffer the full body (``UploadFile.read()`` or
``request.body()``) are vulnerable to memory-exhaustion DoS — a peer can
ship a multi-gigabyte payload before any application logic runs. The two
helpers here let route handlers reject oversized requests cheaply:

* :func:`enforce_content_length` — synchronous header check. Trusts the
  declared ``Content-Length`` and refuses a body declared larger than the
  configured cap. This is the cheap, first-line filter — it does not
  buffer anything.

* :func:`enforce_actual_size` — post-read check. Some clients (especially
  ``Transfer-Encoding: chunked``) omit ``Content-Length`` entirely, so the
  cap must also be enforced after the bytes are pulled. Call this on the
  freshly-read ``bytes`` to catch misdeclared streams.

Both helpers raise ``HTTPException(413)`` with a stable detail string so
callers and audit downstream can match on it.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from nexus.core import LOCAL_SETTINGS


_DEFAULT_RESULT_BYTES = 100 * 1024 * 1024   # 100 MB
_DEFAULT_WS_FRAME_BYTES = 4 * 1024 * 1024   # 4 MB


def get_max_result_bytes() -> int:
    """Cap for HTTP upload bodies (task results, workflow zips)."""
    raw = LOCAL_SETTINGS.get("max_result_bytes", _DEFAULT_RESULT_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RESULT_BYTES
    return max(1024 * 1024, value)  # never lower than 1 MB


def get_max_ws_frame_bytes() -> int:
    """Cap for individual WebSocket frames passed to uvicorn."""
    raw = LOCAL_SETTINGS.get("max_ws_frame_bytes", _DEFAULT_WS_FRAME_BYTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WS_FRAME_BYTES
    return max(64 * 1024, value)  # never lower than 64 KB


def enforce_content_length(
    request: Request, max_bytes: int, label: str = "request"
) -> None:
    """Reject *request* up-front when ``Content-Length`` exceeds *max_bytes*."""
    raw = request.headers.get("content-length")
    if not raw:
        return
    try:
        declared = int(raw)
    except ValueError:
        raise HTTPException(400, detail="Invalid Content-Length header.")
    if declared > max_bytes:
        raise HTTPException(
            413,
            detail=(
                f"{label} too large: {declared} bytes exceeds limit "
                f"{max_bytes} bytes."
            ),
        )


def enforce_actual_size(
    payload: bytes, max_bytes: int, label: str = "request"
) -> None:
    """Reject after-the-fact when a chunked stream exceeds *max_bytes*."""
    if len(payload) > max_bytes:
        raise HTTPException(
            413,
            detail=(
                f"{label} too large: {len(payload)} bytes exceeds limit "
                f"{max_bytes} bytes."
            ),
        )


__all__ = [
    "enforce_actual_size",
    "enforce_content_length",
    "get_max_result_bytes",
    "get_max_ws_frame_bytes",
]
