"""Alert ring buffer.

Extracted from node_modified.py (line 495, 1562-1565).

Alerts are short structured messages the UI renders in the Diagnostics tab.
They are *observations*, not user notifications — the the original implementation UI only shows
the 200 most recent. The ring buffer lives in
:data:`nexus.core.STATE.alerts` so the UI broadcaster can read it without
importing this module.

Severities (loose convention — the UI colours them):

* ``info`` — normal events worth surfacing (peer joined, settings changed)
* ``warn`` — recoverable issues (queue stall, retry storm)
* ``error`` — something broke (relay dropped, Docker ping failed)
"""

from __future__ import annotations

from nexus.core.state import STATE
from nexus.utils.time import now_epoch


def push_alert(severity: str, code: str, message: str) -> None:
    """Prepend an alert onto the ring buffer.

    *code* is a short machine-friendly identifier (``queue_stall``,
    ``relay_drop``). *message* is a human-readable sentence.
    """
    STATE.alerts.appendleft(
        {
            "ts": now_epoch(),
            "severity": severity,
            "code": code,
            "message": message,
        }
    )


def snapshot_alerts() -> list[dict]:
    """Return a shallow-copy list of the current alerts, newest first."""
    return list(STATE.alerts)
