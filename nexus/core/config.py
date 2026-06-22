"""Live node settings: defaults, normalization, load/save hooks.

Extracted from Phase-1/node_modified.py:

* ``DEFAULT_LOCAL_SETTINGS`` — lines 62-98
* ``normalize_bool`` / ``normalize_list_field`` / ``normalize_local_settings``
  — lines 1305-1311, 1478-1555

Design notes
------------
:data:`LOCAL_SETTINGS` is a **mutable live dict**. Many subpackages read
from it frequently on hot paths (scheduler, worker-client, runtime) so
returning a deep copy every time would be wasteful. Callers must treat
keys as read-only; only :func:`save_local_settings_to_db` and the
settings API handler are allowed to mutate it.

Loading from SQLite (to survive restarts) is wired in Step 3 when the
storage layer is available. Until then, :data:`LOCAL_SETTINGS` starts at
:data:`DEFAULT_LOCAL_SETTINGS` and changes are lost on exit.
"""

from __future__ import annotations

from typing import Any

from nexus.core.identity import generate_random_display_name
from nexus.utils.text import split_csv


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_LOCAL_SETTINGS: dict[str, Any] = {
    "mode": "user",
    "max_ram_pct": 50,
    "data_retention": "delete",
    "gdrive_key": "",
    "node_online": True,
    "sharing_mode": "shared",
    "max_serving_masters": 2,
    "lease_seconds": 30,
    "master_quota_per_origin": 3,
    "retry_backoff_base_sec": 5,
    "worker_cooldown_sec": 20,
    # When True, the scheduler ranks each candidate worker's finished-to-fail
    # ratio above raw fitness so more reliable nodes win. Node-wide default;
    # any task/service/DAG dispatch can override it per-submission.
    "prefer_reliable_workers": False,
    # When True, DAG steps are held at "awaiting_approval" once their deps
    # complete; the user verifies the finished level before the next is assigned.
    # Node-wide default; any DAG dispatch can override it per-submission.
    "step_gate": False,
    "allowed_images": ["python:3.11-slim", "node:20-slim", "gcc:latest"],
    "node_region": "local",
    "node_tags": [],
    "node_gpu": False,
    "max_gpu_pct": 80,
    "native_runtime_enabled": False,
    "audit_retention_days": 7,
    "require_worker_consent": False,
    "consent_timeout_sec": 10,
    "consent_max_strikes": 3,
    "queue_timeout_sec": 0,
    "user_display_name": "",
    "about_me": "",
    "hosted_services": [],
    "relay_server_url": "",
    "relay_grid_key": "",
    "relay_enabled": True,
    "allow_cross_region_workers": True,
    "accept_cross_region_tasks": True,
    "security_profile": "maximum",
    "allow_network_tasks": False,
    "enable_task_scanning": True,
    "hide_profile": True,
    "node_uuid": "",
    "require_venv_isolation": False,
    "cache_venvs": False,
    "max_result_bytes": 100 * 1024 * 1024,
    "max_ws_frame_bytes": 4 * 1024 * 1024,
    "native_sandbox_mode": "auto",
    "benchmark_score": 0.0,
    "benchmark_at": "",
    "idle_auto_accept": False,
    "idle_threshold_sec": 300,
    # Foreign-storage throttle + quota knobs.
    "storage_bw_busy_mbps": 10,
    "storage_bw_idle_mbps": 100,
    # Default pledge is conservative (5 GB). The dynamic capability
    # bit auto-flips off when effective_free_gb drops below this floor, so a
    # node never silently ends up advertising space it doesn't have.
    "storage_max_total_gb": 5,
    "storage_max_per_depositor_gb": 5,
    "foreign_storage_host_terms": "",
    # Opt-out toggle. When False this node still acts as a
    # depositor (you can store on others) but rejects all incoming
    # offers. Keeps the bidirectional intent the user requested.
    "foreign_storage_accept_offers": True,
    # Disk-free safety buffer (GB). Advertised free space never drops
    # below 0 even when the pledge would otherwise allow it.
    "foreign_storage_disk_safety_gb": 1.0,
    # P2: how long an auto-mode FS deposit will wait for the first peer
    # to accept before giving up. After this elapses the offer is
    # withdrawn and the user is asked to retry — we deliberately do not
    # auto-retry because that would spam the network with offers.
    # Clamped to [30, 86400] seconds (max 24 h).
    "fs_auto_offer_timeout_sec": 300,
    # P8: pause/resume knobs for an in-flight foreign-storage transfer
    # that hits a connection blip. ``max_retries`` caps auto-resume
    # attempts before the row is marked failed_in_transit and the user is
    # asked to redo. ``chunk_ack_timeout_sec`` is how long the depositor
    # waits for a per-chunk ack before declaring a stall.
    # ``silence_timeout_sec`` is the host-side counterpart — how long
    # without a new chunk before the host treats the depositor as gone.
    # ``abandoned_chunk_ttl_hours`` is how long the host keeps partial
    # chunks for a deposit that never resumes; max 24 h matches the
    # P1 queue-offline TTL.
    "fs_transit_max_retries": 5,
    "fs_transit_chunk_ack_timeout_sec": 30,
    "fs_transit_silence_timeout_sec": 60,
    "fs_transit_abandoned_chunk_ttl_hours": 24,
    # Batch C: peer UUIDs the user has blocked. Blocked peers are
    # hidden from /local/peers, rejected as task/deposit targets, and
    # their inbound task/deposit offers are dropped.
    "blocked_peer_uuids": [],
    # Host-configurable total countdown (in days) between clicking
    # Evict and the bundle being purged from disk + DB. Min 1 day.
    # Default 3 mirrors the original 1-day response + 2-day grace
    # split.
    "evict_total_days": 3,
    # A1: max total size (bytes) of a custom build context (Dockerfile +
    # bundled files) a replica is allowed to build. Default 5 MB.
    "build_max_bytes": 5 * 1024 * 1024,
    # Auto-rescue: depositor-side automatic salvage of your own deposits
    # when a host starts evicting them (or as TTL nears). Default on so a
    # forgotten deposit isn't silently lost. Two action paths:
    #   * cloud  — if ``fs_auto_rescue_cloud_cred`` names a CloudCredential,
    #     the host streams the (still-encrypted) bundle to your bucket. Fully
    #     unattended — no deposit password needed.
    #   * local  — download to ``fs_auto_rescue_dir``. Only possible while the
    #     deposit key is unlocked in this session (we never persist the
    #     password); otherwise the user is notified so files aren't lost.
    # ``trigger`` is "eviction" (act once the host requests eviction) or
    # "days" (also act while still stored, when TTL is within N days).
    "fs_auto_rescue": True,
    # Recovery order when both a local folder and a cloud target are usable:
    #   folder_then_cloud (default) | cloud_then_folder | folder_only | cloud_only
    "fs_auto_rescue_mode": "folder_then_cloud",
    "fs_auto_rescue_trigger": "eviction",
    "fs_auto_rescue_days": 2,
    "fs_auto_rescue_dir": "",
    "fs_auto_rescue_cloud_cred": "",
    # Cloud-overflow: when a deposit can't be rescued to local disk (full),
    # stream its ciphertext straight into ``rclone rcat`` — never staged
    # locally. An ordered list of rclone targets ("remote:path"); tried in
    # order until one upload succeeds. Empty = no overflow (just warn).
    "fs_auto_rescue_rclone_targets": [],
    # Per-deposit overrides keyed by deposit_id. Each value may carry
    # ``enabled`` (bool), ``rclone_targets`` (list) and ``dir`` (str); any
    # field absent falls back to the node defaults above. Edited from the
    # Auto-rescue button on each "My deposits" row.
    "fs_auto_rescue_overrides": {},
    # DAG #4: node-local saved DAG templates, keyed by name. Each value carries
    # ``steps`` (the blueprint step list), ``description`` and ``created_at``.
    # Managed via /local/dag_templates; surfaced in the Dispatcher.
    "dag_templates": {},
    # Node-local saved dispatch-settings profiles, keyed by name. Each value
    # carries ``settings`` (a snapshot of the resources / scheduling / targeting
    # form fields), ``description`` and ``created_at``. Managed via
    # /local/dispatch_templates; surfaced in the Dispatcher.
    "dispatch_templates": {},
    # D3: outbound webhook subscriptions. A list of
    # ``{id, url, events:[...], secret, enabled, description}``; the node POSTs
    # to ``url`` whenever a subscribed event fires. Managed via /local/webhooks.
    "webhooks": [],
}
"""Every key a fully-initialised ``LOCAL_SETTINGS`` must contain."""


