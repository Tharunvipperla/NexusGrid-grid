"""Pytest fixtures shared across the test suite.

Tests build small FastAPI apps from individual routers rather than
``nexus.app.create_app``: the full lifespan opens a SQLite DB, starts
discovery sockets, and spawns background tasks. Per-router isolation keeps
tests fast and deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path


_PHASE2_ROOT = Path(__file__).resolve().parent.parent
if str(_PHASE2_ROOT) not in sys.path:
    sys.path.insert(0, str(_PHASE2_ROOT))
