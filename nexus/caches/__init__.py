"""venv, pip, and node caches + workspace dependency scanning.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.caches.paths import (
    detect_uv,
    node_cache_key,
    node_cache_root,
    pip_wheel_cache_dir,
    venv_cache_key,
    venv_cache_root,
)
from nexus.caches.prewarm import (
    PREWARM_JOBS,
    job_append as prewarm_job_append,
    job_set as prewarm_job_set,
    run_prewarm,
)
from nexus.caches.scanner import (
    detect_language_from_entrypoint,
    extract_imports_from_source,
    extract_js_imports,
    scan_workspace_cpp,
    scan_workspace_dependencies,
    scan_workspace_imports,
    scan_workspace_js,
)

__all__ = [
    # paths
    "venv_cache_root",
    "pip_wheel_cache_dir",
    "node_cache_root",
    "venv_cache_key",
    "node_cache_key",
    "detect_uv",
    # scanner
    "extract_imports_from_source",
    "extract_js_imports",
    "scan_workspace_imports",
    "scan_workspace_js",
    "scan_workspace_cpp",
    "detect_language_from_entrypoint",
    "scan_workspace_dependencies",
    # prewarm
    "PREWARM_JOBS",
    "prewarm_job_set",
    "prewarm_job_append",
    "run_prewarm",
]
