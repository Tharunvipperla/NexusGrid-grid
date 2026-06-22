"""D3 — outbound webhooks / event subscriptions.

External systems react to grid events: the node POSTs a small JSON
payload to user-configured URLs whenever a matching event fires.

We hook the existing in-process domain bus (:mod:`nexus.core.events`) the
same way :mod:`nexus.ui.broadcaster` does — subscribe once at startup to a
curated set of bus events and fan each one out to the subscriptions the user
saved in ``LOCAL_SETTINGS["webhooks"]``. Subscriptions are read at fire-time
so edits in the UI take effect without a restart.

Delivery is best-effort and non-blocking: each POST runs as its own task with
a short timeout; failures are recorded in a small in-memory ring buffer the UI
shows, never retried in a queue (a receiver that wants reliability can ask the
node to re-test, or poll the REST API). Payloads are optionally signed with
HMAC-SHA256 so the receiver can verify authenticity.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import deque
from typing import Any

import httpx

_log = logging.getLogger("nexus.runtime.webhooks")

# Public webhook event catalog — the dotted names a subscription can filter on.
# Kept deliberately small and stable; surfaced to the UI so users pick from a
# known list instead of guessing internal bus names.
WEBHOOK_EVENTS: list[str] = [
    "task.status_changed",
    "task.completed",
    "task.failed",
    "dag.released",
    "dag.gated",
    "scheduler.requeued",
    "storage.deposit_completed",
    "storage.offer_incoming",
]

_DELIVERY_TIMEOUT_S = 8.0
_DELIVERY_LOG_MAX = 50
_deliveries: deque[dict] = deque(maxlen=_DELIVERY_LOG_MAX)

_INSTALLED = False


# --- pure helpers (unit-tested directly) -------------------------------------


def event_matches(subscribed: list[str], event: str) -> bool:
    """True if ``event`` matches any pattern in ``subscribed``.

    Patterns are exact dotted names, ``*`` (all), or a ``domain.*`` prefix
    wildcard (e.g. ``task.*`` matches ``task.completed``).
    """
    for pat in subscribed or []:
        if pat == "*" or pat == event:
            return True
        if pat.endswith(".*") and event.startswith(pat[:-1]):
            return True
    return False


def build_payload(event: str, data: dict, node_id: str = "") -> dict:
    """Assemble the JSON body POSTed to a subscriber."""
    from nexus.utils.time import iso_now

    return {
        "event": event,
        "ts": iso_now(),
        "node": node_id,
        "data": data or {},
    }


def sign_body(secret: str, body: bytes) -> str:
    """Return ``sha256=<hex>`` HMAC of ``body`` for the ``X-NexusGrid-Signature``
    header, or ``""`` when no secret is set."""
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def recent_deliveries() -> list[dict]:
    """Most-recent-first snapshot of the delivery log for the UI."""
    return list(reversed(_deliveries))


# --- delivery ----------------------------------------------------------------


def _record(entry: dict) -> None:
    _deliveries.append(entry)


async def _deliver(sub: dict, event: str, data: dict, node_id: str) -> dict:
    """POST one event to one subscription, recording the outcome."""
    from nexus.utils.time import iso_now

    url = str(sub.get("url") or "")
    payload = build_payload(event, data, node_id)
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "NexusGrid-Webhook",
        "X-NexusGrid-Event": event,
    }
    sig = sign_body(str(sub.get("secret") or ""), body)
    if sig:
        headers["X-NexusGrid-Signature"] = sig

    entry: dict[str, Any] = {
        "id": sub.get("id"),
        "url": url,
        "event": event,
        "at": iso_now(),
    }
    try:
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_S) as client:
            resp = await client.post(url, content=body, headers=headers)
        entry["status"] = resp.status_code
        entry["ok"] = 200 <= resp.status_code < 300
    except Exception as exc:  # network error, timeout, bad URL
        entry["status"] = None
        entry["ok"] = False
        entry["error"] = str(exc)[:200]
    _record(entry)
    return entry


def _subscriptions() -> list[dict]:
    from nexus.core import LOCAL_SETTINGS

    subs = LOCAL_SETTINGS.get("webhooks") or []
    return subs if isinstance(subs, list) else []


def _node_id() -> str:
    try:
        from nexus.core import get_node_identity

        return get_node_identity()
    except Exception:
        return ""


def dispatch(event: str, data: dict) -> None:
    """Schedule delivery of ``event`` to every enabled, matching subscription.

    Non-blocking: returns immediately, deliveries run as background tasks.
    Safe to call with no running loop (deliveries are skipped with a debug log).
    """
    node_id = _node_id()
    for sub in _subscriptions():
        if not isinstance(sub, dict) or not sub.get("enabled", True):
            continue
        if not sub.get("url"):
            continue
        if not event_matches(sub.get("events") or [], event):
            continue
        try:
            asyncio.ensure_future(_deliver(sub, event, data, node_id))
        except RuntimeError:
            _log.debug("webhook dispatch for %s skipped — no running loop", event)


# --- bus bridge --------------------------------------------------------------


def _on_status_changed(data: dict) -> None:
    """Forward raw status changes and synthesize task.completed / task.failed."""
    dispatch("task.status_changed", data)
    new_status = data.get("new_status")
    if new_status == "completed":
        dispatch("task.completed", data)
    elif new_status == "failed":
        dispatch("task.failed", data)


def install_webhook_dispatcher() -> None:
    """Subscribe the dispatcher to the curated bus events. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    from nexus.core import events

    events.subscribe("task.status_changed", _on_status_changed)
    events.subscribe("scheduler.dag_released", lambda d: dispatch("dag.released", d))
    events.subscribe("scheduler.dag_gated", lambda d: dispatch("dag.gated", d))
    events.subscribe("scheduler.requeued", lambda d: dispatch("scheduler.requeued", d))
    events.subscribe(
        "storage.deposit_completed", lambda d: dispatch("storage.deposit_completed", d)
    )
    events.subscribe(
        "storage.offer_incoming", lambda d: dispatch("storage.offer_incoming", d)
    )
    _INSTALLED = True


__all__ = [
    "WEBHOOK_EVENTS",
    "event_matches",
    "build_payload",
    "sign_body",
    "recent_deliveries",
    "dispatch",
    "install_webhook_dispatcher",
]
