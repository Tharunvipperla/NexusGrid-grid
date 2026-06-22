"""Serve ``index.html`` at ``/`` with local-token injection and CSP headers.

Extracted from node_modified.py (``serve_ui`` at lines 7297-7324).

The served HTML embeds the local API token in a ``<meta>`` tag so the
frontend can authenticate its own fetch calls. A Content-Security-Policy
header locks script/style sources to ``self`` plus the two CDNs the original implementation
already trusts.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from nexus.core import get_resource_dir
from nexus.security.auth import _management_client_allowed
from nexus.security.tokens import get_local_api_token
from nexus.utils.net import client_host

router = APIRouter(tags=["UI"])


_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self' ws: wss:; "
    "img-src 'self' data:;"
)


@router.get("/classic", include_in_schema=False)
def serve_ui(request: Request) -> Response:
    """Return the classic ``index.html`` with the local API token injected.

    The v3 UI took over ``/``; the classic monolith stays reachable here
    until it is retired for good.

    emits an ``ETag`` over ``(token, file mtime)``. The
    browser revalidates every load (``Cache-Control: no-cache``) but
    a 304 saves the ~200 KB body transfer whenever neither the token
    nor the file has changed. The token bakes into the ETag so a
    token rotation invalidates the cache automatically — a stale page
    can never be reused across token changes.
    """
    if not _management_client_allowed(client_host(request)):
        raise HTTPException(
            status_code=403,
            detail="Management UI is restricted to local or private-network clients.",
        )
    html_path = os.path.join(str(get_resource_dir()), "nexus", "ui", "index.html")
    token = get_local_api_token()
    try:
        mtime = os.path.getmtime(html_path)
    except OSError:
        mtime = 0.0
    etag_seed = f"{token}:{mtime}".encode("utf-8")
    etag = '"' + hashlib.sha256(etag_seed).hexdigest()[:16] + '"'

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "no-store, must-revalidate",
            },
        )

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    token_meta = f'<meta name="nexus-token" content="{token}">'
    html = html.replace("</head>", f"    {token_meta}\n</head>", 1)
    response = HTMLResponse(content=html)
    response.headers["Content-Security-Policy"] = _CSP
    # Follow-up: switch from `no-cache` to `no-store` because
    # Chrome's disk cache was holding stale HTML across redeploys despite
    # the token-bound ETag — users were seeing old JS while the new token
    # rotated underneath, hiding the onboarding modal and freezing the
    # relay pill on "checking…". `no-store` makes the browser fetch
    # fresh every navigation, no exceptions.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["ETag"] = etag
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ── v3 UI ─────────────────────────────────────────────────────────────
# The redesigned React UI is the default at / (and still answers at /app
# for old bookmarks); the classic index.html moved to /classic. Source
# lives in webui/ and is bundled to webui/dist/bundle.js by esbuild at
# build time; the .spec ships the webui/ tree alongside index.html.


def _webui_path(*parts: str) -> str:
    return os.path.join(str(get_resource_dir()), "webui", *parts)


@router.get("/", include_in_schema=False)
@router.get("/app", include_in_schema=False)
def serve_app(request: Request) -> Response:
    """Serve the v3 UI shell with the local API token injected (same auth
    model as the classic page). Falls back to the classic page if the v3
    bundle is missing from the build, so `/` is never a 404."""
    if not _management_client_allowed(client_host(request)):
        raise HTTPException(
            status_code=403,
            detail="Management UI is restricted to local or private-network clients.",
        )
    html_path = _webui_path("index.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return serve_ui(request)
    token = get_local_api_token()
    token_meta = f'<meta name="nexus-token" content="{token}">'
    html = html.replace("</head>", f"    {token_meta}\n</head>", 1)
    response = HTMLResponse(content=html)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@router.get("/app/bundle.js", include_in_schema=False)
def serve_app_bundle(request: Request) -> Response:
    if not _management_client_allowed(client_host(request)):
        raise HTTPException(status_code=403, detail="restricted")
    path = _webui_path("dist", "bundle.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="bundle not built")
    return FileResponse(path, media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@router.get("/app/styles.css", include_in_schema=False)
def serve_app_styles(request: Request) -> Response:
    if not _management_client_allowed(client_host(request)):
        raise HTTPException(status_code=403, detail="restricted")
    path = _webui_path("styles.css")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="styles not bundled")
    return FileResponse(path, media_type="text/css",
                        headers={"Cache-Control": "no-store"})


__all__ = ["router", "serve_ui"]
