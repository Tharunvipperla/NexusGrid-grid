"""Logs, metrics, alerts, audit, presence, hardware sampling.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.telemetry.alerts import push_alert, snapshot_alerts
from nexus.telemetry.audit import record_audit_event, write_audit_event
from nexus.telemetry.hardware import (
    detect_gpu,
    get_gpu_stats,
    gpu_vendor,
    sample_net_bandwidth,
)
from nexus.telemetry.observability import observability_loop
from nexus.telemetry.rollup import (
    LONG_RUN_WARN_SEC,
    analyze_cluster_health,
    compute_cluster_rollup,
)
from nexus.telemetry.zombie_sweeper import zombie_sweeper
from nexus.telemetry.logs import (
    LogStream,
    clear_local_task_log,
    task_log_append,
    task_log_tail,
)
from nexus.telemetry.metrics import (
    KNOWN_METRICS,
    get_metric,
    incr_metric,
    snapshot_metrics,
)
from nexus.telemetry import presence

__all__ = [
    # logs
    "LogStream",
    "task_log_append",
    "task_log_tail",
    "clear_local_task_log",
    # metrics
    "KNOWN_METRICS",
    "incr_metric",
    "get_metric",
    "snapshot_metrics",
    # alerts
    "push_alert",
    "snapshot_alerts",
    # audit
    "record_audit_event",
    "write_audit_event",
    # presence (submodule; import explicitly for functions)
    "presence",
    # hardware
    "detect_gpu",
    "gpu_vendor",
    "get_gpu_stats",
    "sample_net_bandwidth",
    # rollup
    "compute_cluster_rollup",
    "analyze_cluster_health",
    "LONG_RUN_WARN_SEC",
    # background loops
    "observability_loop",
    "zombie_sweeper",
]
