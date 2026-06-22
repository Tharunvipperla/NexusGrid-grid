# webui/

The React control-panel single-page app. Built with **esbuild** (no framework
runtime beyond React) into `dist/bundle.js`, which the node serves at `/app` with
the local API token injected (`nexus/ui/serve.py`).

```bash
cd webui
npm install
npm run build      # esbuild src/app.jsx -> dist/bundle.js
npm test           # node --test (e.g. the DAG graph helpers in test/)
```

| Path | What it is |
|---|---|
| `src/` | The app source. `app.jsx` (entry), `shell.jsx` (sidebar/top-bar/bell), `screens/*.jsx` (one per UI screen), `components.jsx`, `api.js`, `icons.jsx`, `dag.js`, `toast.jsx`. |
| `index.html` | The SPA shell that loads `dist/bundle.js`. |
| `styles.css` | App styles. |
| `test/` | Frontend unit tests (`node --test`). |
| `dist/` | Build output — **gitignored**; rebuild after editing `src/`. |

`dist/bundle.js` is **not** committed; rebuild it after changing `src/`. The
per-screen behavior is documented in
[`../docs/user/screens/`](../docs/user/screens/).
