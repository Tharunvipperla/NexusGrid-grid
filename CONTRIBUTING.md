# Contributing to NexusGrid

Thanks for your interest! NexusGrid is a peer-to-peer compute and storage grid.
This guide gets you from clone to a merged PR.

## Getting set up

```bash
git clone https://github.com/Tharunvipperla/NexusGrid-grid nexusgrid
cd nexusgrid
pip install -e ".[test]"     # runtime + test tooling (pytest, hypothesis, pip-audit, bandit)
python -m nexus              # start a node; the control panel opens in your browser
```

Run a second node on the same machine to exercise peer paths:

```bash
python -m nexus --port 8001 --no-browser
```

The UI is a React app under `webui/` (esbuild → `webui/dist/bundle.js`); rebuild it
after editing `webui/src`:

```bash
cd webui && npm install && npm run build
```

See [`docs/dev/`](docs/dev/) for the architecture, build/test details, and the
security model.

## Ground rules

- **Keep changes surgical.** Touch only what the change requires; match the
  surrounding style. Don't refactor unrelated code in the same PR.
- **Every change ships with a test.** Add a regression test under `tests/`. For a
  bug or vulnerability, write a test that *reproduces* it first, then fix until green.
- **Run the suite before you push:** `python -m pytest -q`.
- **Don't invent new crypto.** Reuse the primitives in `nexus/security/`.

## Submitting a pull request

1. Fork and branch off `main`.
2. Make your change with its test; ensure `pytest -q` and the UI build pass.
3. Open a PR describing **what** changed and **why**. Link any related issue.
4. CI (backend + frontend) must be green before review.

## Reporting bugs / requesting features

Use the issue templates (Bug report / Feature request). For questions and ideas,
prefer **Discussions**. For security issues, **do not** open a public issue — see
[`SECURITY.md`](SECURITY.md).
