"""Backward-compatibility shim for the old ``node_modified`` monolith.

The package under :mod:`nexus` is the canonical codebase; this file exists only
so older imports written against the original ``node_modified`` module keep
resolving. Every symbol re-exported below has a canonical home under
:mod:`nexus`; new code should import from there, not from here.

Submodule map:

* :mod:`nexus.core`        — ``LOCAL_SETTINGS``, ``NODE_UUID``, ``STATE``,
                             identity helpers, events bus.
* :mod:`nexus.storage`     — ``TaskRecord``, ``Peer``, ``get_session``,
                             settings IO.
* :mod:`nexus.tasks`       — lifecycle, queue, lease, metadata, shadow rows.
* :mod:`nexus.caches`      — venv/pip/node caches + scanner + prewarm.
* :mod:`nexus.runtime`     — executor, capacity, docker/native helpers,
                             worker state, process-tree kill, workspace.
* :mod:`nexus.scheduler`   — fitness, selection, manifest cache, DAG + retry
                             loops.
* :mod:`nexus.networking`  — discovery, gossip, peer protocol, connection
                             manager, peer HTTP, worker-client, relay client.
* :mod:`nexus.telemetry`   — logs, metrics, alerts, audit, presence,
                             hardware, rollup, zombie sweeper, observability.
* :mod:`nexus.security`    — auth, crypto, tokens, threat scanner, profiles.
* :mod:`nexus.api`         — HTTP routers (``/peer``, ``/local``, WS).
* :mod:`nexus.ui`          — UI serve, avatar, broadcaster.
* :mod:`nexus.app`         — ``create_app`` factory.
"""

from __future__ import annotations

# --- Core ---------------------------------------------------------------
from nexus.core import (  # noqa: F401
    ALLOWED_TRANSITIONS,
    DEFAULT_BIND_HOST,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_GRID_KEY,
    DEFAULT_HTTP_PORT,
    DEFAULT_LOCAL_SETTINGS,
    LOCAL_SETTINGS,
    MAX_LOG_LINES,
    NODE_UUID,
    PEER_PRESENCE_TIMEOUT,
    STATE,
    TASK_STATES,
    TERMINAL_STATES,
    cache_dir,
    events,
    fmt_peer,
    get_node_identity,
    get_node_port,
    get_or_create_node_uuid,
    get_settings,
    normalize_bool,
    normalize_list_field,
    normalize_local_settings,
    register_peer_uuid,
    resolve_ip_to_uuid,
    resolve_uuid_to_ip,
    set_node_port,
)

# --- Storage ------------------------------------------------------------
from nexus.storage import (  # noqa: F401
    AuditEvent,
    LocalConfigRecord,
    Peer,
    PresenceEvent,
    TaskRecord,
    get_session,
    init_db,
    load_local_settings_from_db,
    persist_resolved_ip,
    save_local_settings_to_db,
    seed_identity_mappings,
)

# --- Security -----------------------------------------------------------
from nexus.security import (  # noqa: F401
    get_docker_security_opts,
    get_local_api_token,
    get_signing_secret,
    resolve_trusted_peer,
    scan_workspace_for_threats,
    sign_bye,
    sign_bytes,
    verify_bye,
    verify_local_auth,
    verify_signature,
    verify_trusted_peer,
)

# --- Tasks --------------------------------------------------------------
from nexus.tasks import (  # noqa: F401
    add_task_timeline_event,
    build_task_metadata,
    dequeue_task,
    enqueue_task,
    extract_task_metadata,
    get_retry_policy,
    is_task_interrupted,
    is_task_preempted,
    mark_task_interrupted,
    mark_task_preempted,
    parse_task_env,
    queue_depth,
    queue_empty,
    refresh_task_lease,
    set_retry_policy,
    set_task_lease,
    set_task_status,
    task_created_at,
    task_lease_expired,
    task_priority,
    task_retry_at,
    try_schedule_retry,
    upsert_remote_shadow_task,
    write_task_env,
)

