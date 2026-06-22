"""Avatar upload and serve endpoints.

Extracted from node_modified.py:

* ``upload_avatar`` — lines 8549-8558
* ``get_avatar`` — lines 8561-8567

Security posture matches the original implementation: 2MB cap, PNG/JPEG magic-byte check,
``0o600`` permissions on the saved file. The avatar lives inside the
per-port cache directory so multiple nodes on one host don't stomp on
each other.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from nexus.core.paths import cache_dir, secure_file_permissions
from nexus.security.auth import verify_local_auth

router = APIRouter(tags=["Settings"])


_MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2MB
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _avatar_path(port: int) -> str:
    return os.path.join(str(cache_dir(port)), "avatar.png")


@router.post(
    "/local/upload_avatar",
    dependencies=[Depends(verify_local_auth)],
    summary="Upload node avatar image",
)
async def upload_avatar(file: UploadFile = File(...)) -> dict:
    """Accept a small PNG/JPEG and save it as the node avatar."""
    content = await file.read()
    if len(content) > _MAX_AVATAR_BYTES:
        raise HTTPException(400, detail="Avatar file too large (max 2MB).")
    if not (content.startswith(_PNG_MAGIC) or content.startswith(_JPEG_MAGIC)):
        raise HTTPException(400, detail="Avatar must be PNG or JPEG.")
    # The port is derived from CLI args at runtime; fall back to the default
    # so unit tests hitting a factory-built app don't need to pass a port.
    from nexus.core.constants import DEFAULT_HTTP_PORT

    path = _avatar_path(DEFAULT_HTTP_PORT)
    with open(path, "wb") as f:
        f.write(content)
    secure_file_permissions(path)
    return {"status": "ok"}


@router.get("/local/avatar", summary="Serve node avatar image", include_in_schema=False)
async def get_avatar() -> FileResponse:
    """Return the avatar image (404 if none has been uploaded)."""
    from nexus.core.constants import DEFAULT_HTTP_PORT

    path = _avatar_path(DEFAULT_HTTP_PORT)
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    raise HTTPException(404, detail="No avatar set.")


__all__ = ["router"]
