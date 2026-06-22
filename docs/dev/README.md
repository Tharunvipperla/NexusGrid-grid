# Developer Guide

How NexusGrid is built and how to extend it. If you're a *user*, start with the
[User Guide](../user/getting-started.md) instead.

---

## Contents
- **[Architecture](architecture.md)** — the layered package design and a module map.
- **[Plugin system](plugins.md)** — write a relay / pump / runner / DB-provider.
- **[Local API & SDK](api-and-sdk.md)** — the REST API, OpenAPI, SDK/CLI, webhooks.
- **[Security model](security-model.md)** — the trust/crypto invariants you must keep.
- **[Build, test & contribute](build-test.md)** — running, the test suite, packaging, conventions.

---

## 30-second orientation

NexusGrid is a single Python package, `nexus`, exposing a **FastAPI** app
(`nexus.app:create_app`) that serves:
- `/local/*` — the management API (token + private-network gated), used by the UI.
- `/peer/*` — the peer-to-peer protocol (signed/authenticated).
- `/app` — the React control panel (built from `webui/` into a bundle).

The UI is a React app in `webui/` (esbuild → `webui/dist/bundle.js`, served by
`nexus/ui/serve.py` with the local token injected into the page).

```bash
# Run the node (dev) — from the repository root
pip install -e .[test]        # runtime deps + pytest/hypothesis
python -m nexus               # starts the node, opens the UI

# Build the UI bundle after editing webui/src
cd webui && npm install && npm run build

# Run the tests (from the repository root)
python -m pytest -q
```

## Ground rules (read before you change anything)
- **Keep changes surgical** and match the surrounding style.
- **Every change ships with a committed regression test** under `tests/`.
- **Never weaken the [security invariants](security-model.md)** — authorize by
  cryptographic pubkey (never the gossiped UUID), keep DB migrations additive,
  cap untrusted input, and keep code-execution paths consent-gated + sandboxed.