# The live settings dict. Seeded from :data:`DEFAULT_LOCAL_SETTINGS` at import
# time and updated in place by :func:`save_local_settings_to_db` (wired in
# Step 3) or the ``/local/settings`` HTTP handler.
LOCAL_SETTINGS: dict[str, Any] = dict(DEFAULT_LOCAL_SETTINGS)


def get_settings() -> dict[str, Any]:
    """Return the live settings dict.

    **Callers must treat the returned dict as read-only.** See module docstring.
    Tests that need an isolated snapshot should ``copy.deepcopy`` the result.
    """
    return LOCAL_SETTINGS


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

_TRUE_LITERALS: frozenset[str] = frozenset({"1", "true", "yes", "on", "enabled"})


def normalize_bool(value: Any, default: bool = False) -> bool:
    """Coerce *value* (str/int/bool/None) into a bool, falling back to *default*."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_LITERALS
    return default


def normalize_list_field(value: Any) -> list[str]:
    """Normalize a list-shaped setting (accepts list or comma-separated string)."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return split_csv(value)
    return []


def normalize_hosted_services(value: Any) -> list[dict]:
    """Normalize the profile's advertised services (/ 53 / 55).

    A service is a few structured fields (``name, description, version, access,
    tags``) plus a free-form ``readme`` (markdown) where the provider defines
    everything else. ``access`` is ``free`` / ``permission`` / ``paid``. The
    provider-local routing target (``local_host, local_port``) for the data-plane
    tunnel is HOST-ONLY and stripped before the service is served (see
    ``public_services``). Drops entries without a name; caps the list at 50.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()[:80]
        if not name:
            continue
        access = str(item.get("access", "free") or "free").strip().lower()
        if access not in ("free", "permission", "paid"):
            access = "free"
        try:
            local_port = int(item.get("local_port") or 0)
        except (TypeError, ValueError):
            local_port = 0
        local_port = local_port if 0 < local_port < 65536 else 0
        tags = [t.lower() for t in normalize_list_field(item.get("tags", []))][:20]
        components = _normalize_components(item.get("components", []))
        out.append({
            "name": name,
            "description": str(item.get("description", "") or "").strip()[:400],
            "version": str(item.get("version", "") or "").strip()[:40],
            "access": access,
            "tags": tags,
            # Free-form doc — the provider defines EVERYTHING else here (how to
            # connect, what it's built on, recipe/cookbook, links, license,
            # examples). Markdown; rendered on the detail page.
            "readme": str(item.get("readme", "") or "")[:20000],
            # Optional host-side pump name. "" / "default" = the plain
            # byte forwarder; a custom name resolves to a nexus_pumps/ module.
            "pump": str(item.get("pump", "") or "").strip()[:60],
            # Provider opt-in — when True, a consumer may copy this
            # service's cookbook (its public recipe/readme) to their own machine
            # to run it themselves. We never auto-run it; the recipe is the
            # already-public readme, so this just enables the "copy" affordance.
            "replicable": normalize_bool(item.get("replicable", False), False),
            # Optional structured run-spec for AUTO-RUN. Machine-readable
            # (never the free-form readme): a container image / command + env +
            # ports the consumer runs in a sandbox they choose. Public — the
            # consumer needs it to run. Empty {} when the provider didn't supply
            # one, in which case auto-run is unavailable (manual cookbook only).
            "run": _normalize_run_spec(item.get("run")),
            # A composite service bundles sub-services. Each component
            # is independently tunnelled (one grant, a tunnel per component).
            "components": components,
            # Host-only routing target for the data-plane tunnel;
            # never leaves this node (see ``public_services``).
            "local_host": str(item.get("local_host", "") or "").strip()[:120] or "127.0.0.1",
            "local_port": local_port,
            # (DBaaS): public hint for the connection-string template
            # ("postgres"/"redis"/"mysql"/...); "" = not a DB service.
            "service_kind": str(item.get("service_kind", "") or "").strip().lower()[:40],
            # HOST-ONLY DBaaS provider config {engine, admin_dsn}. The
            # admin_dsn holds the DB admin secret, so this is stripped before the
            # service is served (see ``public_services`` / _SERVICE_PRIVATE_FIELDS).
            "db_provider": _normalize_db_provider(item.get("db_provider")),
        })
        if len(out) >= 50:
            break
    return out


# Fields that must NEVER be exposed to a remote peer (routing target + DB admin
# secret). ``db_provider`` carries the admin DSN — strictly host-only.
_SERVICE_PRIVATE_FIELDS = ("local_host", "local_port", "db_provider")


def _normalize_db_provider(value: Any) -> dict:
    """Normalize a service's host-only DBaaS provider config. Keeps
    only ``{engine, admin_dsn}``; empty {} when not configured."""
    if not isinstance(value, dict):
        return {}
    engine = str(value.get("engine", "") or "").strip().lower()[:40]
    admin_dsn = str(value.get("admin_dsn", "") or "").strip()[:500]
    if not engine or not admin_dsn:
        return {}
    return {"engine": engine, "admin_dsn": admin_dsn}


def _normalize_run_spec(value: Any) -> dict:
    """Normalize a service's auto-run spec. All fields optional; an
    empty image means "no auto-run". ``cmd`` is the command (required for the
    raw runner, an optional override for container runners); ``env`` is a list of
    ``KEY=VAL`` strings; ``ports`` are the container ports to publish to the
    consumer's loopback."""
    if not isinstance(value, dict):
        return {}
    image = str(value.get("image", "") or "").strip()[:200]
    cmd = str(value.get("cmd", "") or "").strip()[:500]
    # A1: a build context alone (no prebuilt image, no cmd) is still a valid
    # run-spec — the consumer builds the image locally.
    build = _normalize_build_context(value.get("build"))
    # A2: optional cloud inputs the consumer downloads before the runner runs.
    inputs = _normalize_inputs(value.get("inputs"))
    if not image and not cmd and not build:
        return {}
    env: list[str] = []
    for e in (value.get("env") or [])[:30]:
        e = str(e).strip()
        if "=" in e and len(e) <= 200:
            env.append(e)
    ports: list[int] = []
    for p in (value.get("ports") or [])[:12]:
        try:
            pi = int(p)
        except (TypeError, ValueError):
            continue
        if 0 < pi < 65536 and pi not in ports:
            ports.append(pi)
    spec = {"image": image, "cmd": cmd, "env": env, "ports": ports}
    if build:
        spec["build"] = build
    if inputs:
        spec["inputs"] = inputs
    return spec


