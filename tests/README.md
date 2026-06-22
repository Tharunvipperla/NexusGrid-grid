# tests/

The automated test suite (pytest). It's the source-of-truth regression net —
every change ships with a test here.

```bash
cd Phase-2
pip install -e .[test]     # pytest + hypothesis (+ pip-audit, bandit)
python -m pytest -q        # run everything (~1360 tests)
python -m pytest tests/test_backup.py -q   # one file
```

Conventions:
- **One test (or more) per change.** For a bug/vuln, write a failing test that
  reproduces it first, then fix until green.
- Security tests live alongside the rest: `test_security_*`, `test_fuzz_security.py`
  (hypothesis property tests), `test_*authz*`.
- Frontend logic has its own tests under [`../webui/test/`](../webui/test/)
  (`node --test`).

Some filenames carry historical `test_waveNN_*` prefixes — that's just the
filename; the tests are current. Heavier, dependency-gated manual end-to-end
checks live in [`../scripts/`](../scripts/), each documented in its own header
comment. Developer testing/build conventions:
[`../docs/dev/build-test.md`](../docs/dev/build-test.md).
