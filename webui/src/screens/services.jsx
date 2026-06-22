/* Services — the marketplace the v3 design lacked. Discover services peers
 * offer, manage the access grants you hold, and author your own hosted
 * services (full editor incl. auto-run spec + components), wired to the real
 * endpoints. My-services persistence = PUT /local/profile {hosted_services}
 * (same full-array replace the classic UI does). */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, CardHead, Chk, Field, Avatar, CodeField } from "../components.jsx";
import { notify, toast } from "../toast.jsx";

const fld = (svc, k, d) => (svc && svc[k] != null ? svc[k] : d);

/* ── Full-page service detail (replaces the old popup): readme / how-to,
 * run spec, cookbook copy, and W60 sandbox auto-run with an in-page consent
 * gate (server re-checks `agreed`), plus a metadata sidebar. ── */
const ServiceDetailPage = ({ entry, hasGrant, onBack, onRequest, refresh }) => {
  const svc = entry.service || {};
  const run = svc.run || {};
  const hasRun = !!(run.image || run.cmd || (run.build && run.build.dockerfile));
  const access = fld(svc, "access", "gated");
  const isFree = access === "free" || access === "open";
  const owner = entry.provider_name || entry.provider_uuid;
  const [runners, setRunners] = React.useState(null);
  const [runner, setRunner] = React.useState("");
  const [outbound, setOutbound] = React.useState(false);
  const [agreed, setAgreed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState("");
  const base = `/local/peers/${encodeURIComponent(entry.provider_uuid)}/services/${encodeURIComponent(svc.name || "")}`;

  React.useEffect(() => {
    if (!hasRun) return;
    api.get("/local/replica_runners").then(r => {
      const rs = (r.runners || []).filter(x => x.available);
      setRunners(rs);
      setRunner(prev => prev || (rs[0] && rs[0].name) || "");
    }).catch(() => setRunners([]));
  }, [hasRun]);

  const copyCookbook = async () => {
    setBusy(true);
    try {
      const r = await api.post(`${base}/cookbook`);
      setNote(`Cookbook copied ✓ ${r.path || r.filename || ""}`);
      if (refresh) refresh();
    } catch (e) { setNote("Cookbook failed: " + (e.detail || e.message || "")); }
    setBusy(false);
  };
  const runReplica = async () => {
    setBusy(true);
    try {
      const r = await api.post(`${base}/run`, { runner, allow_outbound: outbound, agreed });
      setNote(`Replica started ✓ ${r.replica_id ? "id " + r.replica_id : ""}${r.ports ? " · ports " + [].concat(r.ports).join(", ") : ""}`);
      if (refresh) refresh();
    } catch (e) {
      const d = e.detail || e.message || "";
      setNote("Run failed: " + d + (d.includes("image_not_allowed") ? " — add the image to your allowed list in Local Config → replication" : ""));
    }
    setBusy(false);
  };

  return (
    <div className="svc-detail">
      <div className="svc-bc">
        <span className="svc-bc-link" onClick={onBack}>Services</span>
        <span className="svc-bc-sep">/</span><span>{owner}</span>
        <span className="svc-bc-sep">/</span><span className="svc-bc-cur">{svc.name || "service"}</span>
      </div>

      <div className="svc-grid">
        <div className="svc-main">
          <h1 className="svc-title">{svc.name || "Service"}</h1>
          <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 6 }}>
            <Pill tone={isFree ? "emerald" : "ghost"}>{access}</Pill>
            {svc.version && <span className="mono dim" style={{ fontSize: 12 }}>v{svc.version}</span>}
            {svc.replicable && <Pill tone="cyan">replicable</Pill>}
            {hasRun && <Pill tone="amber">auto-run</Pill>}
          </div>
          {svc.description && <p className="svc-desc">{svc.description}</p>}

          {(svc.components || []).length > 0 && (
            <div className="svc-sec">
              <h3>Bundle components</h3>
              <div className="hint" style={{ marginBottom: 6 }}>each component gets its own secure tunnel</div>
              {(svc.components || []).map((c, i) => (
                <div key={i} className="mono" style={{ fontSize: 12 }}>{c.name}{c.protocol ? ` · ${c.protocol}` : ""}{(c.tags || []).length ? ` · ${c.tags.join(", ")}` : ""}</div>
              ))}
            </div>
          )}

          {hasRun && (
            <div className="svc-sec">
              <h3>Run spec</h3>
              <pre className="svc-pre">
                {run.build && run.build.dockerfile ? `build  custom Dockerfile (built locally on FROM-allowlisted base)\n` : ""}
                {run.image ? `image  ${run.image}\n` : ""}
                {run.cmd ? `cmd    ${run.cmd}\n` : ""}
                {(run.ports || []).length ? `ports  ${(run.ports || []).join(", ")}\n` : ""}
                {(run.env || []).length ? `env    ${(run.env || []).join(", ")}\n` : ""}
                {(run.inputs || []).length ? `inputs ${run.inputs.map(i => i.dest).join(", ")} (cloud)` : ""}
              </pre>
            </div>
          )}

          <div className="svc-sec">
            <h3>How to use</h3>
            {svc.readme
              ? <pre className="svc-pre svc-readme">{svc.readme}</pre>
              : <div className="dim" style={{ fontSize: 13 }}>The provider hasn't written usage details yet.{hasGrant ? " Use Connect to get the tunnel endpoints." : ""}</div>}
          </div>

          {hasRun && (
            <div className="svc-sec">
              <h3>Run a copy on this machine</h3>
              <div className="card" style={{ background: "rgba(245,158,11,0.05)", border: "1px solid rgba(245,158,11,0.35)", padding: 14 }}>
                <div style={{ fontSize: 12, color: "var(--amber, #fbbf24)", marginBottom: 10 }}>
                  This runs <strong>{owner}'s</strong> published spec on your hardware.
                  Sandboxed runners isolate it in a container with network off by default; the <span className="mono">raw</span> runner
                  executes their command directly on your machine — only use it for providers you fully trust.
                </div>
                {runners === null && <div className="hint">Checking available sandboxes…</div>}
                {runners && runners.length === 0 && <div className="hint">No runners available — install Docker or Podman, or add a nexus_runners plugin.</div>}
                {runners && runners.length > 0 && (
                  <div className="col" style={{ gap: 8 }}>
                    <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <select className="input" style={{ width: 180 }} value={runner} onChange={e => setRunner(e.target.value)}>
                        {runners.map(r => <option key={r.name} value={r.name}>{r.name}{r.sandboxed ? "" : " (NOT sandboxed)"}</option>)}
                      </select>
                      <div className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }} onClick={() => setOutbound(!outbound)}>
                        <Chk on={outbound}/><span style={{ fontSize: 12 }}>allow outbound network</span>
                      </div>
                    </div>
                    <div className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }} onClick={() => setAgreed(!agreed)}>
                      <Chk on={agreed}/>
                      <span style={{ fontSize: 12 }}>I understand this runs the provider's code on my machine and I accept the risk</span>
                    </div>
                    <div>
                      <button className="btn accent sm" disabled={!agreed || !runner || busy} onClick={runReplica}>
                        <I.play size={13}/> {busy ? "Starting…" : "Run replica"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
          {note && <div className="hint" style={{ marginTop: 10, color: note.includes("✓") ? "var(--emerald, #34d399)" : "var(--rose, #fb7185)" }}>{note}</div>}
        </div>

        <aside className="svc-side">
          {!hasGrant && !isFree && (
            <button className="btn accent" style={{ width: "100%" }} onClick={() => onRequest(entry)}><I.key size={14}/> Request access</button>
          )}
          {hasGrant && <div className="svc-have"><I.check size={13}/> You have access — connect from My access</div>}
          {svc.replicable && (
            <button className="btn ghost" style={{ width: "100%" }} disabled={busy} onClick={copyCookbook}><I.copy size={13}/> Copy cookbook</button>
          )}

          <div className="svc-meta">
            <div className="svc-meta-k">Owner</div>
            <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 5 }}>
              <Avatar name={owner} seed={entry.provider_uuid} size={22}/>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{owner}</span>
            </div>
          </div>
          <div className="svc-meta">
            <div className="svc-meta-k">Access</div>
            <div className="svc-meta-v">{isFree ? "Free — open to peers" : access === "paid" ? "Paid (coming later)" : "By request — provider approves each peer"}</div>
          </div>
          {svc.version && (
            <div className="svc-meta"><div className="svc-meta-k">Version</div><div className="svc-meta-v mono">v{svc.version}</div></div>
          )}
          {(svc.tags || []).length > 0 && (
            <div className="svc-meta">
              <div className="svc-meta-k">Tags</div>
              <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 5 }}>{svc.tags.map((t, j) => <span key={j} className="pill ghost">{t}</span>)}</div>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
};