def _normalize_inputs(value: Any) -> list[dict]:
    """Normalize A2 cloud inputs: ``[{uri, dest}]``.

    ``uri`` is an ``http(s)://`` link or an rclone ``remote:path``; ``dest`` is a
    sanitized relative path under the runner's inputs dir. Invalid entries are
    dropped; both fields are required.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for it in value[:20]:  # at most 20 inputs
        if not isinstance(it, dict):
            continue
        uri = str(it.get("uri", "") or "").strip()[:1000]
        dest = _safe_build_path(it.get("dest", ""))
        if uri and dest:
            out.append({"uri": uri, "dest": dest})
    return out


def _safe_build_path(rel: str) -> str:
    """Sanitize a build-context file path: relative, no traversal, POSIX."""
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel != rel.strip():
        return ""
    # Reject any colon — a Windows drive letter ("C:/…") or NTFS alternate data
    # stream survives the "/"-split below, and `base / "C:/x"` ESCAPES the base
    # dir on Windows. Security F-009.
    if ":" in rel:
        return ""
    # Reject control characters (NUL etc.) — they can't be valid path
    # components and a NUL would raise mid-build when the file is written.
    if any(ord(c) < 32 for c in rel):
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)[:200]


def _normalize_build_context(value: Any) -> dict:
    """Normalize an A1 build context: ``{dockerfile, files}``.

    Empty/invalid → ``{}`` (no build). Hard upper bounds keep a published
    profile from ballooning; the operator's run-time size cap
    (``build_max_bytes``) is enforced separately in ``replica_runner``.
    """
    if not isinstance(value, dict):
        return {}
    dockerfile = str(value.get("dockerfile", "") or "")
    if not dockerfile.strip():
        return {}
    dockerfile = dockerfile[:65536]  # 64 KB hard cap on the Dockerfile text
    files: dict[str, str] = {}
    raw = value.get("files")
    if isinstance(raw, dict):
        for k, v in list(raw.items())[:64]:  # at most 64 bundled files
            rel = _safe_build_path(k)
            if rel:
                files[rel] = str(v)[:262144]  # 256 KB per file hard cap
    return {"dockerfile": dockerfile, "files": files}


def _normalize_components(value: Any) -> list[dict]:
    """Normalize a composite service's sub-services. Each has a name,
    optional protocol/tags, the host-only local target, and an optional pump."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for c in value:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "") or "").strip()[:60]
        if not name:
            continue
        try:
            port = int(c.get("local_port") or 0)
        except (TypeError, ValueError):
            port = 0
        out.append({
            "name": name,
            "protocol": str(c.get("protocol", "") or "").strip()[:40],
            "tags": [t.lower() for t in normalize_list_field(c.get("tags", []))][:10],
            "pump": str(c.get("pump", "") or "").strip()[:60],
            "local_host": str(c.get("local_host", "") or "").strip()[:120] or "127.0.0.1",
            "local_port": port if 0 < port < 65536 else 0,
        })
        if len(out) >= 12:
            break
    return out


