# tools/

Maintainer tooling that is **not** shipped inside the app.

| Tool | What it does |
|---|---|
| `sign_release.py` | Sign a release manifest with the offline keys (root → delegation cert → release key). Used when cutting a release. |

`sign_release.py` is the only place the **private** signing keys are handled, and
only at release time — keys are kept offline and are never committed (see
[`../release/`](../release/) and [`../release/RELEASING.md`](../release/RELEASING.md)).
The matching verification logic that every node runs is
`nexus/security/app_update.py`.
