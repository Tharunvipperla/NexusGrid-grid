/* API reference — surfaces this node's auto-generated OpenAPI so anyone can
 * build their own UI against it. Reads /openapi.json live (so it never drifts
 * from the real surface) and links to the interactive Swagger / ReDoc docs
 * FastAPI already serves. */
import React from "react";
import { I } from "../icons.jsx";
import { CardHead, Pill, Field, Toggle, Chk } from "../components.jsx";
import { api } from "../api.js";
import { notify } from "../toast.jsx";

const METHOD_TONE = { GET: "emerald", POST: "cyan", PUT: "amber", DELETE: "rose", PATCH: "purple" };

/* D3 — outbound webhooks: subscribe an external URL to grid events. The node
 * POSTs a small signed JSON payload whenever a subscribed event fires. */
const WebhooksCard = () => {
  const blank = { url: "", events: [], secret: "", description: "", enabled: true };
  const [hooks, setHooks] = React.useState([]);
  const [catalog, setCatalog] = React.useState([]);
  const [deliveries, setDeliveries] = React.useState([]);
  const [adding, setAdding] = React.useState(false);
  const [f, setF] = React.useState(blank);

  const load = React.useCallback(async () => {
    try {
      const r = await api.get("/local/webhooks");
      setHooks(r.webhooks || []); setCatalog(r.events || []); setDeliveries(r.deliveries || []);
    } catch (_) {}
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const toggleEvent = (ev) => setF(s => ({
    ...s, events: s.events.includes(ev) ? s.events.filter(x => x !== ev) : [...s.events, ev],
  }));

  const save = async () => {
    try {
      await api.post("/local/webhooks", { ...f, url: f.url.trim() });
      setF(blank); setAdding(false); notify("Webhook saved"); load();
    } catch (e) { notify("Webhook failed: " + (e.detail || e.message || "")); }
  };
  const del = async (id) => {
    try { await api.del(`/local/webhooks/${encodeURIComponent(id)}`); notify("Webhook removed"); load(); }
    catch (e) { notify("Remove failed: " + (e.detail || e.message || "")); }
  };
  const test = async (id) => {
    try {
      const r = await api.post(`/local/webhooks/${encodeURIComponent(id)}/test`);
      notify(r.result && r.result.ok ? `Test delivered (HTTP ${r.result.status})` : `Test failed: ${(r.result && (r.result.error || r.result.status)) || "?"}`);
      load();
    } catch (e) { notify("Test failed: " + (e.detail || e.message || "")); }
  };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <CardHead icon={<I.send size={14}/>} tone="amber" title="Webhooks" meta={<span>{hooks.length}</span>}>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => { setF(blank); setAdding(!adding); }}>
          <I.plus size={13}/> Add webhook
        </button>
      </CardHead>
      <div style={{ padding: "14px 20px 18px" }}>
        <div className="hint" style={{ marginBottom: 12 }}>
          NexusGrid POSTs a JSON payload to your URL when a subscribed event fires. Set a secret to
          verify the <span className="mono">X-NexusGrid-Signature</span> (<span className="mono">sha256=HMAC(secret, body)</span>) header.
        </div>

        {adding && (
          <div className="col" style={{ gap: 10, marginBottom: 14 }}>
            <Field label="Payload URL"><input className="input mono" placeholder="https://example.com/hook" value={f.url} onChange={e => setF({ ...f, url: e.target.value })}/></Field>
            <Field label="Events">
              <div className="row" style={{ flexWrap: "wrap", gap: "6px 16px" }}>
                {catalog.map(ev => (
                  <label key={ev} className="row" style={{ gap: 6, cursor: "pointer", fontSize: 12 }}>
                    <Chk on={f.events.includes(ev)} onChange={() => toggleEvent(ev)}/>
                    <span className="mono">{ev}</span>
                  </label>
                ))}
              </div>
            </Field>
            <div className="field-row">
              <Field label="Signing secret (optional)"><input className="input mono" type="password" placeholder="leave blank to keep / disable" value={f.secret} onChange={e => setF({ ...f, secret: e.target.value })}/></Field>
              <Field label="Description (optional)"><input className="input" value={f.description} onChange={e => setF({ ...f, description: e.target.value })}/></Field>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn accent sm" disabled={!f.url.trim() || f.events.length === 0} onClick={save}><I.check size={13}/> Save</button>
              <button className="btn ghost sm" onClick={() => setAdding(false)}>Cancel</button>
            </div>
          </div>
        )}

        {hooks.length === 0 && !adding && <div className="dim" style={{ fontSize: 12 }}>No webhooks yet.</div>}
        {hooks.length > 0 && (
          <table className="t">
            <thead><tr><th>URL</th><th>Events</th><th>Signed</th><th>On</th><th></th></tr></thead>
            <tbody>
              {hooks.map(h => (
                <tr key={h.id}>
                  <td className="mono name" style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis" }}>{h.url}</td>
                  <td style={{ fontSize: 11 }}>{(h.events || []).join(", ")}</td>
                  <td>{h.has_secret ? <Pill tone="emerald">yes</Pill> : <span className="dim">no</span>}</td>
                  <td><Pill tone={h.enabled ? "emerald" : "ghost"}>{h.enabled ? "on" : "off"}</Pill></td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="icon-btn" title="Send test event" onClick={() => test(h.id)}><I.play size={14}/></button>
                    <button className="icon-btn" title="Delete webhook" onClick={() => del(h.id)}><I.trash size={14}/></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {deliveries.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <div className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em", marginBottom: 6 }}>Recent deliveries</div>
            <table className="t">
              <thead><tr><th>When</th><th>Event</th><th>URL</th><th>Result</th></tr></thead>
              <tbody>
                {deliveries.slice(0, 10).map((d, i) => (
                  <tr key={i}>
                    <td className="mono dim" style={{ fontSize: 11 }}>{(d.at || "").slice(11, 19)}</td>
                    <td className="mono" style={{ fontSize: 11 }}>{d.event}</td>
                    <td className="mono dim" style={{ fontSize: 11, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{d.url}</td>
                    <td>{d.ok ? <Pill tone="emerald">{d.status}</Pill> : <Pill tone="rose">{d.status || (d.error || "err").slice(0, 24)}</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

const ApiScreen = () => {
  const [spec, setSpec] = React.useState(null);
  const [err, setErr] = React.useState("");
  const [q, setQ] = React.useState("");
  const [copied, setCopied] = React.useState("");

  const loadSpec = React.useCallback(() => {
    setErr(""); setSpec(null);
    // no-store so a rebuilt schema (added/removed routes) is never served stale.
    fetch("/openapi.json", { cache: "no-store" }).then(r => r.json()).then(setSpec).catch(e => setErr(String(e.message || e)));
  }, []);
  React.useEffect(() => { loadSpec(); }, [loadSpec]);

  const token = (document.querySelector('meta[name="nexus-token"]') || {}).content || "";
  const origin = location.origin;
  const copy = (text, what) => { navigator.clipboard.writeText(text); setCopied(what); setTimeout(() => setCopied(""), 1500); };

  // Flatten the OpenAPI paths into one operation list, grouped by tag.
  const ops = [];
  const paths = (spec && spec.paths) || {};
  for (const p of Object.keys(paths)) {
    for (const m of Object.keys(paths[p])) {
      const op = paths[p][m];
      if (!op || typeof op !== "object") continue;
      ops.push({
        method: m.toUpperCase(), path: p, summary: op.summary || "",
        tag: (op.tags && op.tags[0]) || "Other",
        params: (op.parameters || []).map(x => x.name),
      });
    }
  }
  const ql = q.trim().toLowerCase();
  const shown = ops.filter(o => !ql || (o.path + " " + o.summary + " " + o.method + " " + o.tag).toLowerCase().includes(ql));
  const byTag = {};
  for (const o of shown) (byTag[o.tag] = byTag[o.tag] || []).push(o);
  const tags = Object.keys(byTag).sort();

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">API &amp; docs</div>
          <div className="page-sub">Every endpoint this node serves — build your own UI or scripts against it.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={loadSpec}><I.refresh size={14}/> Refresh</button>
          <a className="btn ghost" href="/docs" target="_blank" rel="noreferrer"><I.terminal size={14}/> Swagger</a>
          <a className="btn ghost" href="/redoc" target="_blank" rel="noreferrer"><I.book size={14}/> ReDoc</a>
          <a className="btn ghost" href="/openapi.json" target="_blank" rel="noreferrer"><I.download size={14}/> openapi.json</a>
        </div>
      </div>

      {/* Quickstart — base URL, auth header, token. */}
      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.zap size={14}/>} tone="cyan" title="Quickstart"/>
        <div style={{ padding: "16px 20px 18px" }}>
        <div className="api-qs">
          <div className="api-qs-row">
            <div className="api-qs-k">Base URL</div>
            <div className="api-block">
              <code>{origin}</code>
              <button className="api-copy" onClick={() => copy(origin, "base")}><I.copy size={12}/> {copied === "base" ? "Copied" : "Copy"}</button>
            </div>
          </div>
          <div className="api-qs-row">
            <div className="api-qs-k">Auth header<span className="hint">every /local call</span></div>
            <div className="api-block">
              <code>X-Local-Token: {token ? "••••••••••••••••" : "<token>"}</code>
              {token && <button className="api-copy" onClick={() => copy(token, "tok")}><I.copy size={12}/> {copied === "tok" ? "Copied" : "Copy token"}</button>}
            </div>
          </div>
          <div className="api-qs-row">
            <div className="api-qs-k">Example</div>
            <div className="api-block">
              <code>{`curl -k ${origin}/local/network -H "X-Local-Token: <token>"`}</code>
              <button className="api-copy" onClick={() => copy(`curl -k ${origin}/local/network -H "X-Local-Token: ${token}"`, "curl")}><I.copy size={12}/> {copied === "curl" ? "Copied" : "Copy"}</button>
            </div>
          </div>
        </div>
        <div className="hint" style={{ marginTop: 14 }}>
          CORS is restricted to this node's own origins by default; set the <span className="mono">NEXUS_CORS_ORIGINS</span> env var
          (comma-separated) to allow a custom UI served from elsewhere.
        </div>
        </div>
      </div>

      {/* SDK & CLI — generate clients or drive the API from the shell. */}
      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.terminal size={14}/>} tone="emerald" title="SDK & CLI"/>
        <div style={{ padding: "16px 20px 18px" }}>
          <div className="api-qs">
            <div className="api-qs-row">
              <div className="api-qs-k">Built-in CLI<span className="hint">lists/calls live ops</span></div>
              <div className="api-block">
                <code>{`python -m nexus.sdk --base ${origin} ops`}</code>
                <button className="api-copy" onClick={() => copy(`python -m nexus.sdk --base ${origin} ops`, "cli1")}><I.copy size={12}/> {copied === "cli1" ? "Copied" : "Copy"}</button>
              </div>
            </div>
            <div className="api-qs-row">
              <div className="api-qs-k">CLI call</div>
              <div className="api-block">
                <code>{`python -m nexus.sdk --base ${origin} call GET /local/network`}</code>
                <button className="api-copy" onClick={() => copy(`python -m nexus.sdk --base ${origin} call GET /local/network`, "cli2")}><I.copy size={12}/> {copied === "cli2" ? "Copied" : "Copy"}</button>
              </div>
            </div>
            <div className="api-qs-row">
              <div className="api-qs-k">Python SDK</div>
              <div className="api-block">
                <code>{`from nexus.sdk import NexusClient; NexusClient.from_local("${origin}").get("/local/network")`}</code>
                <button className="api-copy" onClick={() => copy(`from nexus.sdk import NexusClient\nc = NexusClient.from_local("${origin}")\nprint(c.get("/local/network"))`, "py")}><I.copy size={12}/> {copied === "py" ? "Copied" : "Copy"}</button>
              </div>
            </div>
            <div className="api-qs-row">
              <div className="api-qs-k">Generate a typed client<span className="hint">any standard tool, from the spec</span></div>
              <div className="api-block">
                <code>{`npx openapi-typescript ${origin}/openapi.json -o nexus.d.ts`}</code>
                <button className="api-copy" onClick={() => copy(`npx openapi-typescript ${origin}/openapi.json -o nexus.d.ts`, "ts")}><I.copy size={12}/> {copied === "ts" ? "Copied" : "Copy"}</button>
              </div>
            </div>
            <div className="api-qs-row">
              <div className="api-qs-k">Python (typed)</div>
              <div className="api-block">
                <code>{`openapi-python-client generate --url ${origin}/openapi.json`}</code>
                <button className="api-copy" onClick={() => copy(`openapi-python-client generate --url ${origin}/openapi.json`, "opc")}><I.copy size={12}/> {copied === "opc" ? "Copied" : "Copy"}</button>
              </div>
            </div>
          </div>
          <div className="hint" style={{ marginTop: 14 }}>
            The CLI reads <span className="mono">.nexus_local_token</span> automatically; pass
            <span className="mono"> --token</span> to override. The generators above are third-party —
            the node just serves the spec, so any OpenAPI tool works.
          </div>
        </div>
      </div>

      <WebhooksCard/>

      <div className="card">
        <CardHead icon={<I.list size={14}/>} tone="purple" title="Endpoints" meta={<span>{ops.length} total</span>}>
          <div className="api-search" style={{ marginLeft: "auto" }}>
            <I.search size={13}/>
            <input placeholder="Filter by path, tag, or method…" value={q} onChange={e => setQ(e.target.value)}/>
          </div>
        </CardHead>
        {err && <div className="dim" style={{ padding: 16 }}>Couldn't load the schema: {err}</div>}
        {!err && !spec && <div className="dim" style={{ padding: 16 }}>Loading schema…</div>}
        {spec && shown.length === 0 && <div className="dim" style={{ padding: 16 }}>No endpoints match "{q}".</div>}
        {tags.map(tag => (
          <div key={tag} className="api-group">
            <div className="api-tag">{tag} <span className="dim">· {byTag[tag].length}</span></div>
            {byTag[tag].map((o, i) => (
              <div key={o.method + o.path + i} className="api-row">
                <Pill tone={METHOD_TONE[o.method] || "ghost"}>{o.method}</Pill>
                <code className="api-path">{o.path}</code>
                {o.summary && <span className="api-sum dim">{o.summary}</span>}
              </div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
};

export { ApiScreen };