def public_services(services: Any) -> list[dict]:
    """Project hosted services down to the publicly-shareable descriptor —
    strips the host-only routing target so ``local_host:local_port`` never
    leaves this node, for the service AND each composite component (/58)."""
    out: list[dict] = []
    for s in services or []:
        if not isinstance(s, dict):
            continue
        pub = {k: v for k, v in s.items() if k not in _SERVICE_PRIVATE_FIELDS}
        if isinstance(pub.get("components"), list):
            pub["components"] = [
                {k: v for k, v in c.items() if k not in _SERVICE_PRIVATE_FIELDS}
                for c in pub["components"] if isinstance(c, dict)
            ]
        out.append(pub)
    return out


def _clamp_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw or default)))
    except (TypeError, ValueError):
        return default


def normalize_local_settings(settings: dict | None) -> dict[str, Any]:
    """Return a new dict that is :data:`DEFAULT_LOCAL_SETTINGS` + validated overrides.

    Unknown keys are dropped. Out-of-range numeric values are clamped rather
    than rejected — settings can round-trip through older nodes that had
    tighter bounds without error.
    """
    merged: dict[str, Any] = dict(DEFAULT_LOCAL_SETTINGS)
    if isinstance(settings, dict):
        merged.update(settings)

    merged["mode"] = "master" if str(merged.get("mode", "user")) == "master" else "user"
    merged["max_ram_pct"] = _clamp_int(merged.get("max_ram_pct"), 50, 10, 90)
    merged["data_retention"] = (
        "keep" if str(merged.get("data_retention", "delete")) == "keep" else "delete"
    )
    merged["gdrive_key"] = str(merged.get("gdrive_key", ""))
    merged["node_online"] = normalize_bool(merged.get("node_online", True), True)
    merged["sharing_mode"] = (
        "single" if str(merged.get("sharing_mode", "shared")) == "single" else "shared"
    )
    merged["max_serving_masters"] = _clamp_int(merged.get("max_serving_masters"), 2, 1, 8)
    merged["lease_seconds"] = _clamp_int(merged.get("lease_seconds"), 30, 10, 120)
    merged["master_quota_per_origin"] = _clamp_int(
        merged.get("master_quota_per_origin"), 3, 1, 32
    )
    merged["retry_backoff_base_sec"] = _clamp_int(
        merged.get("retry_backoff_base_sec"), 5, 1, 120
    )
    merged["worker_cooldown_sec"] = _clamp_int(
        merged.get("worker_cooldown_sec"), 20, 0, 300
    )
    merged["prefer_reliable_workers"] = normalize_bool(
        merged.get("prefer_reliable_workers", False), False
    )
    merged["step_gate"] = normalize_bool(merged.get("step_gate", False), False)
    merged["allowed_images"] = normalize_list_field(
        merged.get("allowed_images", DEFAULT_LOCAL_SETTINGS["allowed_images"])
    )
    merged["node_region"] = (
        str(merged.get("node_region", "local") or "local").strip() or "local"
    )
    merged["node_tags"] = normalize_list_field(merged.get("node_tags", []))
    merged["node_gpu"] = normalize_bool(merged.get("node_gpu", False), False)
    merged["max_gpu_pct"] = _clamp_int(merged.get("max_gpu_pct"), 80, 10, 95)
    merged["native_runtime_enabled"] = normalize_bool(
        merged.get("native_runtime_enabled", False), False
    )
    merged["audit_retention_days"] = _clamp_int(
        merged.get("audit_retention_days"), 7, 1, 365
    )
    merged["require_worker_consent"] = normalize_bool(
        merged.get("require_worker_consent", False), False
    )
    merged["consent_timeout_sec"] = _clamp_int(
        merged.get("consent_timeout_sec"), 10, 3, 60
    )
    merged["consent_max_strikes"] = _clamp_int(
        merged.get("consent_max_strikes"), 3, 0, 10
    )
    merged["queue_timeout_sec"] = max(
        0, int(merged.get("queue_timeout_sec") or 0)
    )
    display = str(merged.get("user_display_name", "") or "").strip()[:50]
    merged["user_display_name"] = display or generate_random_display_name()
    merged["about_me"] = str(merged.get("about_me", "") or "")[:1000]
    merged["hosted_services"] = normalize_hosted_services(merged.get("hosted_services", []))
    merged["hide_profile"] = normalize_bool(merged.get("hide_profile", True), True)
    merged["node_uuid"] = str(merged.get("node_uuid", "") or "")
    merged["require_venv_isolation"] = normalize_bool(
        merged.get("require_venv_isolation", False), False
    )
    merged["cache_venvs"] = normalize_bool(merged.get("cache_venvs", False), False)
    merged["max_result_bytes"] = _clamp_int(
        merged.get("max_result_bytes"),
        DEFAULT_LOCAL_SETTINGS["max_result_bytes"],
        1 * 1024 * 1024,
        2 * 1024 * 1024 * 1024,
    )
    merged["max_ws_frame_bytes"] = _clamp_int(
        merged.get("max_ws_frame_bytes"),
        DEFAULT_LOCAL_SETTINGS["max_ws_frame_bytes"],
        64 * 1024,
        64 * 1024 * 1024,
    )
    sandbox_raw = str(merged.get("native_sandbox_mode", "auto") or "auto").lower()
    merged["native_sandbox_mode"] = (
        sandbox_raw if sandbox_raw in ("auto", "strict", "off") else "auto"
    )
    try:
        merged["benchmark_score"] = max(0.0, float(merged.get("benchmark_score") or 0.0))
    except (TypeError, ValueError):
        merged["benchmark_score"] = 0.0
    merged["benchmark_at"] = str(merged.get("benchmark_at", "") or "")
    merged["idle_auto_accept"] = normalize_bool(
        merged.get("idle_auto_accept", False), False
    )
    merged["idle_threshold_sec"] = _clamp_int(
        merged.get("idle_threshold_sec"), 300, 30, 86_400
    )
    merged["blocked_peer_uuids"] = normalize_list_field(
        merged.get("blocked_peer_uuids", [])
    )
    # P2: clamp to [30 s, 24 h]. Anything outside this falls back to default.
    merged["fs_auto_offer_timeout_sec"] = _clamp_int(
        merged.get("fs_auto_offer_timeout_sec"), 300, 30, 86_400
    )
    # P8: pause/resume transit knobs — all clamped so a fat-fingered
    # setting can't lock the user out or spam the network.
    merged["fs_transit_max_retries"] = _clamp_int(
        merged.get("fs_transit_max_retries"), 5, 1, 20
    )
    merged["fs_transit_chunk_ack_timeout_sec"] = _clamp_int(
        merged.get("fs_transit_chunk_ack_timeout_sec"), 30, 5, 300
    )
    merged["fs_transit_silence_timeout_sec"] = _clamp_int(
        merged.get("fs_transit_silence_timeout_sec"), 60, 10, 600
    )
    merged["fs_transit_abandoned_chunk_ttl_hours"] = _clamp_int(
        merged.get("fs_transit_abandoned_chunk_ttl_hours"), 24, 1, 24
    )
    merged["build_max_bytes"] = _clamp_int(
        merged.get("build_max_bytes"), 5 * 1024 * 1024, 64 * 1024, 100 * 1024 * 1024
    )
    # Auto-rescue knobs.
    merged["fs_auto_rescue"] = normalize_bool(
        merged.get("fs_auto_rescue", True), True
    )
    _mode = str(merged.get("fs_auto_rescue_mode", "folder_then_cloud") or "").lower()
    merged["fs_auto_rescue_mode"] = (
        _mode if _mode in (
            "folder_then_cloud", "cloud_then_folder", "folder_only", "cloud_only"
        ) else "folder_then_cloud"
    )
    trigger = str(merged.get("fs_auto_rescue_trigger", "eviction") or "eviction").lower()
    merged["fs_auto_rescue_trigger"] = (
        trigger if trigger in ("eviction", "days") else "eviction"
    )
    merged["fs_auto_rescue_days"] = _clamp_int(
        merged.get("fs_auto_rescue_days"), 2, 1, 30
    )
    merged["fs_auto_rescue_dir"] = str(merged.get("fs_auto_rescue_dir", "") or "")
    merged["fs_auto_rescue_cloud_cred"] = str(
        merged.get("fs_auto_rescue_cloud_cred", "") or ""
    )
    merged["fs_auto_rescue_rclone_targets"] = normalize_list_field(
        merged.get("fs_auto_rescue_rclone_targets", [])
    )
    merged["fs_auto_rescue_overrides"] = _normalize_auto_rescue_overrides(
        merged.get("fs_auto_rescue_overrides", {})
    )
    merged["dag_templates"] = _normalize_dag_templates(merged.get("dag_templates", {}))
    merged["dispatch_templates"] = _normalize_dispatch_templates(
        merged.get("dispatch_templates", {})
    )
    merged["webhooks"] = _normalize_webhooks(merged.get("webhooks", []))
    return merged


