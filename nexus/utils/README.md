# utils — leaf helpers used everywhere

## What this owns

Small, pure-Python helpers that every other layer needs: time formatting, content
hashing, text sanitization, and local-host network discovery. `utils` has no
NexusGrid-specific domain knowledge — anything that would grow domain logic
belongs in `core` or another subpackage instead.

## Public surface

Exports from `nexus.utils`:

- `timestamp()` — monotonic + wall-clock pair for logs/audit
- `now_epoch()` — wall-clock seconds since epoch
- `format_elapsed(seconds)` — human-readable duration
- `content_hash(bytes|str)` — stable SHA-256 digest for cache keys
- `stable_hash(obj)` — deterministic hash of JSON-serializable objects
- `sanitize_shell_token(s)` — reject shell metacharacters
- `mask_ips_in_log(s, mapping)` / `MASKED_IP_PLACEHOLDER` — swap real IPs for display aliases
- `split_csv(s)` — trimmed comma-split of settings strings
- `prepare_multiline_command(cmd)` — normalize multi-line shell commands
- `safe_extractall(archive, target)` — tar/zip extract that blocks path traversal
- `get_local_ip()` — best-guess local LAN IP
- `is_private_or_loopback_host(host)` — RFC1918 / loopback / link-local test
- `client_host(request)` — extract client host from a FastAPI/Starlette request
- `env_flag(name, default)` — parse a boolean env-var
- `dir_size_bytes(path)` — recursive directory size

Everything else in these modules is internal.

## Dependencies

- Imports from: stdlib only. **No other `nexus.*` module.**
- Imported by: every other nexus subpackage.

Violating this (e.g. importing from `nexus.core`) would create a cycle because
`nexus.core` imports from here.

## Extending

- New helper? Ask first whether it is *actually* generic. If it needs settings,
  a DB session, or the event bus, it belongs in `core` or a domain package.
- Keep each module narrow and alphabetical. Don't add a `misc.py`.
- Every exported helper should have a docstring with a one-line summary.

## Key files

| File        | Purpose                                               |
|-------------|-------------------------------------------------------|
| `time.py`   | Timestamp and duration helpers                        |
| `hashing.py`| Content and object hashing                            |
| `text.py`   | String sanitization, escape, IP masking               |
| `net.py`    | Local IP resolution, private-range checks, probes     |
| `fs.py`     | `dir_size_bytes` + related filesystem helpers         |
