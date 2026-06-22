"""Worker → master live log forwarding.

Wave-after-Batch-C addition (Batch D1). The worker accumulates task
output in :class:`nexus.telemetry.logs.LogStream`; this module batches
those chunks and POSTs them to the dispatching master's
``/peer/task_log_chunk/{task_id}`` so the master's
``STATE.task_log_buffers`` fills up while the task is still running.
The UI's existing ``/local/task_log_tail`` poller then shows live logs
for remotely-executed tasks, not just locally-executed ones.

Design choices kept deliberately small:

* Pure fire-and-forget — :func:`enqueue_chunk` never raises, never
  blocks. Lost forwards are non-fatal; the worker still ships the full
  log via ``/peer/submit_result`` when the task finishes.
* One coalescing flusher coroutine per active task; sleeps 250 ms or
  flushes early when the pending buffer reaches 4 KiB. Avoids a POST
  per ``+=``.
* Targets are registered explicitly by ``worker_client_process``
  before invoking the executor and dropped after the result is
  submitted (success / failure / drop).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from nexus.core import get_node_identity, resolve_uuid_to_ip

_log = logging.getLogger("nexus.networking.log_forwarder")

_FLUSH_INTERVAL_S = 0.25
_EARLY_FLUSH_BYTES = 4096
_HTTP_TIMEOUT_S = 4.0


@dataclass
class _Target:
    master_ip: str
    token: str
    pending: list[str] = field(default_factory=list)
    pending_bytes: int = 0
    flusher: Optional[asyncio.Task] = None


_TARGETS: dict[str, _Target] = {}
_LOCK: Optional[asyncio.Lock] = None
_CLIENT: Optional[httpx.AsyncClient] = None
_SCHEMES: dict[str, str] = {}


def _get_lock() -> asyncio.Lock:
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(verify=False, timeout=_HTTP_TIMEOUT_S)
    return _CLIENT


def register_target(task_id: str, master_ip: str, token: str) -> None:
    """Record the master to forward this task's logs to."""
    if not task_id or not master_ip or not token:
        return
    _TARGETS[task_id] = _Target(master_ip=master_ip, token=token)


def unregister_target(task_id: str) -> None:
    """Stop forwarding for this task. Cancels any pending flusher."""
    tgt = _TARGETS.pop(task_id, None)
    if not tgt:
        return
    if tgt.flusher and not tgt.flusher.done():
        tgt.flusher.cancel()


def enqueue_chunk(task_id: str, chunk: str) -> None:
    """Append *chunk* to the forward buffer; never raises."""
    if not chunk:
        return
    tgt = _TARGETS.get(task_id)
    if tgt is None:
        return
    tgt.pending.append(chunk)
    tgt.pending_bytes += len(chunk)
    if tgt.flusher is None or tgt.flusher.done():
        try:
            loop = asyncio.get_event_loop()
            tgt.flusher = loop.create_task(_flush_loop(task_id))
        except RuntimeError:
            return


async def _flush_loop(task_id: str) -> None:
    """Drain *task_id*'s pending buffer in 250 ms batches."""
    try:
        while True:
            tgt = _TARGETS.get(task_id)
            if tgt is None:
                return
            # Early flush if the buffer is already large, otherwise wait.
            if tgt.pending_bytes < _EARLY_FLUSH_BYTES:
                await asyncio.sleep(_FLUSH_INTERVAL_S)
            tgt = _TARGETS.get(task_id)
            if tgt is None:
                return
            if not tgt.pending:
                # Nothing accumulated this cycle — stop the loop; the
                # next enqueue_chunk will restart it.
                tgt.flusher = None
                return
            payload = "".join(tgt.pending)
            tgt.pending.clear()
            tgt.pending_bytes = 0
            await _post_chunk(tgt.master_ip, tgt.token, task_id, payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.debug("log forwarder loop for %s failed: %s", task_id, exc)


async def _post_chunk(
    master_ip: str, token: str, task_id: str, chunk: str
) -> None:
    """POST one batched chunk to the master; best-effort."""
    resolved_ip = resolve_uuid_to_ip(master_ip) or master_ip
    if resolved_ip == master_ip and str(master_ip).startswith("nexus_"):
        return  # master not resolvable yet — drop, will retry next batch
    headers = {
        "X-Cluster-Key": str(token),
        "X-Node-Address": get_node_identity(),
    }
    cached = _SCHEMES.get(master_ip)
    order = [cached, "https", "http"] if cached else ["https", "http"]
    seen: set[str] = set()
    client = _get_client()
    for scheme in order:
        if not scheme or scheme in seen:
            continue
        seen.add(scheme)
        try:
            res = await client.post(
                f"{scheme}://{resolved_ip}/peer/task_log_chunk/{task_id}",
                data={"chunk": chunk},
                headers=headers,
                timeout=_HTTP_TIMEOUT_S,
            )
            _SCHEMES[master_ip] = scheme
            if res.status_code != 200:
                _log.debug(
                    "log forward to %s returned %s", master_ip, res.status_code
                )
            return
        except httpx.RequestError:
            continue


def reset_for_testing() -> None:
    """Clear all forwarder state. Tests call this between cases.

    Tolerates a closed event loop (the per-test loop in unit tests is
    already gone by the time pytest tears down the fixture).
    """
    for tgt in _TARGETS.values():
        flusher = tgt.flusher
        if flusher and not flusher.done():
            try:
                flusher.cancel()
            except Exception:
                pass
    _TARGETS.clear()
    _SCHEMES.clear()


__all__ = [
    "register_target",
    "unregister_target",
    "enqueue_chunk",
    "reset_for_testing",
]
