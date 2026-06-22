"""Minimal health / diagnostics router.

Extracted from Phase-1/node_modified.py:

* ``/health`` — line 8527
* ``/local/shutdown`` (graceful shutdown) — lines 8533-8546
* ``/local/diagnostics`` — line 8401-8412 (full port pending)

Kept deliberately thin: auth + delegation to business modules. The heavy
``/local/*`` routes live in :mod:`nexus.api.local`.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from nexus import __version__

_START_TIME = time.time()

router = APIRouter(tags=["Diagnostics"])


@router.get("/health", summary="Liveness probe")
async def health_check() -> dict:
    """Return a minimal liveness payload with node uptime and app version."""
    return {"status": "ok", "uptime": int(time.time() - _START_TIME), "version": __version__}


__all__ = ["router"]