const csv = (s) => String(s || "").split(",").map(t => t.trim()).filter(Boolean);

/* In-page service editor (create when svc is null). Saves by replacing the
 * hosted_services array, preserving any fields this form doesn't know about. */
const ServiceEditor = ({ svc, onSave, onDelete, onCancel }) => {
  const s = svc || {};
  const run = s.run || {};
  const [f, setF] = React.useState({
    name: s.name || "", version: s.version || "", access: s.access || "free",
    description: s.description || "", tags: (s.tags || []).join(", "),
    local_host: s.local_host || "127.0.0.1", local_port: s.local_port || "",
    pump: s.pump || "", replicable: !!s.replicable, readme: s.readme || "",
    run_image: run.image || "", run_cmd: run.cmd || "",
    run_ports: (run.ports || []).join(", "), run_env: (run.env || []).join(", "),
    run_gpu: run.gpu == null ? "" : String(run.gpu),
    run_dockerfile: (run.build && run.build.dockerfile) || "",
    run_inputs: (run.inputs || []).map(i => `${i.dest} = ${i.uri}`).join("\n"),
    service_kind: s.service_kind || "",
    db_engine: (s.db_provider && s.db_provider.engine) || "",
    db_admin_dsn: (s.db_provider && s.db_provider.admin_dsn) || "",
  });
  const [components, setComponents] = React.useState(
    (s.components || []).map(c => ({ name: c.name || "", protocol: c.protocol || "", local_port: c.local_port || "", tags: (c.tags || []).join(", ") })));
  const [confirmDel, setConfirmDel] = React.useState(false);
  const [engineBusy, setEngineBusy] = React.useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e && e.target ? e.target.value : e });

  // GPU control is host-aware: a simple on/off when there's one GPU, a count
  // slider only when the host actually has several, disabled when there's none.
  // run_gpu stays a string ("", a count, or "all"); the backend re-validates it
  // and rejects a GPU request on a host with no GPU. No free text = no junk.
  const [gpuInfo, setGpuInfo] = React.useState(null);
  React.useEffect(() => {
    api.get("/local/gpu_info").then(setGpuInfo)
       .catch(() => setGpuInfo({ available: false, count: 0 }));
  }, []);
  const gpuMax = (gpuInfo && gpuInfo.count) || 1;
  const gpuOn = !!f.run_gpu;
  const gpuN = f.run_gpu === "all" ? gpuMax : (parseInt(f.run_gpu, 10) || 1);
  const setGpuOn = (on) => set("run_gpu")(on ? "all" : "");
  const setGpuN = (n) => set("run_gpu")(n >= gpuMax ? "all" : String(n));

  /* C7: one-click — start a managed local DB engine and auto-fill the engine,
   * service kind, and admin DSN fields below. */
  const startEngine = async (engine) => {
    setEngineBusy(engine);
    try {
      const r = await api.post("/local/dbaas/start_engine", { engine });
      setF(prev => ({ ...prev, db_engine: r.engine, service_kind: r.kind, db_admin_dsn: r.admin_dsn }));
      notify(`Started ${engine} on 127.0.0.1:${r.port} — admin DSN filled in. Save the service to use it.`);
    } catch (e) {
      toast("Could not start engine: " + (e.detail || e.message || ""), "danger");
    } finally { setEngineBusy(""); }
  };

  const save = () => {
    if (!f.name.trim()) return;
    onSave({
      ...s, // keep fields the editor doesn't manage
      name: f.name.trim(), version: f.version.trim(), access: f.access,
      description: f.description.trim(), tags: csv(f.tags), readme: f.readme,
      local_host: f.local_host.trim() || "127.0.0.1",
      local_port: parseInt(f.local_port || "0", 10) || 0,
      pump: f.pump.trim(), replicable: f.replicable,
      service_kind: f.service_kind.trim(),
      // Host-only DBaaS config: when both set, approved consumers get a
      // per-consumer database + login provisioned on this engine. Stripped
      // before the service is advertised (admin_dsn is a secret).
      db_provider: (f.db_engine.trim() && f.db_admin_dsn.trim())
        ? { engine: f.db_engine.trim(), admin_dsn: f.db_admin_dsn.trim() }
        : {},
      run: {
        image: f.run_image.trim(), cmd: f.run_cmd.trim(),
        ports: csv(f.run_ports).map(p => parseInt(p, 10)).filter(n => n > 0),
        env: csv(f.run_env),
        // GPU passthrough request: "all" or a count. Empty = no GPU.
        ...(f.run_gpu.trim() ? { gpu: f.run_gpu.trim() } : {}),
        // A1: optional custom build context. The consumer builds it locally
        // (consent + FROM-allowlist + sandbox); empty = pull `image` instead.
        ...(f.run_dockerfile.trim() ? { build: { dockerfile: f.run_dockerfile } } : {}),
        // A2: optional cloud inputs ("dest = uri" per line) the consumer
        // downloads before running. uri is http(s):// or an rclone remote:path.
        ...((() => {
          const inputs = f.run_inputs.split("\n").map(l => l.trim()).filter(Boolean).map(l => {
            const i = l.indexOf("=");
            return i < 0 ? null : { dest: l.slice(0, i).trim(), uri: l.slice(i + 1).trim() };
          }).filter(x => x && x.dest && x.uri);
          return inputs.length ? { inputs } : {};
        })()),
      },
      components: components
        .map(c => ({ name: c.name.trim(), protocol: c.protocol.trim(), local_port: parseInt(c.local_port || "0", 10) || 0, tags: csv(c.tags) }))
        .filter(c => c.name),
    });
  };

  return (
    <div className="card pad-lg" style={{ borderColor: "var(--accent)" }}>
      <CardHead icon={<I.terminal size={14}/>} tone="purple" title={svc ? "Edit service" : "Deploy a service"}/>
      <div className="field-row tri" style={{ marginTop: 18, marginBottom: 12 }}>
        <Field label="Service name"><input className="input" maxLength={80} placeholder="e.g. BigLLM" value={f.name} onChange={set("name")}/></Field>
        <Field label="Version (optional)"><input className="input mono" maxLength={40} value={f.version} onChange={set("version")}/></Field>
        <Field label="Access" hint={f.access === "free" ? "anyone you're connected to can use it" : f.access === "permission" ? "peers must request — you approve each one" : "payments ship later"}>
          <select className="input" value={f.access} onChange={set("access")}>
            <option value="free">Free</option>
            <option value="permission">Permission</option>
            <option value="paid">Paid (soon)</option>
          </select>
        </Field>
      </div>
      <Field label="One-line description" hint="shown in the discover list">
        <input className="input" maxLength={400} value={f.description} onChange={set("description")}/>
      </Field>
      <div style={{ marginTop: 12 }}>
        <Field label="Tags (comma-separated)"><input className="input mono" placeholder="redis, sql, gpu" value={f.tags} onChange={set("tags")}/></Field>
      </div>
      <div className="field-row tri" style={{ marginTop: 12 }}>
        <Field label="Local host"><input className="input mono" maxLength={120} value={f.local_host} onChange={set("local_host")}/></Field>
        <Field label="Local port" hint="where the service listens on this machine"><input className="input mono" type="number" min={1} max={65535} value={f.local_port} onChange={set("local_port")}/></Field>
        <Field label="Pump (optional)" hint="blank = default byte forwarder; or a custom pump from nexus_pumps/"><input className="input mono" maxLength={60} value={f.pump} onChange={set("pump")}/></Field>
      </div>
      <div className="row" style={{ gap: 8, marginTop: 14, alignItems: "center" }}>
        <Chk on={f.replicable} onChange={v => setF({ ...f, replicable: v })}/>
        <span style={{ fontSize: 13 }}>Let others copy the cookbook</span>
        <span className="hint">they run it themselves — never auto-run on your machine</span>
      </div>

      <hr className="divider" style={{ margin: "16px 0" }}/>
      <div className="label" style={{ marginBottom: 8 }}>Database service (optional) <span className="hint" style={{ fontWeight: 400 }}>— DBaaS: provision a private DB + login per approved consumer</span></div>
      <div className="field-row tri">
        <Field label="Service kind" hint="postgres / mysql / redis / mongo — drives the connection string">
          <input className="input mono" maxLength={40} placeholder="postgres" value={f.service_kind} onChange={set("service_kind")}/>
        </Field>
        <Field label="Provider engine" hint="adapter to provision with (e.g. postgres); blank = no provisioning">
          <input className="input mono" maxLength={40} placeholder="postgres" value={f.db_engine} onChange={set("db_engine")}/>
        </Field>
        <Field label="Admin DSN" hint="host-only secret — never leaves this node">
          <input className="input mono" type="password" maxLength={500} placeholder="postgresql://admin:pw@127.0.0.1:5432/postgres" value={f.db_admin_dsn} onChange={set("db_admin_dsn")}/>
        </Field>
      </div>
      <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
        <span className="hint">No engine yet? Start a managed local one (loopback-only, fills the fields above):</span>
        {["postgres", "mysql", "redis", "mongo"].map(eng => (
          <button key={eng} type="button" className="btn ghost sm" disabled={!!engineBusy} onClick={() => startEngine(eng)}>
            {engineBusy === eng ? "Starting…" : <><I.box size={12}/> {eng}</>}
          </button>
        ))}
      </div>
      <div className="hint" style={{ marginTop: 6 }}>First start pulls the image (can take a minute). The engine survives node restarts; remove it from Docker when done.</div>

      <hr className="divider" style={{ margin: "16px 0" }}/>
      <div className="label" style={{ marginBottom: 8 }}>Auto-run spec (optional) <span className="hint" style={{ fontWeight: 400 }}>— lets others run this in their own sandbox</span></div>
      <div className="field-row">
        <Field label="Container image"><input className="input mono" maxLength={200} placeholder="ollama/ollama:latest" value={f.run_image} onChange={set("run_image")}/></Field>
        <Field label="Command" hint="optional; required for the raw runner"><input className="input mono" maxLength={500} value={f.run_cmd} onChange={set("run_cmd")}/></Field>
      </div>
      <div className="field-row" style={{ marginTop: 12 }}>
        <Field label="Ports (CSV)"><input className="input mono" placeholder="11434" value={f.run_ports} onChange={set("run_ports")}/></Field>
        <Field label="Env (CSV, KEY=VAL)" hint="secrets allowed as secret://NAME — resolved at run time, never shared"><input className="input mono" value={f.run_env} onChange={set("run_env")}/></Field>
      </div>
      <div style={{ marginTop: 12 }}>
        {!gpuInfo ? (
          <Field label="GPU"><div className="hint">checking for a GPU…</div></Field>
        ) : !gpuInfo.available ? (
          <Field label="GPU" hint="no GPU detected on this host — the service runs on CPU here.">
            <div className="hint">No GPU available on this machine.</div>
          </Field>
        ) : (
          <Field label="GPU"
                 hint="give this service the host's GPU (NVIDIA, via --gpus). Sharing isn't throttled — the service gets the full card.">
            <div className="row" style={{ alignItems: "center", gap: 10 }}>
              <Chk on={gpuOn} onChange={setGpuOn}/>
              <span style={{ fontSize: 13 }}>
                {!gpuOn ? "No GPU"
                  : gpuInfo.count > 1 ? `Use ${gpuN >= gpuMax ? "all" : gpuN} of ${gpuMax} GPUs`
                  : "Use the GPU"}
              </span>
            </div>
            {gpuOn && gpuInfo.count > 1 && (
              <input type="range" min={1} max={gpuMax} step={1} value={gpuN}
                     onChange={e => setGpuN(+e.target.value)} style={{ width: "100%", marginTop: 8 }}/>
            )}
          </Field>
        )}
      </div>
      <div style={{ marginTop: 12 }}>
        <CodeField label="Custom build — Dockerfile (optional)" language="dockerfile" rows={5}
                   hint="leave blank to pull the image above; or build a custom image (FROM base must be on the consumer's allowed images). Expand for full-screen."
                   value={f.run_dockerfile} onChange={set("run_dockerfile")}/>
      </div>
      <div style={{ marginTop: 12 }}>
        <Field label="Cloud inputs (optional)"
               hint={"one per line: dest = uri. uri is an http(s):// link or an rclone remote:path. Downloaded into /nexus/inputs (container) or the working dir (raw) before the command runs."}>
          <textarea className="input mono" rows={3} style={{ resize: "vertical", fontSize: 12 }}
                    placeholder={"model.bin = https://example.com/model.bin\ndata/train.csv = gdrive:datasets/train.csv"}
                    value={f.run_inputs} onChange={set("run_inputs")}/>
        </Field>
      </div>

      <hr className="divider" style={{ margin: "16px 0" }}/>
      <div className="row" style={{ alignItems: "center", marginBottom: 8 }}>
        <div className="label">Components (optional) <span className="hint" style={{ fontWeight: 400 }}>— bundle sub-services; each gets its own tunnel</span></div>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }}
                onClick={() => setComponents([...components, { name: "", protocol: "", local_port: "", tags: "" }])}>
          <I.plus size={13}/> Add component
        </button>
      </div>
      {components.map((c, i) => (
        <div key={i} className="row" style={{ gap: 8, marginBottom: 8 }}>
          <input className="input mono" placeholder="Component (e.g. postgres)" maxLength={60} style={{ flex: 2 }} value={c.name}
                 onChange={e => setComponents(components.map((x, j) => j === i ? { ...x, name: e.target.value } : x))}/>
          <input className="input mono" placeholder="Protocol" maxLength={40} style={{ flex: 1 }} value={c.protocol}
                 onChange={e => setComponents(components.map((x, j) => j === i ? { ...x, protocol: e.target.value } : x))}/>
          <input className="input mono" placeholder="Local port" type="number" min={1} max={65535} style={{ flex: 1 }} value={c.local_port}
                 onChange={e => setComponents(components.map((x, j) => j === i ? { ...x, local_port: e.target.value } : x))}/>
          <input className="input mono" placeholder="tags" style={{ flex: 1 }} value={c.tags}
                 onChange={e => setComponents(components.map((x, j) => j === i ? { ...x, tags: e.target.value } : x))}/>
          <button className="btn ghost sm" onClick={() => setComponents(components.filter((_, j) => j !== i))}><I.x size={13}/></button>
        </div>
      ))}

      <div style={{ marginTop: 4 }}>
        <Field label="Details (markdown)" hint="how to connect, what it's built on, a recipe, links, license…">
          <textarea className="input mono" rows={10} style={{ width: "100%", resize: "vertical", fontSize: 12 }}
                    placeholder={"# My Service\n\n## How to connect\nPoint your client at the local port.\n\n## Recipe (run it yourself)\n```\ndocker compose up\n```"}
                    value={f.readme} onChange={set("readme")}/>
        </Field>
      </div>

      <div className="row" style={{ gap: 10, marginTop: 16 }}>
        <button className="btn accent" disabled={!f.name.trim()} onClick={save}><I.check size={14}/> Save</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
        {svc && (
          <button className={"btn sm " + (confirmDel ? "accent" : "ghost")} style={{ marginLeft: "auto" }}
                  onClick={() => { if (confirmDel) { setConfirmDel(false); onDelete(); } else { setConfirmDel(true); setTimeout(() => setConfirmDel(false), 3500); } }}>
            {confirmDel ? "Delete this service?" : "Delete"}
          </button>
        )}
      </div>
    </div>
  );
};

