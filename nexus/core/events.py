"""In-process publish/subscribe event bus.

Allows loose coupling between subpackages that would otherwise have to
import each other. For example, ``runtime`` emits ``task.completed`` when a
task exits; ``telemetry`` subscribes to update metrics; ``ui`` subscribes to
broadcast the UI update. Neither ``runtime`` nor ``telemetry`` imports
``ui``.

Contract
--------

* Handlers are called synchronously in the order they were registered.
* An exception in one handler is logged (via standard ``logging``) and
  swallowed; it does not affect other handlers.
* Event names are strings in dotted form: ``domain.verb`` (``task.completed``,
  ``peer.joined``, ``settings.changed``). Payloads are plain dicts.
* Async handlers are allowed — they are scheduled on the running loop via
  ``asyncio.ensure_future``; their completion is not awaited.

This is a deliberately minimal implementation. If we ever need priorities,
awaited handlers, or middleware, add them here rather than inventing a
second bus.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Union

_log = logging.getLogger("nexus.events")

Handler = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]

_subscribers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(event: str, handler: Handler) -> None:
    """Register *handler* to be invoked whenever *event* is published."""
    _subscribers[event].append(handler)


def unsubscribe(event: str, handler: Handler) -> None:
    """Remove a previously-registered handler. No-op if not registered."""
    lst = _subscribers.get(event)
    if not lst:
        return
    try:
        lst.remove(handler)
    except ValueError:
        pass


def publish(event: str, payload: dict[str, Any] | None = None) -> None:
    """Invoke every subscriber of *event* with *payload* (default ``{}``).

    Synchronous handlers run in registration order. Async handlers are
    scheduled on the running loop and not awaited; if no loop is running,
    async handlers are skipped with a debug log.
    """
    data = payload or {}
    for handler in list(_subscribers.get(event, ())):
        try:
            result = handler(data)
        except Exception:
            _log.exception("event handler for %s raised", event)
            continue
        if asyncio.iscoroutine(result):
            try:
                asyncio.ensure_future(result)
            except RuntimeError:
                _log.debug(
                    "async handler for %s skipped — no running event loop", event
                )


def clear_all() -> None:
    """Remove every subscriber. Intended for tests."""
    _subscribers.clear()


def subscriber_count(event: str) -> int:
    """Return how many handlers are currently registered for *event*."""
    return len(_subscribers.get(event, ()))
