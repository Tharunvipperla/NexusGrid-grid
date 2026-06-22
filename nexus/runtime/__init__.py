"""Task execution: Docker, native, WASM runtimes + workspace + process-tree kill.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.runtime.capacity import (
    can_pull_more_tasks,
    can_pull_task_from_master,
    get_dispatch_capacity_mb,
    image_allowed,
    local_capabilities,
    refresh_worker_task_leases,
    task_required_caps,
)
from nexus.runtime.docker_client import (
    docker_available,
    docker_security_opts,
    get_docker_client,
    reset_docker_client,
)
from nexus.runtime.executor import execute_bundle_with_watchdog
from nexus.runtime.process_tree import (
    kill_process_tree,
    kill_task_native_proc,
    snapshot_proc_children,
)
from nexus.runtime.worker_state import (
    clear_local_task,
    get_local_worker_snapshot,
    interrupt_running_task,
    mark_local_task_result,
    mark_local_task_running,
    preempt_running_task,
    register_running_container,
    register_running_proc,
    set_connected_masters_hook,
    unregister_running_container,
    unregister_running_proc,
    update_local_task_children,
    update_local_task_stage,
)
from nexus.runtime.workspace import resolve_p2p_cache

__all__ = [
    # capacity
    "get_dispatch_capacity_mb",
    "can_pull_more_tasks",
    "can_pull_task_from_master",
    "refresh_worker_task_leases",
    "image_allowed",
    "local_capabilities",
    "task_required_caps",
    # docker
    "get_docker_client",
    "reset_docker_client",
    "docker_security_opts",
    "docker_available",
    # process tree
    "kill_process_tree",
    "kill_task_native_proc",
    "snapshot_proc_children",
    # worker state
    "set_connected_masters_hook",
    "get_local_worker_snapshot",
    "mark_local_task_running",
    "update_local_task_stage",
    "update_local_task_children",
    "clear_local_task",
    "mark_local_task_result",
    "register_running_container",
    "unregister_running_container",
    "register_running_proc",
    "unregister_running_proc",
    "interrupt_running_task",
    "preempt_running_task",
    # workspace
    "resolve_p2p_cache",
    # executor
    "execute_bundle_with_watchdog",
]