const ServicesScreen = () => {
  const [tab, setTab] = React.useState("discover");
  const [services, setServices] = React.useState([]);
  const [held, setHeld] = React.useState([]);
  const [issued, setIssued] = React.useState([]);
  const [mine, setMine] = React.useState([]);
  const [editing, setEditing] = React.useState(null); // {idx} — idx -1 = new
  const [loading, setLoading] = React.useState(true);
  const [msg, setMsg] = React.useState("");
  const [viewing, setViewing] = React.useState(null);  // discover entry → full-page detail
  const [replicas, setReplicas] = React.useState([]);
  const [cookbooks, setCookbooks] = React.useState([]);
  const [inbox, setInbox] = React.useState([]);        // pending access requests (host side)
  const [names, setNames] = React.useState({});        // peer_names: nexus_<id> → display name
  const [dbConn, setDbConn] = React.useState({});      // grant_id → DBaaS conn {engine,kind,database,user,password}
  const [ports, setPorts] = React.useState({});        // grant_id → local tunnel port (from Connect)

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [disc, grants, profile, reps, books, reqs, net] = await Promise.all([
        api.get("/local/services/discover").catch(() => ({ services: [] })),
        api.get("/local/service_grants").catch(() => ({ held: [], issued: [] })),
        api.get("/local/profile").catch(() => ({})),
        api.get("/local/replicas").catch(() => ({ replicas: [] })),
        api.get("/local/cookbooks").catch(() => ({ cookbooks: [] })),
        api.get("/local/service_requests").catch(() => ({ requests: [] })),
        api.get("/local/network").catch(() => ({})),
      ]);
      setServices((disc && disc.services) || []);
      setHeld((grants && grants.held) || []);
      setIssued((grants && grants.issued) || []);
      setMine((profile && profile.hosted_services) || []);
      setReplicas((reps && reps.replicas) || []);
      setCookbooks((books && books.cookbooks) || []);
      setInbox((reqs && reqs.requests) || []);
      setNames((net && net.peer_names) || {});
    } catch (_) {}
    setLoading(false);
  }, []);

  // Cookbook filenames embed the provider's node id (nexus_<id>_Name.md) — swap
  // any known id for its display name.
  const resolveIds = (text) => String(text || "").replace(/nexus_[0-9a-f]+/gi, (m) => names[m] || m);

  React.useEffect(() => { load(); }, [load]);

  const flash = (t) => { setMsg(t); setTimeout(() => setMsg(""), 4000); };
  const act = async (label, fn) => {
    try { await fn(); flash(label + " ✓"); await load(); }
    catch (e) { flash(label + " failed: " + (e.detail || e.message || "")); }
  };

  const saveMine = async (next, label) => {
    try {
      await api.put("/local/profile", { hosted_services: next });
      flash(label + " ✓"); setEditing(null); await load();
    } catch (e) { flash(label + " failed: " + (e.detail || e.message || "")); }
  };

  const requestAccess = (entry) => {
    const name = fld(entry.service, "name", "");
    return act("Requested " + name, () =>
      api.post(`/local/peers/${encodeURIComponent(entry.provider_uuid)}/services/${encodeURIComponent(name)}/request`,
               { provider_pubkey: entry.provider_pubkey || "" }));
  };

  /* Connect flashes the actual tunnel endpoints so users know where to point a client. */
  const connectGrant = async (g) => {
    try {
      const r = await api.post(`/local/service_grants/${encodeURIComponent(gId(g))}/connect`);
      const eps = (r.endpoints || []).map(e => `${e.name ? e.name + " " : ""}${e.host || "127.0.0.1"}:${e.port}`).join(" · ");
      // Remember the local listener port so a DBaaS connection string can use it.
      if (r.endpoints && r.endpoints[0]) setPorts(p => ({ ...p, [gId(g)]: r.endpoints[0].port }));
      flash(`Connected ✓ ${eps || ""}`);
      await load();
    } catch (e) { flash("Connect failed: " + (e.detail || e.message || "")); }
  };

  /* DBaaS: fetch the provider-provisioned per-consumer login for a DB service. */
  const getDbCreds = async (g) => {
    try {
      const r = await api.get(`/local/service_grants/${encodeURIComponent(gId(g))}/db_credentials`);
      setDbConn(c => ({ ...c, [gId(g)]: r.conn || {} }));
    } catch (e) { flash("DB credentials: " + (e.detail || e.message || "")); }
  };

  /* Build a ready-to-run connection string from creds + the live tunnel port. */
  const dbDsn = (conn, port) => {
    const k = (conn.kind || conn.engine || "").toLowerCase();
    const pt = port || "<connect first>";
    if (k === "postgres") return `PGPASSWORD=${conn.password} psql -h localhost -p ${pt} -U ${conn.user} ${conn.database}`;
    if (k === "mysql") return `mysql -h 127.0.0.1 -P ${pt} -u ${conn.user} -p'${conn.password}' ${conn.database}`;
    if (k === "mongo") return `mongosh "mongodb://${conn.user}:${conn.password}@localhost:${pt}/${conn.database}"`;
    return `${conn.user}:${conn.password}@localhost:${pt}/${conn.database}`;
  };

  const gName = (g) => g.service_name || (g.service && g.service.name) || g.name || g.grant_id || "service";
  const gProvider = (g) => g.provider_name || names[g.provider_uuid] || g.provider_uuid || g.issuer_name || "";
  const provName = (e) => e.provider_name || names[e.provider_uuid] || e.provider_uuid;
  const gId = (g) => g.grant_id || g.id;

  // A detail page opens only for a free service or one you already hold a grant
  // for — gated services with no access can only be requested first.
  const isFreeEntry = (e) => { const a = fld(e.service, "access", ""); return a === "free" || a === "open"; };
  const hasGrantFor = (e) => {
    const n = (fld(e.service, "name", "") || "").toLowerCase();
    return held.some(g => (g.service_name || (g.service && g.service.name) || "").toLowerCase() === n
                          && (!g.provider_uuid || g.provider_uuid === e.provider_uuid));
  };
  const canOpen = (e) => isFreeEntry(e) || hasGrantFor(e);

  if (viewing) {
    return <ServiceDetailPage entry={viewing} hasGrant={hasGrantFor(viewing)}
                              onBack={() => setViewing(null)}
                              onRequest={(e) => requestAccess(e)} refresh={load}/>;
  }

  if (editing) {
    const svc = editing.idx >= 0 ? mine[editing.idx] : null;
    return (
      <div className="svc-detail">
        <div className="svc-bc">
          <span className="svc-bc-link" onClick={() => setEditing(null)}>Services</span>
          <span className="svc-bc-sep">/</span>
          <span className="svc-bc-cur">{svc ? `Edit ${svc.name || "service"}` : "Deploy a service"}</span>
        </div>
        {msg && <div className={"banner " + (msg.includes("failed") ? "danger" : "info")} style={{ marginBottom: 14 }}>
          <I.info size={14}/><span>{msg}</span>
        </div>}
        <ServiceEditor
          svc={svc}
          onCancel={() => setEditing(null)}
          onSave={(s) => {
            const next = editing.idx >= 0 ? mine.map((x, i) => i === editing.idx ? s : x) : [...mine, s];
            saveMine(next, editing.idx >= 0 ? "Service updated" : "Service deployed");
          }}
          onDelete={() => saveMine(mine.filter((_, i) => i !== editing.idx), "Service deleted")}/>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Services</div>
          <div className="page-sub">Discover services peers host, and manage the access you hold.</div>
        </div>
        <div className="page-tools">
          <div className="seg">
            <button className={tab === "discover" ? "on" : ""} onClick={() => setTab("discover")}>Discover</button>
            <button className={tab === "mine" ? "on" : ""} onClick={() => setTab("mine")}>My services {mine.length ? `(${mine.length})` : ""}</button>
            <button className={tab === "access" ? "on" : ""} onClick={() => setTab("access")}>My access {held.length ? `(${held.length})` : ""}</button>
            <button className={tab === "issued" ? "on" : ""} onClick={() => setTab("issued")}>
              Issued {issued.length ? `(${issued.length})` : ""}{inbox.length ? ` · ${inbox.length} pending` : ""}
            </button>
          </div>
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
        </div>
      </div>

      {msg && <div className={"banner " + (msg.includes("failed") ? "danger" : "info")} style={{ marginBottom: 14 }}>
        <I.info size={14}/><span>{msg}</span>
      </div>}

      {tab === "discover" && (
        <div className="card">
          <CardHead icon={<I.box size={14}/>} tone="cyan" title="Available services" meta={<span>{services.length} found</span>}/>
          {loading && <div className="dim" style={{ padding: 16 }}>Loading…</div>}
          {!loading && services.length === 0 && (
            <div className="dim" style={{ padding: 16 }}>No services discovered. Pair with peers or join groups that host services.</div>
          )}
          <div className="col" style={{ gap: 10, padding: services.length ? 4 : 0 }}>
            {services.map((entry, i) => {
              const svc = entry.service || {};
              const tags = fld(svc, "tags", []) || [];
              const open = canOpen(entry);
              return (
                <div key={i} className={"card pad-lg svc-card" + (open ? " svc-open" : "")} style={{ background: "var(--bg-card-2)" }}
                     onClick={open ? () => setViewing(entry) : undefined}>
                  <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                    <span className="ico-tile purple" style={{ width: 32, height: 32 }}><I.terminal size={16}/></span>
                    <div style={{ flex: 1 }}>
                      <div className="row" style={{ gap: 8, alignItems: "center" }}>
                        <span style={{ fontWeight: 600 }}>{fld(svc, "name", "service")}</span>
                        {fld(svc, "version", "") && <span className="mono dim" style={{ fontSize: 11 }}>v{svc.version}</span>}
                        <Pill tone={isFreeEntry(entry) ? "emerald" : "ghost"}>{fld(svc, "access", "gated")}</Pill>
                        {fld(svc, "replicable", false) && <Pill tone="cyan">replicable</Pill>}
                        {hasGrantFor(entry) && <Pill tone="emerald">access</Pill>}
                      </div>
                      <div className="dim" style={{ fontSize: 12, marginTop: 3 }}>{fld(svc, "description", "")}</div>
                      <div className="mono dim" style={{ fontSize: 11, marginTop: 4 }}>by {provName(entry)}</div>
                      {tags.length > 0 && <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                        {tags.map((t, j) => <span key={j} className="pill ghost">{t}</span>)}
                      </div>}
                    </div>
                    {open
                      ? <I.arr size={16} style={{ color: "var(--t-faint)", flexShrink: 0, marginTop: 4 }}/>
                      : <div onClick={e => e.stopPropagation()}>
                          <button className="btn accent sm" onClick={() => requestAccess(entry)}><I.key size={13}/> Request access</button>
                        </div>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {tab === "discover" && replicas.length > 0 && (
        <div className="card" style={{ marginTop: 14 }}>
          <CardHead icon={<I.play size={14}/>} tone="amber" title="Replicas running on this machine" meta={<span>{replicas.length}</span>}/>
          <table className="t">
            <tbody>
              {replicas.map((r, i) => (
                <tr key={r.replica_id || i}>
                  <td>
                    <div className="name">{r.service_name || r.replica_id}</div>
                    <div className="mono dim" style={{ fontSize: 11 }}>
                      {r.runner || ""}{r.image ? ` · ${r.image}` : ""}{(r.ports || []).length ? ` · ports ${[].concat(r.ports).join(", ")}` : ""}
                    </div>
                  </td>
                  <td><Pill tone={r.running ? "emerald" : "ghost"} dot>{r.running ? "running" : "stopped"}</Pill></td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn ghost sm u-danger" onClick={() => act("Replica stopped", () => api.post(`/local/replicas/${encodeURIComponent(r.replica_id)}/stop`))}>Stop</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {tab === "discover" && cookbooks.length > 0 && (
        <div className="card" style={{ marginTop: 14 }}>
          <CardHead icon={<I.copy size={14}/>} tone="cyan" title="Cookbooks copied to this machine" meta={<span>{cookbooks.length}</span>}/>
          <div className="col" style={{ gap: 4, padding: "8px 14px 12px" }}>
            {cookbooks.map((c, i) => (
              <div key={i} className="mono" style={{ fontSize: 11 }} title={c.path}>{resolveIds(c.filename)} <span className="dim">· {Math.round((c.size || 0) / 1024)} KB</span></div>
            ))}
          </div>
        </div>
      )}

      {tab === "mine" && (
        <div className="card">
          <CardHead icon={<I.terminal size={14}/>} tone="purple" title="Services you host">
            <button className="btn accent sm" style={{ marginLeft: "auto" }} onClick={() => setEditing({ idx: -1 })}>
              <I.plus size={13}/> Deploy a service
            </button>
          </CardHead>
          {mine.length === 0 && (
            <div className="dim" style={{ padding: 16 }}>
              Nothing hosted yet. A service is anything listening on a local port — an LLM, a database, an API —
              that you let peers reach through a secure tunnel. Click <strong>Deploy a service</strong> to register one.
            </div>
          )}
          <div className="col" style={{ gap: 10, padding: mine.length ? 4 : 0 }}>
            {mine.map((s, i) => (
              <div key={i} className="card pad-lg svc-card svc-open" style={{ background: "var(--bg-card-2)" }}
                   onClick={() => setEditing({ idx: i })}>
                <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                  <span className="ico-tile purple" style={{ width: 32, height: 32 }}><I.terminal size={16}/></span>
                  <div style={{ flex: 1 }}>
                    <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                      <span style={{ fontWeight: 600 }}>{s.name}</span>
                      {s.version && <span className="mono dim" style={{ fontSize: 11 }}>v{s.version}</span>}
                      <Pill tone={s.access === "free" ? "emerald" : "ghost"}>{s.access || "free"}</Pill>
                      {s.replicable && <Pill tone="cyan">replicable</Pill>}
                      {(s.components || []).length > 0 && <Pill tone="ghost">bundle · {s.components.length}</Pill>}
                      {(s.run && (s.run.image || s.run.cmd)) ? <Pill tone="amber">auto-run</Pill> : null}
                    </div>
                    <div className="dim" style={{ fontSize: 12, marginTop: 3 }}>{s.description}</div>
                    <div className="mono dim" style={{ fontSize: 11, marginTop: 4 }}>
                      {s.local_host || "127.0.0.1"}:{s.local_port || "?"}{s.pump ? ` · pump: ${s.pump}` : ""}
                    </div>
                    {(s.tags || []).length > 0 && <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                      {s.tags.map((t, j) => <span key={j} className="pill ghost">{t}</span>)}
                    </div>}
                  </div>
                  <I.arr size={16} style={{ color: "var(--t-faint)", flexShrink: 0, marginTop: 4 }}/>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "access" && (
        <div className="card">
          <CardHead icon={<I.key size={14}/>} tone="emerald" title="Access you hold" meta={<span>{held.length}</span>}/>
          {held.length === 0 && <div className="dim" style={{ padding: 16 }}>No access grants yet. Request access from Discover.</div>}
          <table className="t">
            <tbody>
              {held.map((g, i) => {
                const conn = dbConn[gId(g)];
                return (
                <React.Fragment key={i}>
                <tr>
                  <td>
                    <div className="name">{gName(g)}</div>
                    <div className="mono dim" style={{ fontSize: 11 }}>{gProvider(g)}</div>
                  </td>
                  <td>{g.status && <Pill tone={g.status === "active" ? "emerald" : "ghost"}>{g.status}</Pill>}</td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                      <button className="btn ghost sm" onClick={() => connectGrant(g)}><I.link size={13}/> Connect</button>
                      {(g.status === "approved" || g.status === "active") &&
                        <button className="btn ghost sm" onClick={() => getDbCreds(g)} title="Fetch DB login (for database services)"><I.key size={13}/> DB credentials</button>}
                      <button className="btn ghost sm" onClick={() => act("Disconnect", () => api.post(`/local/service_grants/${encodeURIComponent(gId(g))}/disconnect`))}>Disconnect</button>
                    </div>
                  </td>
                </tr>
                {conn && (
                  <tr><td colSpan={3} style={{ background: "rgba(52,211,153,0.06)", padding: "8px 14px" }}>
                    <div className="col" style={{ gap: 4, fontSize: 12 }}>
                      <div><span className="dim">database</span> <code className="mono">{conn.database}</code>　<span className="dim">user</span> <code className="mono">{conn.user}</code>　<span className="dim">password</span> <code className="mono">{conn.password}</code></div>
                      <div className="row" style={{ gap: 6, alignItems: "center" }}>
                        <code className="mono" style={{ fontSize: 11, wordBreak: "break-all", flex: 1 }}>{dbDsn(conn, ports[gId(g)])}</code>
                        <button className="btn ghost sm" onClick={() => { navigator.clipboard.writeText(dbDsn(conn, ports[gId(g)])); flash("Copied"); }}><I.copy size={12}/></button>
                      </div>
                      {!ports[gId(g)] && <div className="hint">Click Connect first to open the tunnel and fill in the local port.</div>}
                    </div>
                  </td></tr>
                )}
                </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {tab === "issued" && inbox.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <CardHead icon={<I.bell size={14}/>} tone="amber" title="Access requests waiting on you" meta={<span>{inbox.length}</span>}/>
          <div className="col" style={{ gap: 8, padding: "8px 14px 12px" }}>
            {inbox.map((r, i) => (
              <div key={gId(r) || i} className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <div style={{ flex: 1, minWidth: 180 }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>{gName(r)}</span>
                  <span className="dim" style={{ fontSize: 12 }}> — requested by {r.consumer_name || r.requester_name || r.grantee_name || (r.consumer_pubkey || "").slice(0, 10) || "peer"}</span>
                </div>
                <button className="btn accent sm" onClick={() => act("Approved", () => api.post(`/local/service_requests/${encodeURIComponent(gId(r))}/approve`))}>Approve</button>
                <button className="btn ghost sm" onClick={() => act("Denied", () => api.post(`/local/service_requests/${encodeURIComponent(gId(r))}/deny`))}>Deny</button>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "issued" && (
        <div className="card">
          <CardHead icon={<I.share size={14}/>} tone="amber" title="Access you've granted" meta={<span>{issued.length}</span>}/>
          {issued.length === 0 && <div className="dim" style={{ padding: 16 }}>You haven't granted any service access.</div>}
          <table className="t">
            <tbody>
              {issued.map((g, i) => (
                <tr key={i}>
                  <td>
                    <div className="name">{gName(g)}</div>
                    <div className="mono dim" style={{ fontSize: 11 }}>to {g.grantee_name || g.grantee_pubkey || g.holder || ""}</div>
                  </td>
                  <td>{g.status && <Pill tone={g.status === "active" ? "emerald" : "ghost"}>{g.status}</Pill>}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn ghost sm u-danger" onClick={() => act("Revoke", () => api.post(`/local/service_grants/${encodeURIComponent(gId(g))}/revoke`))}><I.x size={13}/> Revoke</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
};

export { ServicesScreen };