# --- Caches -------------------------------------------------------------
from nexus.caches import (  # noqa: F401
    PREWARM_JOBS,
    detect_language_from_entrypoint,
    detect_uv,
    extract_imports_from_source,
    extract_js_imports,
    node_cache_key,
    node_cache_root,
    pip_wheel_cache_dir,
    prewarm_job_append,
    prewarm_job_set,
    run_prewarm,
    scan_workspace_cpp,
    scan_workspace_dependencies,
    scan_workspace_imports,
    scan_workspace_js,
    venv_cache_key,
    venv_cache_root,
)

# --- Runtime ------------------------------------------------------------
from nexus.runtime import (  # noqa: F401
    can_pull_more_tasks,
    can_pull_task_from_master,
    clear_local_task,
    docker_available,
    docker_security_opts,
    execute_bundle_with_watchdog,
    get_dispatch_capacity_mb,
    get_docker_client,
    get_local_worker_snapshot,
    image_allowed,
    interrupt_running_task,
    kill_process_tree,
    kill_task_native_proc,
    local_capabilities,
    mark_local_task_result,
    mark_local_task_running,
    preempt_running_task,
    refresh_worker_task_leases,
    register_running_container,
    register_running_proc,
    reset_docker_client,
    resolve_p2p_cache,
    set_connected_masters_hook,
    snapshot_proc_children,
    task_required_caps,
    unregister_running_container,
    unregister_running_proc,
    update_local_task_children,
    update_local_task_stage,
)

# --- Scheduler ----------------------------------------------------------
from nexus.scheduler import (  # noqa: F401
    clear_manifest_cache,
    dag_scheduler_loop,
    read_task_manifest,
    retry_scheduler_loop,
    select_task_for_worker,
    start_scheduler_loops,
    worker_fit_score,
    worker_supports_task,
)

# --- Networking ---------------------------------------------------------
from nexus.networking import (  # noqa: F401
    ConnectionManager,
    UDPDiscoveryProtocol,
    check_join_rate_limit,
    get_connected_master_peers,
    get_grid_key,
    get_relay_url,
    gossip_broadcaster_loop,
    master_manager_loop,
    open_worker_websocket,
    peer_http_post,
    relay_client_loop,
    relay_http_request,
    relay_send,
    relay_send_to_peer,
    set_grid_key_provider,
    set_relay_cli_overrides,
    sign_join_request,
    start_discovery,
    start_worker_client,
    verify_join_hmac,
    ws_manager,
)
from nexus.networking.discovery import lookup_discovered_peer  # noqa: F401
from nexus.networking.worker_client import worker_client_process  # noqa: F401

# --- Telemetry ----------------------------------------------------------
from nexus.telemetry import (  # noqa: F401
    LONG_RUN_WARN_SEC,
    LogStream,
    analyze_cluster_health,
    clear_local_task_log,
    compute_cluster_rollup,
    detect_gpu,
    get_gpu_stats,
    gpu_vendor,
    incr_metric,
    observability_loop,
    presence,
    push_alert,
    record_audit_event,
    sample_net_bandwidth,
    snapshot_alerts,
    snapshot_metrics,
    task_log_append,
    task_log_tail,
    write_audit_event,
    zombie_sweeper,
)
from nexus.telemetry.presence import (  # noqa: F401
    is_peer_offline,
    mark_peer_offline,
    mark_peer_online,
)

# --- Utils --------------------------------------------------------------
from nexus.utils import (  # noqa: F401
    MASKED_IP_PLACEHOLDER,
    content_hash,
    dir_size_bytes,
    env_flag,
    format_elapsed,
    get_local_ip,
    mask_ips_in_log,
    now_epoch,
    prepare_multiline_command,
    safe_extractall,
    sanitize_shell_token,
    split_csv,
    stable_hash,
    timestamp,
)

# --- UI -----------------------------------------------------------------
from nexus.ui import broadcast_ui_update, mount_ui  # noqa: F401

# --- App factory --------------------------------------------------------
from nexus.app import create_app  # noqa: F401


# Underscore aliases kept for callers that hard-code the private name.
_get_docker_security_opts = docker_security_opts


__all__ = [name for name in globals() if not name.startswith("_")]
