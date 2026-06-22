# caches — dependency caches + workspace scanning

## What this owns

The three on-disk caches that make repeated task dispatches fast, and the scanner
that figures out what dependencies a task actually needs:

- `nexus_venv_cache/` — pre-built, hash-keyed Python virtual environments.
- `nexus_pip_cache/` — shared wheel cache across all venvs.
- `nexus_node_cache/` — extracted Node.js scanner output.
- **Scanner** — walks a workspace and classifies language + imports so the
  executor can prepare the right environment without running user code.

Everything here is stateless w.r.t. the task in flight — caches are content-
addressed by hash, so concurrent tasks can share entries without coordination.

## Public surface

Exports from `nexus.caches`:

- `venv_cache_root(port)`, `venv_cache_key(requirements)`.
- `pip_wheel_cache_dir(port)`.
- `node_cache_root(port)`, `node_cache_key(package_json_hash)`.
- `scan_workspace_dependencies(workspace)`, `scan_workspace_imports(...)`,
  `scan_workspace_js(...)`, `scan_workspace_cpp(...)`.
- `extract_imports_from_source(src)`, `extract_js_imports(src)`,
  `detect_language_from_entrypoint(entry)`, `detect_uv()`.
- Prewarm state + driver: `PREWARM_JOBS`, `prewarm_job_set`,
  `prewarm_job_append`, `run_prewarm`.

## Dependencies

- Imports from: `nexus.core`, `nexus.utils`, `nexus.telemetry`.
- Imported by: `runtime`, `scheduler` (capacity estimation), `api` (cache admin).

Forbidden: `runtime`, `scheduler`, `networking`, `api`. Scanning is synchronous
and CPU-bound; if you want to wrap it in a thread pool, do that in the caller
(`runtime`), not here.

## Extending

- **New language**: add a scanner module next to `scanner.py` and register it in
  the language-detection switch. Don't bloat `scanner.py` with every language.
- **New cache shape**: create a new file (e.g. `rust_cache.py`) and expose an
  `__init__.py` entry. Keep the cache root path in `paths.py`.
- **Eviction policy**: currently content-addressed with no eviction. Add a
  `gc.py` module if we ever need bounded caches — do not scatter eviction logic.

## Key files

| File         | Purpose                                                 |
|--------------|---------------------------------------------------------|
| `paths.py`   | Cache root directories + hash-key derivation            |
| `scanner.py` | `scan_workspace_*`, import extractors, language detect  |
| `prewarm.py` | Background prewarm job state + `run_prewarm` coroutine  |
