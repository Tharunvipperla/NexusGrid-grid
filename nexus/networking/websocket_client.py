"""Outbound WebSocket helper with websockets-library version tolerance.

Extracted from node_modified.py (``open_worker_websocket`` at
lines 654-666).

The ``websockets`` library renamed the per-connection extra-headers keyword
from ``extra_headers`` to ``additional_headers`` between majors. Rather than
pin a single version, the original implementation tries both and falls through the
``TypeError`` path. This helper preserves that dance so the worker-client
and relay-client loops can stay on whichever version a given deployment
has installed.
"""

from __future__ import annotations

import ssl
from contextlib import asynccontextmanager

import websockets


def _unverified_tls_context() -> ssl.SSLContext:
    """Trust is established via signing key + cert fingerprint pinning, not
    CA chains, so the worker connects without verification — same posture as
    ``peer_http.py`` (``httpx.AsyncClient(verify=False)``)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@asynccontextmanager
async def open_worker_websocket(ws_url: str, headers: dict):
    """Open *ws_url* with *headers* injected, yielding the connection.

    When given a plain ``ws://`` URL the helper first attempts the ``wss://``
    upgrade so a TLS master is reachable; if that fails it falls back to plain
    ``ws://`` for non-TLS deployments.

    Usage::

        async with open_worker_websocket(url, {"X-Cluster-Key": tok}) as ws:
            await ws.send(...)
            async for frame in ws:
                ...
    """
    candidates: list[str] = []
    if ws_url.startswith("ws://"):
        candidates.append("wss://" + ws_url[len("ws://"):])
    candidates.append(ws_url)

    last_err: BaseException | None = None
    for url in candidates:
        connect_kwargs: dict = {}
        if url.startswith("wss://"):
            connect_kwargs["ssl"] = _unverified_tls_context()
        for header_arg in ("additional_headers", "extra_headers"):
            try:
                async with websockets.connect(
                    url, **connect_kwargs, **{header_arg: headers}
                ) as ws:
                    yield ws
                    return
            except TypeError as exc:
                if header_arg in str(exc):
                    last_err = exc
                    continue
                raise
            except Exception as exc:
                last_err = exc
                break
    raise last_err or RuntimeError("Unable to open websocket connection.")


__all__ = ["open_worker_websocket"]
