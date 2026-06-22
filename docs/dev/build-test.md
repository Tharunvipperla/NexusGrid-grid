# Build, Test & Contribute

---

## Running for development

```bash
# from the repository root
pip install -e .[test]          # runtime deps + test tooling (pytest, hypothesis, pip-audit, bandit)
python -m nexus                 # start a node (UI auto-opens)
python -m nexus --port 8001 --host 127.0.0.1 --no-browser   # a second, loopback-only node
```
Python changes require a restart. The UI bundle is served fresh from disk, so a
rebuild is picked up on reload.

### Multi-node testing on one machine
Run several nodes on different ports; each keeps its own `nexus_mod_<port>.db` and
`nexus_cache_<port>/`. Pair them or put them in a group to exercise the peer paths.

---

## The UI (`webui/`)

React + esbuild, no framework runtime beyond React.
```bash
cd webui
npm install
npm run build      # esbuild src/app.jsx -> dist/bundle.js (gitignored)
npm test           # node --test (e.g. the DAG graph helpers)
```
`dist/bundle.js` is **not** committed — rebuild it after changing `webui/src`.
The node serves it from disk with the local token injected.

---

## Tests

```bash
# from the repository root
python -m pytest -q                       # full suite (~1360 tests)
python -m pytest tests/test_backup.py -q  # one file
```
- **Every change ships a committed regression test.** For a bug/vuln, write a test
  that *reproduces* it first, then fix until green.
- Security tests live alongside the suite (`test_security_*`, `test_fuzz_security.py`,
  `test_*authz*`). The fuzz tests use **hypothesis** (property-based).
- The suite must be green before merge; it's also run repeatedly as a soak (hypothesis
  reseeds each run).

### Security tooling
```bash
python -m pip_audit            # dependency CVEs (keep clean)
python -m bandit -r nexus -ll  # static security lint (triage medium+; many are benign — see SECURITY_FINDINGS.md)
```

---

## Packaging (PyInstaller)

The node packages into a single executable via `build/NexusGrid.spec`.
- Runtime **data files** (e.g. `nexus/CHANGELOG.md`) must be listed in the spec's
  `datas` and resolved at runtime via `get_resource_dir()` — **not** `__file__`,
  which breaks inside a PyInstaller bundle.
- Keep the loopback-only invariants intact in any packaged entry point.

---

## Conventions
- **Surgical changes**: touch only what the task needs; match surrounding style;
  don't refactor unrelated code.
- **Simplicity first**: the minimum that solves the problem; no speculative
  abstractions.
- **Settings**: add to `DEFAULT_LOCAL_SETTINGS` + a `_normalize_*` (see
  [architecture](architecture.md#settings-model)).
- **Schema**: additive migrations only; bump `SCHEMA_VERSION` (see the
  [security model](security-model.md#6-backups--migrations-stay-forward-compatible)).
- **Events**: publish on `nexus.core.events`; subscribe in the broadcaster/webhook
  dispatcher to surface to UI/integrations.
- **No new crypto**: reuse `nexus/security/*`.
- **Commit messages**: explain *why*; reference the finding/feature.

---

## Where to look first
- A user-visible feature → the matching `webui/src/screens/*.jsx` + its `/local/*`
  endpoint in `nexus/api/local.py` + the `runtime/` function behind it.
- A peer interaction → `nexus/api/peer.py` + `nexus/networking/*` + the relevant
  `runtime/*` handler.
- Scheduling → `nexus/scheduler/*` + `nexus/tasks/*`.
- Anything security → `nexus/security/*` and the
  [security model guide](security-model.md).
