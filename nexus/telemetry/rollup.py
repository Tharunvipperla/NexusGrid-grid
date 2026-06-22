"""Cluster rollup + rules-based health analyzer.

Extracted from node_modified.py:

* ``compute_cluster_rollup`` — lines 1916-1943
* ``analyze_cluster_health`` — lines 1946-2004

Both are pure functions over the ``net`` dict returned by
``/local/network``. They are kept together because the health analyzer
reads the same rollup shape the UI also consumes.

The health analyzer also pulls two process-wide bits of state:

* :data:`STATE.alerts` — recent alerts (checked for ``critical`` severity).
* Relay connection status — exposed via
  :func:`nexus.networking.relay_client.relay_status` once the relay
  client lands (Step 9); until then the analyzer safely no-ops that branch
  via a ``try/except NameError`` mirror of the original implementation's behaviour.
"""

from __future__ import annotations

import time

from nexus.core import STATE

# the original implementation constant — surfaced through this module because rollup is the
# sole consumer today. Moved here to avoid adding another top-level in
# :mod:`nexus.core.constants` for a single use site.
LONG_RUN_WARN_SEC = 1800


def compute_cluster_rollup(net: dict) -> dict:
    """Aggregate local + worker stats into a single row for the UI header."""
    local = net.get("local_worker") or {}
    workers = net.get("workers") or {}
    total_cpu = float(local.get("cpu", 0) or 0)
    total_ram_pct = float(local.get("ram", 0) or 0)
    total_active_tasks = int(local.get("active_task_count", 0) or 0)
    total_sent = float((local.get("net_io") or {}).get("sent_per_sec", 0) or 0)
    total_recv = float((local.get("net_io") or {}).get("recv_per_sec", 0) or 0)
    online_count = 1
    for _ip, w in workers.items():
        if not w.get("online"):
            continue
        online_count += 1
        total_cpu += float(w.get("cpu", 0) or 0)
        total_ram_pct += float(w.get("ram", 0) or 0)
        total_active_tasks += int(w.get("active_task_count", 0) or 0)
        io = w.get("net_io") or {}
        total_sent += float(io.get("sent_per_sec", 0) or 0)
        total_recv += float(io.get("recv_per_sec", 0) or 0)
    return {
        "node_count": 1 + len(workers),
        "online_count": online_count,
        "avg_cpu_pct": round(total_cpu / max(1, online_count), 1),
        "avg_ram_pct": round(total_ram_pct / max(1, online_count), 1),
        "total_active_tasks": total_active_tasks,
        "total_net_sent_per_sec": total_sent,
        "total_net_recv_per_sec": total_recv,
    }


def analyze_cluster_health(net: dict) -> list[dict]:
    """Rules-based anomaly detector. Returns ``[{severity, code, message, node}]``."""
    out: list[dict] = []
    local = net.get("local_worker") or {}
    workers = net.get("workers") or {}
    now = time.time()

    if float(local.get("ram", 0) or 0) > 90:
        out.append({
            "severity": "critical",
            "code": "LOCAL_RAM_HIGH",
            "message": f"Local RAM at {float(local.get('ram', 0)):.0f}% — tasks may be preempted.",
            "node": "local",
        })
    if float(local.get("cpu", 0) or 0) > 95:
        out.append({
            "severity": "warning",
            "code": "LOCAL_CPU_SATURATED",
            "message": f"Local CPU saturated at {float(local.get('cpu', 0)):.0f}%.",
            "node": "local",
        })
    gs = local.get("gpu_stats") or {}
    if gs.get("gpu_mem_used_mb") and gs.get("gpu_mem_total_mb"):
        vram_pct = (gs["gpu_mem_used_mb"] / gs["gpu_mem_total_mb"]) * 100
        if vram_pct > 92:
            out.append({
                "severity": "critical",
                "code": "LOCAL_VRAM_HIGH",
                "message": f"GPU VRAM at {vram_pct:.0f}%.",
                "node": "local",
            })

    for ip, w in workers.items():
        label = w.get("display_ip") or ip
        if not w.get("online"):
            last_seen = float(w.get("last_seen", 0) or 0)
            if last_seen > 0 and (now - last_seen) < 300:
                out.append({
                    "severity": "warning",
                    "code": "WORKER_OFFLINE_RECENT",
                    "message": f"Worker {label} went offline {int(now - last_seen)}s ago.",
                    "node": label,
                })
            continue
        if float(w.get("ram", 0) or 0) > 92:
            out.append({
                "severity": "warning",
                "code": "WORKER_RAM_HIGH",
                "message": f"{label} RAM at {float(w.get('ram', 0)):.0f}%.",
                "node": label,
            })
        if float(w.get("cpu", 0) or 0) > 97:
            out.append({
                "severity": "warning",
                "code": "WORKER_CPU_SATURATED",
                "message": f"{label} CPU at {float(w.get('cpu', 0)):.0f}%.",
                "node": label,
            })

    for t in (local.get("active_tasks") or []):
        started = float(t.get("started_at", 0) or 0)
        if started > 0 and (now - started) > LONG_RUN_WARN_SEC:
            out.append({
                "severity": "warning",
                "code": "TASK_LONG_RUN",
                "message": f"Task {t.get('task_id')} running {int((now - started) / 60)} min.",
                "node": "local",
            })

    # Relay status — only meaningful once the relay client is ported (Step 9).
    # Until then ``STATE.relay_connected`` stays False, and the "last error"
    # key is absent, so this branch is a no-op. The try/except mirrors the
    # the original implementation ``NameError`` guard.
    try:
        if not STATE.relay_connected and getattr(STATE, "relay_last_error", ""):
            out.append({
                "severity": "warning",
                "code": "RELAY_DOWN",
                "message": f"Relay disconnected: {STATE.relay_last_error}",
                "node": "relay",
            })
    except Exception:
        pass

    for a in list(STATE.alerts)[-10:]:
        if isinstance(a, dict) and a.get("severity") == "critical":
            out.append({
                "severity": "critical",
                "code": a.get("code", "ALERT"),
                "message": a.get("message", ""),
                "node": "alert",
            })

    return out


__all__ = [
    "compute_cluster_rollup",
    "analyze_cluster_health",
    "LONG_RUN_WARN_SEC",
]
