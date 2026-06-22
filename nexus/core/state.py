"""Shared in-memory state registry.

In the original implementation this state lived as a constellation of top-level globals in
``node_modified.py`` (``ACTIVE_WORKERS``, ``INBOUND_PEER_WS``, ``TASK_QUEUE``,
``PEER_PRESENCE``, ``METRICS``, …). Moving them under a single namespace
does two things:

1. Makes ownership obvious. Each field declares which subpackage is allowed
   to *write* to it; every other subpackage reads only.
2. Makes it possible to reset state in tests without walking a long list of
   module globals.

Subpackages import :data:`STATE` and read/write the fields they own. They
**must not** grow new fields here without updating this file's docstring and
the owner column below.

Field ownership matrix
----------------------

============================  ===================  ==========================
Field                         Writer               Readers
============================  ===================  ==========================
``task_queue``                tasks.queue          scheduler, api, ui
``consent_strikes``           networking.peer      tasks.lifecycle, scheduler
``disrupted_master_tasks``    runtime              scheduler, api
``active_workers``            networking.worker    scheduler, telemetry, api
``inbound_peer_ws``           networking.peer      api, ui, telemetry
``outbound_master_ws``        networking.worker    networking.peer, runtime
``peer_presence``             telemetry.presence   networking, scheduler, api
``discovered_peers``          networking.discovery api, ui
``running_task_containers``   runtime              api (disrupt)
``running_task_procs``        runtime.native       api (disrupt)
``interrupted_task_ids``      runtime              runtime, scheduler
``preempted_task_ids``        runtime              runtime, scheduler
``task_log_buffers``          telemetry.logs       api, ui
``metrics``                   telemetry.metrics    api, ui
``alerts``                    telemetry.alerts     api, ui
``relay_peers``               networking.relay     api, ui
============================  ===================  ==========================

Only the fields needed for the currently-extracted subpackages are populated
at any given step. Unfilled fields stay at their empty default so downstream
code can read them without ``AttributeError``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SharedState:
    """Process-wide mutable state. One instance exported as :data:`STATE`."""

    # --- Task queue + worker lease bookkeeping ------------------------------
    task_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task_assign_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running_task_containers: dict[str, Any] = field(default_factory=dict)
    running_task_procs: dict[str, Any] = field(default_factory=dict)
    running_task_cleanup_dirs: dict[str, str] = field(default_factory=dict)
    running_container_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    interrupted_task_ids: set[str] = field(default_factory=set)
    preempted_task_ids: set[str] = field(default_factory=set)
    disrupted_master_tasks: set[str] = field(default_factory=set)
    consent_strikes: dict[tuple, int] = field(default_factory=dict)

    # --- Consent-mode task offers (master side) ----------------------------
    pending_task_offers: dict[str, dict] = field(default_factory=dict)
    pending_offers_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- Consent-mode task offers (worker side, awaiting UI decision) ------
    worker_pending_offers: dict[str, dict] = field(default_factory=dict)
    worker_pending_offers_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- Relay diagnostics --------------------------------------------------
    relay_last_error: str = ""

    # --- Peer / worker connectivity -----------------------------------------
    active_workers: dict[str, dict] = field(default_factory=dict)
    worker_state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inbound_peer_ws: dict[str, Any] = field(default_factory=dict)
    inbound_peer_ws_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    outbound_master_ws: dict[str, Any] = field(default_factory=dict)
    outbound_master_ws_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_cooldown_until: dict[str, float] = field(default_factory=dict)
    # Per-worker outcome tally for reliability-aware scheduling: worker_id ->
    # {"ok": int, "fail": int}. Written by scheduler.reliability at the master's
    # task completion / failure sites; read by the selector when a task opts into
    # "prefer reliable workers". In-memory (resets on restart) like the cooldown
    # map above — a scheduling heuristic, not durable accounting.
    worker_outcomes: dict[str, dict] = field(default_factory=dict)
    discovered_peers: dict[str, Any] = field(default_factory=dict)

    # --- Relay --------------------------------------------------------------
    relay_ws: Any = None
    relay_ws_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    relay_connected: bool = False
    relay_peers: dict[str, Any] = field(default_factory=dict)
    relay_settings_changed: asyncio.Event = field(default_factory=asyncio.Event)
    relay_peer_changed: asyncio.Event = field(default_factory=asyncio.Event)
    relay_http_pending: dict[str, Any] = field(default_factory=dict)
    relay_http_pending_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Pool of *secondary* relay connections (every active
    # GroupRelayBinding URL that isn't the legacy primary). Inbound
    # messages from these connections flow through the same handler
    # as the primary, so peers using any of the group's relays can
    # reach us. Outbound still goes through STATE.relay_ws for now;
    # W36.C will switch to pool-aware lowest-RTT selection.
    relay_ws_pool: dict[str, Any] = field(default_factory=dict)
    relay_ws_pool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Per-URL relay code fingerprint, captured from each
    # relay's register-ack reply. Group bindings with a frozen
    # ``Group.relay_code_fingerprint`` reject this URL if the value
    # here doesn't match. Empty / missing entry means the relay didn't
    # report one (pre-W41 server) — caller decides whether to block.
    relay_code_fingerprints: dict[str, str] = field(default_factory=dict)

    # --- Discovered peers lock (STATE.discovered_peers protected by this) --
    discovered_peers_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- Worker pull coordination ------------------------------------------
    worker_pull_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- Presence + telemetry ----------------------------------------------
    peer_presence: dict[str, dict] = field(default_factory=dict)
    task_log_buffers: dict[str, deque] = field(default_factory=dict)
    task_log_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    metrics: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    alerts: deque = field(default_factory=lambda: deque(maxlen=200))

    # --- Service tasks (long-running containers + tunnels) --
    # ``service_records`` lives on both master and worker:
    #   * master: full record {primary, standbys, status, started_at, manifest, ...}
    #   * worker: subset {task_id, container, started_at, expires_at, manifest}
    service_records: dict[str, dict] = field(default_factory=dict)
    # Worker-only: container_port -> host_port mappings learned via
    # container.reload() after detached start.
    service_port_mappings: dict[str, dict[int, int]] = field(default_factory=dict)
    # Updated by the tunnel pump on every byte; idle-timeout watchdog reads it.
    service_last_activity: dict[str, float] = field(default_factory=dict)
    # Tunnel registry on the master side (filled in 9b).
    service_tunnels: dict[str, dict] = field(default_factory=dict)
    # Per-service watchdog tasks (asyncio.Task handles, worker-side).
    service_watchdog_tasks: dict[str, Any] = field(default_factory=dict)
    # Per-service snapshot ticker tasks (asyncio.Task handles, worker primary side).
    service_snapshot_tasks: dict[str, Any] = field(default_factory=dict)
    # Worker-side: task_ids this node has been told to stage as a snapshot
    # standby (image pulled, snapshots written to disk, no container running).
    service_standbys: dict[str, dict] = field(default_factory=dict)
    # Dep-tunnel grants. On the dep's primary worker, master
    # writes "these peers may tunnel into service X" so the worker accepts
    # tunnel_open from non-master peers.
    service_dep_grants: dict[str, set[str]] = field(default_factory=dict)
    # Master-side reverse index: dep_task_id -> set of consumer_task_ids that
    # currently depend on it. Used by 9e failover to notify dependents.
    service_dependents: dict[str, set[str]] = field(default_factory=dict)
    # Per-service token-bucket rate limiters keyed by task_id.
    # Value is a ``nexus.networking.tunnel._TokenBucket``; typed as Any here
    # to avoid the import cycle.
    service_rate_buckets: dict[str, Any] = field(default_factory=dict)
    # Per-service HTTP inspector ring buffers (deque of dicts,
    # capped at 100 entries). Populated by the pump for service_kind=http.
    service_http_inspector: dict[str, Any] = field(default_factory=dict)
    # (5a.4.1): per-service session-replay byte ring. Each
    # entry is `(ts, direction, bytes)`; total bytes capped at 1 MB per task.
    service_replay_buffers: dict[str, Any] = field(default_factory=dict)
    # (5a.4.3): UDP tunnel state.
    #   service_udp_listeners[task_id] = {"transport", "host_port",
    #     "peer_id", "container_port", "pending": {udp_id: (addr, deadline)}}
    service_udp_listeners: dict[str, Any] = field(default_factory=dict)
    # Foreign-storage pump state, keyed by deposit_id. Two roles:
    #   * depositor: {role, peer_uuid, password, salt, file_path,
    #         total_bytes, chunk_count, sent_idx, acked_idx, ack_event,
    #         status}
    #   * host:      {role, peer_uuid, total_bytes, chunk_count, dir,
    #         received_idx, last_chunk_at, status}
    foreign_storage_pumps: dict[str, Any] = field(default_factory=dict)
    foreign_storage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Batch C: per-deposit_id flag that the unauthorized-access tripwire
    # has already audited a tamper event. Prevents the 2s lifecycle pass
    # from spamming repeat audits while the host hasn't fixed (or
    # destroyed) the chunks. Cleared when the deposit row is purged.
    foreign_storage_tripwire_fired: dict[str, bool] = field(default_factory=dict)
    service_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Ring of recent _send_to_peer attempt traces, keyed by peer
    # UUID. Lets the deposit endpoint explain "why couldn't we reach them?"
    # in a 503 instead of a generic message. Bounded to 16 peers.
    last_send_attempts: dict[str, list[str]] = field(default_factory=dict)
    # Temp directories created by /foreign_storage/upload_temp.
    # Two indices so we can clean up by either staged path (when the user
    # types it into the deposit form) or by deposit_id (after transfer
    # completes). Removing the entry from one drops both — the cleanup
    # helper reconciles them.
    upload_temp_dirs_by_path: dict[str, str] = field(default_factory=dict)
    upload_temp_dirs_by_deposit: dict[str, str] = field(default_factory=dict)
    # P2: Auto-mode FS offer fan-out — only populated for deposits whose
    # ``status == "offering_multi"``. The candidates list is the set of
    # peers the offer was sent to (so we can broadcast cancels to losers
    # on first-accept and on timeout). ``started_at`` is monotonic time
    # used by the lifecycle pass to enforce the per-user timeout.
    # Both maps are cleared on acceptance, decline-out, or timeout.
    foreign_storage_auto_candidates: dict[str, list[str]] = field(
        default_factory=dict
    )
    foreign_storage_auto_started_at: dict[str, float] = field(
        default_factory=dict
    )
    # P2: arbitrates concurrent ACCEPT frames on the depositor side so
    # exactly one candidate wins the deposit and the others get cancelled.
    foreign_storage_auto_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # P8.8: per-deposit count of how many times the host has replied with
    # storage_missing_chunks. Bounded by ``fs_transit_max_retries`` — once
    # we hit the cap the depositor flips to ``failed_in_transit`` instead
    # of looping forever on a host that keeps losing chunks.
    foreign_storage_missing_rounds: dict[str, int] = field(default_factory=dict)
    # Auto-rescue: per-deposit_id flag that the lifecycle pass has already
    # kicked off (or notified about) a rescue, so the 2 s tick doesn't fire
    # the same download / cloud-evict / "can't rescue" notice repeatedly.
    # Value is the reason slug last acted on; cleared when the row leaves
    # an at-risk state.
    foreign_storage_auto_rescue_seen: dict[str, str] = field(
        default_factory=dict
    )
    # Auto-rescue cloud-overflow: while a deposit is being streamed to
    # rclone (local disk full), its ciphertext chunks are handed off here
    # — deposit_id -> asyncio.Queue of (chunk_idx, blob) — instead of
    # being written to disk. Registered/cleared by the streaming task.
    foreign_storage_stream_queues: dict = field(default_factory=dict)

    def reset(self) -> None:
        """Clear every field. Intended for tests; never call at runtime."""
        self.task_queue = asyncio.Queue()
        self.running_task_containers.clear()
        self.running_task_procs.clear()
        self.interrupted_task_ids.clear()
        self.preempted_task_ids.clear()
        self.disrupted_master_tasks.clear()
        self.consent_strikes.clear()
        self.pending_task_offers.clear()
        self.worker_pending_offers.clear()
        self.relay_last_error = ""
        self.active_workers.clear()
        self.inbound_peer_ws.clear()
        self.outbound_master_ws.clear()
        self.worker_cooldown_until.clear()
        self.worker_outcomes.clear()
        self.discovered_peers.clear()
        self.relay_peers.clear()
        self.peer_presence.clear()
        self.task_log_buffers.clear()
        self.metrics.clear()
        self.alerts.clear()
        self.service_records.clear()
        self.service_port_mappings.clear()
        self.service_last_activity.clear()
        self.service_tunnels.clear()
        self.service_watchdog_tasks.clear()
        self.service_snapshot_tasks.clear()
        self.service_standbys.clear()
        self.service_dep_grants.clear()
        self.service_dependents.clear()
        self.service_rate_buckets.clear()
        self.service_http_inspector.clear()
        self.service_replay_buffers.clear()
        self.service_udp_listeners.clear()
        self.foreign_storage_pumps.clear()
        self.foreign_storage_auto_candidates.clear()
        self.foreign_storage_auto_started_at.clear()
        self.foreign_storage_missing_rounds.clear()
        self.foreign_storage_auto_rescue_seen.clear()
        self.foreign_storage_stream_queues.clear()
        self.relay_ws = None
        self.relay_connected = False


STATE: SharedState = SharedState()
"""The process-wide shared state. Import, don't re-instantiate."""
