"""Relay-latency cache + periodic probe loop.

Maintains an in-memory dict of ``{relay_url: {rtt_ms, last_probed_at}}``
populated by:

* the periodic background probe loop started in :mod:`nexus.app`
  (sweeps every known relay URL — legacy ``relay_server_url`` setting +
  every ``GroupRelayBinding`` row — at a per-URL interval that adapts
  to RTT stability, see :data:`PROBE_INTERVAL_SEC` /
  :data:`STABLE_PROBE_INTERVAL_SEC`), and
* on-demand pokes from the ``/local/groups/{id}/relays/probe`` endpoint
  so the UI's "Probe" button reflects the latest measurement without
  waiting for the next loop tick.

The cache is the source of truth for /C's pool-aware relay
selection: outbound RPC picks the lowest-RTT relay among the bound
set, falling back to the next-best on timeout.

adds two perf hygiene knobs:

* :data:`_PROBE_CONCURRENCY` — semaphore that caps concurrent HTTP
  probes so a sweep across many relays doesn't spike CPU / sockets.
* Adaptive per-URL interval — a relay whose last :data:`_HISTORY_SIZE`
  measurements are tight (within ±30% or ±50 ms of the mean) drops to
  :data:`STABLE_PROBE_INTERVAL_SEC`. New / fluctuating relays stay at
  the tight :data:`PROBE_INTERVAL_SEC` cadence.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from nexus.core import LOCAL_SETTINGS
from nexus.storage import get_session
from nexus.storage.models import GroupRelayBinding

_log = logging.getLogger("nexus.runtime.relay_latency")

# How often the background loop *considers* re-probing every known relay.
# Per-URL gating below decides whether each URL is actually due.
PROBE_INTERVAL_SEC = 20
STABLE_PROBE_INTERVAL_SEC = 120

# Cap concurrent HTTP probes during a sweep so we don't
# spike CPU / sockets when a node knows about a lot of relays at once.
_PROBE_CONCURRENCY = asyncio.Semaphore(4)

# Inner-loop tick — short so newly-discovered relays get their first
# probe quickly, but per-URL gating keeps actual probe rate sane.
_OUTER_TICK_SEC = 5

# Tail of recent RTT samples per URL, used by :func:`_is_stable`.
_HISTORY_SIZE = 5
_RTT_HISTORY: dict[str, list[int]] = {}

# ``{relay_url: {"rtt_ms": int|None, "last_probed_at": float}}``.
# ``rtt_ms`` is None when the last probe failed; cache is in-memory only.
_CACHE: dict[str, dict] = {}


def record(relay_url: str, rtt_ms: int | None) -> None:
    """Store one probe result. Called by the loop + by probe_group_relays."""
    url = (relay_url or "").strip()
    if not url:
        return
    _CACHE[url] = {"rtt_ms": rtt_ms, "last_probed_at": time.time()}
    if rtt_ms is not None:
        hist = _RTT_HISTORY.setdefault(url, [])
        hist.append(int(rtt_ms))
        if len(hist) > _HISTORY_SIZE:
            hist.pop(0)


def get(relay_url: str) -> int | None:
    """Latest known RTT (ms) for ``relay_url``, or None if never probed
    successfully."""
    entry = _CACHE.get((relay_url or "").strip())
    if not entry:
        return None
    return entry.get("rtt_ms")


def snapshot() -> dict[str, dict]:
    """Full cache snapshot — used by the ``/local/relay/latency`` endpoint."""
    return {url: dict(entry) for url, entry in _CACHE.items()}


def best_relay_url(candidates: list[str]) -> str | None:
    """Return the lowest-RTT URL among ``candidates`` that has a probe hit.

    Returns ``None`` when no candidate has a successful probe — caller
    falls back to its own default (e.g. the legacy single setting).
    """
    best_url: str | None = None
    best_rtt: int | None = None
    for url in candidates:
        rtt = get(url)
        if rtt is None:
            continue
        if best_rtt is None or rtt < best_rtt:
            best_rtt = rtt
            best_url = url
    return best_url


def _is_stable(url: str) -> bool:
    """True if the last :data:`_HISTORY_SIZE` RTTs cluster tightly
    enough that re-probing every 60 s is wasteful."""
    hist = _RTT_HISTORY.get(url, [])
    if len(hist) < _HISTORY_SIZE:
        return False
    avg = sum(hist) / len(hist)
    tolerance = max(50.0, avg * 0.3)
    return all(abs(r - avg) <= tolerance for r in hist)


def _probe_due_interval(url: str) -> int:
    """Adaptive interval for the next probe of *url*."""
    return STABLE_PROBE_INTERVAL_SEC if _is_stable(url) else PROBE_INTERVAL_SEC


async def _collect_known_relays() -> list[str]:
    """Every relay URL this node should probe.

    Sources:

    * Legacy ``relay_server_url`` setting (the primary subscription).
    * Every active ``GroupRelayBinding`` (per-group bindings).
    * follow-up: the in-process local relay's LAN URL and its
      Cloudflare tunnel URL when either is up. Without these, a node
      running its own relay has zero latency data until at least one
      group binds those URLs, so the sidebar pill and the group-create
      form both show "no latency" even though both endpoints are
      perfectly reachable.
    """
    urls: set[str] = set()
    legacy = str(LOCAL_SETTINGS.get("relay_server_url", "") or "").strip()
    if legacy:
        urls.add(legacy)
    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupRelayBinding.relay_url).where(
                    GroupRelayBinding.status == "active"
                )
            )
        ).fetchall()
    for (url,) in rows:
        if url:
            urls.add(url.strip())
    # Local relay bindings — always probe these when up.
    try:
        from nexus.runtime import local_relay
        st = local_relay.status()
        if st.get("running") and st.get("suggested_url"):
            urls.add(str(st["suggested_url"]).strip())
    except Exception:
        pass
    tunnel = str(LOCAL_SETTINGS.get("relay_self_heal_url", "") or "").strip()
    if tunnel:
        urls.add(tunnel)
    return sorted(u for u in urls if u)


#: consecutive probe failures before we flip a binding to
#: ``offline``. Three matches the user's stated tolerance ("a couple of
#: blips don't count, three in a row is a real outage").
OFFLINE_FAILURE_THRESHOLD = 3


async def _probe_once(url: str) -> None:
    """Probe one URL (rate-limited by :data:`_PROBE_CONCURRENCY`) and
    update the latency cache + binding state machine.

    wired in:

    * Success: increment isn't reset on the row directly — the
      :func:`~nexus.runtime.relay_state.transition` call into
      ``STATE_ONLINE`` zeroes it as its own side effect. The probe just
      kicks the binding toward online if it had been offline.
    * Failure: bumps ``consecutive_probe_failures``. Crossing
      :data:`OFFLINE_FAILURE_THRESHOLD` while currently ``online`` flips
      the binding to ``offline``. Group traffic then auto-routes through
      the surviving bound relays via the publish_frame fan-out.
    """
    from nexus.api.groups import _probe_relay_url
    from nexus.runtime import relay_state

    async with _PROBE_CONCURRENCY:
        reachable, rtt_ms = await _probe_relay_url(url)
    record(url, rtt_ms if reachable else None)

    async with get_session() as session:
        rows = (
            await session.execute(
                select(GroupRelayBinding).where(
                    GroupRelayBinding.relay_url == url
                )
            )
        ).scalars().all()
        for row in rows:
            if reachable:
                row.last_rtt_ms = rtt_ms
                # Recovery: if the binding was offline/reconnecting and we
                # just got a successful probe back, walk it toward online.
                if row.state == relay_state.STATE_OFFLINE:
                    await relay_state.transition(
                        row, relay_state.STATE_RECONNECTING,
                        reason="probe ok",
                    )
                if row.state == relay_state.STATE_RECONNECTING:
                    await relay_state.transition(
                        row, relay_state.STATE_SYNCING,
                        reason="probe ok",
                    )
                if row.state == relay_state.STATE_SYNCING:
                    await relay_state.transition(
                        row, relay_state.STATE_ONLINE,
                        reason="catch-up complete",
                    )
            else:
                # Unreachable — drop the stale RTT so an offline relay
                # doesn't keep showing a latency it can't be measuring.
                row.last_rtt_ms = None
                row.consecutive_probe_failures = (
                    int(row.consecutive_probe_failures or 0) + 1
                )
                if (
                    row.state == relay_state.STATE_ONLINE
                    and row.consecutive_probe_failures >= OFFLINE_FAILURE_THRESHOLD
                ):
                    await relay_state.transition(
                        row, relay_state.STATE_OFFLINE,
                        reason=f"{row.consecutive_probe_failures} consecutive probe failures",
                    )
        await session.commit()


async def probe_loop() -> None:
    """Background task: every :data:`_OUTER_TICK_SEC` seconds, re-probe
    every URL whose adaptive interval has elapsed since the last probe.

    Outer tick is short so a brand-new relay gets a fresh RTT within
    ~15 s of being discovered; per-URL adaptive interval keeps the
    actual probe rate down to once-per-5-min for stable relays.
    """
    while True:
        try:
            urls = await _collect_known_relays()
            if urls:
                now = time.time()
                due: list[str] = []
                for url in urls:
                    entry = _CACHE.get(url) or {}
                    last = entry.get("last_probed_at", 0.0)
                    if now - last >= _probe_due_interval(url):
                        due.append(url)
                if due:
                    await asyncio.gather(*(_probe_once(u) for u in due))
        except Exception:
            _log.warning("relay_latency probe sweep failed", exc_info=True)
        await asyncio.sleep(_OUTER_TICK_SEC)


__all__ = [
    "PROBE_INTERVAL_SEC",
    "STABLE_PROBE_INTERVAL_SEC",
    "record",
    "get",
    "snapshot",
    "best_relay_url",
    "probe_loop",
]