def _normalize_dag_templates(value: Any) -> dict[str, dict]:
    """Sanitize the saved DAG-template map: ``name -> {steps, description,
    created_at}``. Drops junk; caps template count, step count, and field sizes
    so a bad import can't balloon the settings blob."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for name, tpl in list(value.items())[:50]:  # at most 50 templates
        if not isinstance(name, str) or not name.strip() or not isinstance(tpl, dict):
            continue
        steps = tpl.get("steps")
        if not isinstance(steps, list):
            continue
        clean_steps = [s for s in steps if isinstance(s, dict)][:100]  # cap steps
        out[name.strip()[:80]] = {
            "steps": clean_steps,
            "description": str(tpl.get("description", "") or "")[:200],
            "created_at": str(tpl.get("created_at", "") or ""),
        }
    return out


def _normalize_dispatch_templates(value: Any) -> dict[str, dict]:
    """Sanitize the saved dispatch-settings profiles: ``name -> {settings,
    description, created_at}``. ``settings`` is a free-form snapshot of the
    Dispatcher form fields (the node's own data) kept only if it's a dict; junk
    is dropped and the template count + field sizes are capped."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for name, tpl in list(value.items())[:50]:  # at most 50 profiles
        if not isinstance(name, str) or not name.strip() or not isinstance(tpl, dict):
            continue
        settings = tpl.get("settings")
        if not isinstance(settings, dict):
            continue
        out[name.strip()[:80]] = {
            "settings": settings,
            "description": str(tpl.get("description", "") or "")[:200],
            "created_at": str(tpl.get("created_at", "") or ""),
        }
    return out


def _normalize_webhooks(value: Any) -> list[dict]:
    """Sanitize the saved webhook subscriptions: a list of
    ``{id, url, events, secret, enabled, description}``. Drops junk, keeps only
    http/https URLs, caps the count + field sizes so a bad import can't balloon
    the settings blob."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for hook in value[:50]:  # at most 50 subscriptions
        if not isinstance(hook, dict):
            continue
        url = str(hook.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        events = hook.get("events")
        if not isinstance(events, list):
            events = []
        clean_events = [str(e).strip()[:80] for e in events if str(e).strip()][:32]
        out.append(
            {
                "id": str(hook.get("id") or "").strip()[:64],
                "url": url[:500],
                "events": clean_events,
                "secret": str(hook.get("secret") or "")[:200],
                "enabled": normalize_bool(hook.get("enabled"), True),
                "description": str(hook.get("description", "") or "")[:200],
            }
        )
    return out


def _normalize_auto_rescue_overrides(value: Any) -> dict[str, dict]:
    """Sanitize the per-deposit override map (drop junk; coerce fields)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for dep_id, ov in value.items():
        if not isinstance(dep_id, str) or not isinstance(ov, dict):
            continue
        entry: dict[str, Any] = {}
        if "enabled" in ov:
            entry["enabled"] = normalize_bool(ov.get("enabled"), True)
        if "mode" in ov:
            m = str(ov.get("mode") or "").lower()
            if m in ("folder_then_cloud", "cloud_then_folder", "folder_only", "cloud_only"):
                entry["mode"] = m
        if "trigger" in ov:
            t = str(ov.get("trigger") or "").lower()
            if t in ("eviction", "days"):
                entry["trigger"] = t
        if "days" in ov:
            try:
                entry["days"] = max(1, min(30, int(ov.get("days"))))
            except (TypeError, ValueError):
                pass
        if "cloud_cred" in ov:
            entry["cloud_cred"] = str(ov.get("cloud_cred") or "")
        if "rclone_targets" in ov:
            entry["rclone_targets"] = normalize_list_field(ov.get("rclone_targets"))
        if "dir" in ov:
            entry["dir"] = str(ov.get("dir") or "")
        if entry:
            out[dep_id[:128]] = entry
    return out


def effective_auto_rescue(deposit_id: str) -> dict[str, Any]:
    """Per-deposit auto-rescue config, override merged over node defaults.

    Returns ``{enabled, mode, trigger, days, cloud_cred, rclone_targets, dir,
    is_override}`` — every element overridable per deposit, each falling back
    to the node default. The lifecycle pass, the rescue path-helpers and the
    config endpoint all read through here so they agree on what's in effect.
    """
    ov = (LOCAL_SETTINGS.get("fs_auto_rescue_overrides") or {}).get(deposit_id) or {}
    enabled = (
        normalize_bool(ov["enabled"], True)
        if "enabled" in ov
        else normalize_bool(LOCAL_SETTINGS.get("fs_auto_rescue", True), True)
    )
    mode = ov.get("mode") or str(
        LOCAL_SETTINGS.get("fs_auto_rescue_mode", "folder_then_cloud") or "folder_then_cloud"
    )
    trigger = ov.get("trigger") or str(
        LOCAL_SETTINGS.get("fs_auto_rescue_trigger", "eviction") or "eviction"
    )
    days = int(ov["days"]) if ov.get("days") else int(
        LOCAL_SETTINGS.get("fs_auto_rescue_days", 2) or 2
    )
    cloud_cred = (
        ov["cloud_cred"]
        if "cloud_cred" in ov
        else str(LOCAL_SETTINGS.get("fs_auto_rescue_cloud_cred", "") or "")
    )
    targets = ov.get("rclone_targets")
    if not targets:
        targets = list(LOCAL_SETTINGS.get("fs_auto_rescue_rclone_targets") or [])
    rescue_dir = str(ov.get("dir") or "").strip() or str(
        LOCAL_SETTINGS.get("fs_auto_rescue_dir", "") or ""
    )
    return {
        "enabled": enabled,
        "mode": mode,
        "trigger": trigger,
        "days": days,
        "cloud_cred": cloud_cred,
        "rclone_targets": list(targets),
        "dir": rescue_dir,
        "is_override": bool(ov),
    }
