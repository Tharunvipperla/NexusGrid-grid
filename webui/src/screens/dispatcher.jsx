/* Dispatcher — deploy a workload to the grid (port of the classic "Deploy
 * Workload" form). Simple mode builds a one-task workflow; DAG mode takes the
 * blueprint JSON verbatim. The workspace folder is zipped client-side (JSZip)
 * and POSTed to /local/add_workflow — same contract as the classic UI.
 *
 * Consent flows (dependency verification, cloud task-data terms on 412) are
 * in-page panels, never popups. Cloud/Drive data sources stay in the classic
 * UI for now. */
import React from "react";
import JSZip from "jszip";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Field, Chk, Toggle, CardHead, Pill, DagGraph, Help, Disclosure, CodeField } from "../components.jsx";
import { toast } from "../toast.jsx";
import { parseDag, csvArr, dagIssues, layoutDag, wouldCycle } from "../dag.js";

const DEFAULT_DAG = JSON.stringify([
  { id: "step1_download", runtime: "docker", image: "python:3.11-slim", entrypoint: "python download.py", depends_on: [] },
  { id: "step2_process_chunk", runtime: "docker", image: "python:3.11-slim", setup_cmd: "pip install pandas", entrypoint: "python process.py", depends_on: ["step1_download"], slice_count: 3 },
  { id: "step3_merge", runtime: "docker", image: "python:3.11-slim", entrypoint: "python merge.py", depends_on: ["step2_process_chunk"] },
], null, 2);

/* ── A3: DAG authoring — templates, validation, and a structured step
 * builder so users aren't hand-editing a cramped JSON box. The JSON string
 * stays the source of truth (submit contract unchanged); the builder just
 * parses → edits → re-serializes. */
const STEP_TEMPLATE = () => ({ id: "", runtime: "docker", image: "python:3.11-slim", setup_cmd: "", entrypoint: "", depends_on: [] });

const DAG_TEMPLATES = {
  "ETL: download → process×N → merge": DEFAULT_DAG,
  "Two-step (build → test)": JSON.stringify([
    { id: "build", runtime: "docker", image: "python:3.11-slim", entrypoint: "python build.py", depends_on: [] },
    { id: "test", runtime: "docker", image: "python:3.11-slim", entrypoint: "python -m pytest", depends_on: ["build"] },
  ], null, 2),
  "Fan-out map (split → map×N → reduce)": JSON.stringify([
    { id: "split", runtime: "docker", image: "python:3.11-slim", entrypoint: "python split.py", depends_on: [] },
    { id: "map", runtime: "docker", image: "python:3.11-slim", entrypoint: "python map.py", depends_on: ["split"], slice_count: 4 },
    { id: "reduce", runtime: "docker", image: "python:3.11-slim", entrypoint: "python reduce.py", depends_on: ["map"] },
  ], null, 2),
};

const DagBuilder = ({ value, onChange }) => {
  const nodes = parseDag(value);
  if (!nodes) return (
    <div className="hint" style={{ padding: 10 }}>
      The blueprint isn't valid JSON, so the visual builder can't load it — fix it in the JSON tab, or load a template.
    </div>
  );
  const commit = (next) => onChange(JSON.stringify(next, null, 2));
  const upd = (i, patch) => commit(nodes.map((n, j) => j === i ? { ...n, ...patch } : n));
  const addStep = () => commit([...nodes, { ...STEP_TEMPLATE(), id: `step${nodes.length + 1}` }]);
  const dup = (i) => { const next = [...nodes]; next.splice(i + 1, 0, { ...nodes[i], id: (nodes[i].id || "step") + "_copy", depends_on: [] }); commit(next); };
  const del = (i) => commit(nodes.filter((_, j) => j !== i));
  const toggleDep = (i, depId) => {
    const cur = new Set(nodes[i].depends_on || []);
    cur.has(depId) ? cur.delete(depId) : cur.add(depId);
    upd(i, { depends_on: [...cur] });
  };
  return (
    <div className="col" style={{ gap: 10 }}>
      {nodes.map((n, i) => (
        <div key={i} className="card" style={{ padding: 12 }}>
          <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span className="ico-tile cyan" style={{ width: 22, height: 22, fontSize: 11 }}>{i + 1}</span>
            <input className="input mono" style={{ flex: 1, fontWeight: 600 }} placeholder="step id" value={n.id || ""} onChange={e => upd(i, { id: e.target.value })}/>
            <select className="input" style={{ width: 110 }} value={n.runtime || "docker"} onChange={e => upd(i, { runtime: e.target.value })}>
              <option value="docker">docker</option><option value="wasm">wasm</option><option value="native">native</option>
            </select>
            <button className="btn ghost sm" title="Duplicate step" onClick={() => dup(i)}><I.copy size={13}/></button>
            <button className="btn ghost sm" title="Remove step" onClick={() => del(i)}><I.x size={13}/></button>
          </div>
          {(n.runtime || "docker") === "docker" && (
            <Field label="Image"><input className="input mono" value={n.image || ""} onChange={e => upd(i, { image: e.target.value })}/></Field>
          )}
          <div className="field-row" style={{ marginTop: 8 }}>
            <Field label="Run command"><input className="input mono" placeholder="python step.py" value={n.entrypoint || ""} onChange={e => upd(i, { entrypoint: e.target.value })}/></Field>
            <Field label="Setup (optional)"><input className="input mono" placeholder="pip install -r requirements.txt" value={n.setup_cmd || ""} onChange={e => upd(i, { setup_cmd: e.target.value })}/></Field>
          </div>
          <div className="field-row" style={{ marginTop: 8 }}>
            <Field label="Parallel slices" hint="fan this step out N ways (optional)">
              <input className="input mono" type="number" min={1} style={{ width: 110 }} value={n.slice_count || ""}
                     onChange={e => upd(i, { slice_count: e.target.value ? parseInt(e.target.value, 10) : undefined })}/>
            </Field>
          </div>
          <div style={{ marginTop: 8 }}>
            <div className="label" style={{ marginBottom: 4 }}>Depends on</div>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              {nodes.map((o, j) => (j !== i && o && o.id) ? (() => {
                const on = (n.depends_on || []).includes(o.id);
                return <button key={o.id} className={"btn sm " + (on ? "accent" : "ghost")} onClick={() => toggleDep(i, o.id)}>{on ? "✓ " : ""}{o.id}</button>;
              })() : null)}
              {nodes.length === 1 && <span className="hint">add another step to chain dependencies</span>}
            </div>
          </div>
          <div style={{ marginTop: 8 }}>
            <div className="label" style={{ marginBottom: 4 }}>Targeting (optional) <span className="hint" style={{ fontWeight: 400 }}>— overrides the dispatch defaults for this step only</span></div>
            <div className="field-row tri">
              <Field label="Groups (CSV)"><input className="input mono" placeholder="groupA, groupB" value={(n.target_groups || []).join(", ")} onChange={e => upd(i, { target_groups: csvArr(e.target.value) })}/></Field>
              <Field label="Nodes (CSV)" hint="trusted worker IPs"><input className="input mono" placeholder="10.0.0.5" value={(n.preferred_workers || []).join(", ")} onChange={e => upd(i, { preferred_workers: csvArr(e.target.value) })}/></Field>
              <Field label="Required tags (CSV)"><input className="input mono" placeholder="gpu, highmem" value={(n.required_tags || []).join(", ")} onChange={e => upd(i, { required_tags: csvArr(e.target.value) })}/></Field>
            </div>
            <div className="row" style={{ gap: 18, marginTop: 8, alignItems: "center" }}>
              <div className="row" style={{ gap: 6, alignItems: "center" }}>
                <Chk on={!!n.require_gpu} onChange={v => upd(i, { require_gpu: v || undefined })}/>
                <span style={{ fontSize: 12 }}>Require GPU</span>
              </div>
              <Field label="Priority (0–100)">
                <input className="input" type="number" min={0} max={100} style={{ width: 90 }} placeholder="default"
                       value={n.priority ?? ""} onChange={e => upd(i, { priority: e.target.value === "" ? undefined : parseInt(e.target.value, 10) })}/>
              </Field>
            </div>
          </div>
        </div>
      ))}
      <button className="btn ghost" onClick={addStep}><I.plus size={13}/> Add step</button>
    </div>
  );
};

/* A3: interactive graph editor — the third "Graph" view. Same depth-column
 * layout as the read-only DagGraph, but nodes and edges are live: click a node
 * to edit it inline, use its ◇ handle to start wiring a dependency (then click
 * the target), and click an edge to remove it. The JSON string stays the
 * source of truth — every action re-serialises through onChange. */
const DagCanvas = ({ value, onChange, note }) => {
  const nodes = parseDag(value);
  const [sel, setSel] = React.useState(null);
  const [linkFrom, setLinkFrom] = React.useState(null);
  const [zoom, setZoom] = React.useState(1);  // scale the whole drawing so big DAGs (100–200 steps) stay navigable
  const wrapRef = React.useRef(null);
  if (!nodes) return (
    <div className="hint" style={{ padding: 10 }}>
      The blueprint isn't valid JSON, so the graph editor can't load it — fix it in the JSON tab, or load a template.
    </div>
  );
  const commit = (next) => onChange(JSON.stringify(next, null, 2));
  const at = (id) => nodes.find(n => n && n.id === id);
  const upd = (id, patch) => commit(nodes.map(n => (n && n.id === id) ? { ...n, ...patch } : n));
  const addNode = () => {
    const ids = new Set(nodes.map(n => n && n.id));
    let k = nodes.length + 1, id = `step${k}`;
    while (ids.has(id)) { k += 1; id = `step${k}`; }
    commit([...nodes, { ...STEP_TEMPLATE(), id }]); setSel(id); setLinkFrom(null);
  };
  const delNode = (id) => {
    commit(nodes.filter(n => !(n && n.id === id))
      .map(n => ({ ...n, depends_on: (n.depends_on || []).filter(d => d !== id) })));
    if (sel === id) setSel(null); if (linkFrom === id) setLinkFrom(null);
  };
  const renameNode = (oldId, nid) => {
    commit(nodes.map(n => n.id === oldId
      ? { ...n, id: nid }
      : { ...n, depends_on: (n.depends_on || []).map(d => d === oldId ? nid : d) }));
    setSel(nid);
  };
  const removeEdge = (depId, nodeId) => upd(nodeId, { depends_on: (at(nodeId).depends_on || []).filter(d => d !== depId) });
  const clickNode = (id) => {
    if (linkFrom && linkFrom !== id) {
      const cur = at(id).depends_on || [];
      if (cur.includes(linkFrom)) { note("ok", "That dependency already exists."); setLinkFrom(null); return; }
      if (wouldCycle(nodes, id, linkFrom)) { note("danger", "That link would create a dependency cycle."); setLinkFrom(null); return; }
      upd(id, { depends_on: [...cur, linkFrom] }); setSel(id); setLinkFrom(null); return;
    }
    setSel(id); setLinkFrom(null);
  };
  const { pos, W, H } = layoutDag(nodes);
  const selNode = sel != null ? at(sel) : null;
  const zoomBy = (f) => setZoom(z => Math.max(0.25, Math.min(2, +(z * f).toFixed(2))));
  const fitWidth = () => { const el = wrapRef.current; if (el && W) setZoom(Math.max(0.25, Math.min(1, (el.clientWidth - 6) / W))); };
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn ghost sm" onClick={addNode}><I.plus size={13}/> Add node</button>
        {linkFrom
          ? <span className="hint" style={{ color: "var(--cyan)" }}>Linking from <b className="mono">{linkFrom}</b> — click the node that should depend on it, or <a onClick={() => setLinkFrom(null)} style={{ cursor: "pointer", textDecoration: "underline" }}>cancel</a></span>
          : <span className="hint">click a node to edit · use its ◇ handle to wire a dependency · click an edge to remove it</span>}
        <div className="row" style={{ gap: 4, alignItems: "center", marginLeft: "auto" }}>
          <span className="hint">{nodes.length} step{nodes.length > 1 ? "s" : ""}</span>
          <button className="btn ghost sm" title="Zoom out" onClick={() => zoomBy(0.8)}>−</button>
          <span className="hint mono" style={{ minWidth: 40, textAlign: "center" }}>{Math.round(zoom * 100)}%</span>
          <button className="btn ghost sm" title="Zoom in" onClick={() => zoomBy(1.25)}>+</button>
          <button className="btn ghost sm" title="Fit to width" onClick={fitWidth}>Fit</button>
        </div>
      </div>
      <div ref={wrapRef} style={{ maxHeight: 520, overflow: "auto", border: "1px solid var(--br-mute)", borderRadius: 8 }}>
        <svg width={W * zoom} height={H * zoom} viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
          {nodes.flatMap(n => ((n.depends_on || []).map(d => {
            const a = pos[d], b = pos[n.id];
            if (!a || !b) return null;
            const mx = (a.x + b.x) / 2;
            const dd = `M ${a.x + 46} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x - 46} ${b.y}`;
            return (
              <g key={d + ">" + n.id} style={{ cursor: "pointer" }} onClick={() => removeEdge(d, n.id)}>
                <path d={dd} fill="none" stroke="transparent" strokeWidth="12"/>
                <path d={dd} fill="none" stroke="var(--br)" strokeWidth="1.6"/>
                <title>remove dependency {d} → {n.id}</title>
              </g>
            );
          })))}
          {nodes.map(n => {
            const p = pos[n.id]; if (!p) return null;
            const on = sel === n.id, lf = linkFrom === n.id;
            return (
              <g key={n.id}>
                <rect x={p.x - 46} y={p.y - 18} width={92} height={36} rx={8}
                      fill="var(--bg-card, #15171c)"
                      stroke={lf ? "var(--cyan)" : on ? "var(--accent, #818cf8)" : "var(--t-dim)"}
                      strokeWidth={on || lf ? "2.2" : "1.4"} style={{ cursor: "pointer" }}
                      onClick={() => clickNode(n.id)}/>
                <text x={p.x} y={p.y + 4} textAnchor="middle"
                      style={{ font: "10.5px var(--f-mono)", fill: "var(--t)", pointerEvents: "none" }}>
                  {String(n.id).length > 12 ? String(n.id).slice(0, 11) + "…" : n.id}
                </text>
                <g style={{ cursor: "pointer" }} onClick={() => { setSel(null); setLinkFrom(lf ? null : n.id); }}>
                  <title>wire a dependency from {n.id}</title>
                  <circle cx={p.x + 46} cy={p.y} r={7.5} fill="var(--bg-card, #15171c)"
                          stroke={lf ? "var(--cyan)" : "var(--t-dim)"} strokeWidth="1.4"/>
                  <text x={p.x + 46} y={p.y + 3.5} textAnchor="middle"
                        style={{ font: "10px var(--f-mono)", fill: lf ? "var(--cyan)" : "var(--t-dim)", pointerEvents: "none" }}>◇</text>
                </g>
              </g>
            );
          })}
        </svg>
      </div>
      {selNode && (
        <div className="card" style={{ padding: 12 }}>
          <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
            <span className="ico-tile cyan" style={{ width: 22, height: 22, fontSize: 11 }}><I.cog size={12}/></span>
            <span className="label" style={{ flex: 1 }}>Edit node</span>
            <button className="btn ghost sm" title="Remove node" onClick={() => delNode(selNode.id)}><I.x size={13}/></button>
          </div>
          <div className="field-row">
            <Field label="Step id"><input className="input mono" value={selNode.id || ""} onChange={e => renameNode(selNode.id, e.target.value)}/></Field>
            <Field label="Runtime">
              <select className="input" value={selNode.runtime || "docker"} onChange={e => upd(selNode.id, { runtime: e.target.value })}>
                <option value="docker">docker</option><option value="wasm">wasm</option><option value="native">native</option>
              </select>
            </Field>
          </div>
          {(selNode.runtime || "docker") === "docker" && (
            <div style={{ marginTop: 8 }}>
              <Field label="Image"><input className="input mono" value={selNode.image || ""} onChange={e => upd(selNode.id, { image: e.target.value })}/></Field>
            </div>
          )}
          <div className="field-row" style={{ marginTop: 8 }}>
            <Field label="Run command"><input className="input mono" placeholder="python step.py" value={selNode.entrypoint || ""} onChange={e => upd(selNode.id, { entrypoint: e.target.value })}/></Field>
            <Field label="Setup (optional)"><input className="input mono" placeholder="pip install -r requirements.txt" value={selNode.setup_cmd || ""} onChange={e => upd(selNode.id, { setup_cmd: e.target.value })}/></Field>
          </div>
          <div style={{ marginTop: 8 }}>
            <Field label="Parallel slices" hint="fan this step out N ways (optional)">
              <input className="input mono" type="number" min={1} style={{ width: 110 }} value={selNode.slice_count || ""}
                     onChange={e => upd(selNode.id, { slice_count: e.target.value ? parseInt(e.target.value, 10) : undefined })}/>
            </Field>
          </div>
        </div>
      )}
    </div>
  );
};

const PRIORITY_HINTS = {
  "40": "Normal — yields to busier work when resources are contested.",
  "60": "Medium — balanced scheduling weight (the default).",
  "80": "High — runs ahead of normal work when queued.",
  "95": "Very high — jumps nearly every queue; use sparingly.",
};

const fmtBytes = (n) => {
  const u = ["B", "KB", "MB", "GB"]; let i = 0; n = Number(n) || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};

const Sec = ({ icon, tone, title, sub, children }) => (
  <div className="card pad-lg">
    <div className="fsec-head">
      <span className={"ico-tile " + (tone || "emerald")} style={{ width: 28, height: 28 }}>{icon}</span>
      <h4>{title}</h4>
      {sub && <span className="fsec-sub">{sub}</span>}
    </div>
    {children}
  </div>
);

/* DAG #4: full-page editor for one blueprint — view/edit the JSON, load it into
 * the dispatcher, save changes, save a copy, or delete. Full-page (not a popup)
 * so big blueprints scroll naturally. */
/* Shared "Where it runs" targeting controls — used by both the dispatcher form
 * and the dispatch-profile editor so they edit groups / blocked members /
 * manual node pins identically. Group-member loading and all search state are
 * internal; the parent owns only the four targeting values + their setters and
 * supplies the live groups / workerPool lists. Setters must accept a functional
 * updater (both React useState setters and the profile editor's do). */
const Targeting = ({ groups, workerPool, selGroups, setSelGroups, blocked, setBlocked, manual, setManual, selWorkers, setSelWorkers }) => {
  selGroups = selGroups || []; blocked = blocked || []; selWorkers = selWorkers || []; manual = !!manual;
  const [groupMembers, setGroupMembers] = React.useState([]);
  const [groupQuery, setGroupQuery] = React.useState("");
  const [groupFocus, setGroupFocus] = React.useState(false);
  const [memberQuery, setMemberQuery] = React.useState("");
  const [workerQuery, setWorkerQuery] = React.useState("");

  React.useEffect(() => {
    let dead = false;
    (async () => {
      const rows = [];
      for (const gid of selGroups) {
        try {
          const d = await api.get(`/local/groups/${encodeURIComponent(gid)}`);
          for (const m of (d.members || [])) {
            if (m.node_id && m.pubkey !== d.my_pubkey) {
              rows.push({ gid, gname: d.name || gid, node_id: m.node_id, name: m.display_name || (m.pubkey || "").slice(0, 8) });
            }
          }
        } catch (_) {}
      }
      if (!dead) {
        setGroupMembers(rows);
        const valid = new Set(rows.map(r => r.node_id));
        setBlocked(b => (b || []).filter(id => valid.has(id)));
      }
    })();
    return () => { dead = true; };
  }, [selGroups.join(",")]);

  const toggleIn = (list, setList, val) => setList((list || []).includes(val) ? (list || []).filter(x => x !== val) : [...(list || []), val]);
  const filteredPool = workerPool.filter(w => !workerQuery || `${w.name} ${w.display_ip} ${w.role}`.toLowerCase().includes(workerQuery.toLowerCase()));
  const filteredGroups = groups.filter(g => !groupQuery || String(g.name || g.group_id || g.id || "").toLowerCase().includes(groupQuery.toLowerCase()));
  const filteredMembers = groupMembers.filter(m => !memberQuery || `${m.name} ${m.gname}`.toLowerCase().includes(memberQuery.toLowerCase()));

  return (
    <>
      {groups.length > 0 && (
        <>
          <div className="label" style={{ marginBottom: 8 }}>Run on groups (optional) <span className="hint" style={{ fontWeight: 400 }}>— only members holding task:run; combined with manual picks below</span></div>
          {selGroups.length > 0 && (
            <div className="row" style={{ gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
              {selGroups.map(gid => {
                const g = groups.find(x => (x.group_id || x.id) === gid);
                return (
                  <span key={gid} className="chip">
                    {(g && g.name) || gid}
                    <button className="icon-btn" onClick={() => setSelGroups(selGroups.filter(x => x !== gid))}><I.x size={11}/></button>
                  </span>
                );
              })}
            </div>
          )}
          <div style={{ position: "relative", maxWidth: 360 }}>
            <input className="input" placeholder={groups.length > 6 ? `Search ${groups.length} groups to add…` : "Add a group…"}
                   value={groupQuery} onChange={e => setGroupQuery(e.target.value)}
                   onFocus={() => setGroupFocus(true)} onBlur={() => setTimeout(() => setGroupFocus(false), 150)}/>
            {groupFocus && (
              <div className="card" style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30, marginTop: 4, maxHeight: 240, overflowY: "auto", boxShadow: "0 10px 32px rgba(0,0,0,0.5)" }}>
                {filteredGroups.filter(g => !selGroups.includes(g.group_id || g.id)).slice(0, 30).map(g => {
                  const gid = g.group_id || g.id;
                  return (
                    <div key={gid} className="rail-item" style={{ cursor: "pointer" }} onMouseDown={() => { setSelGroups([...selGroups, gid]); setGroupQuery(""); }}>
                      <div className="rail-icon"><I.users size={13}/></div>
                      <div className="rail-text">{g.name || gid}</div>
                    </div>
                  );
                })}
                {filteredGroups.filter(g => !selGroups.includes(g.group_id || g.id)).length === 0 && (
                  <div className="hint" style={{ padding: 12 }}>{groupQuery ? "No groups match." : "All groups already added."}</div>
                )}
                {filteredGroups.filter(g => !selGroups.includes(g.group_id || g.id)).length > 30 && (
                  <div className="hint" style={{ padding: "8px 12px" }}>More matches — keep typing to narrow.</div>
                )}
              </div>
            )}
          </div>
          {groupMembers.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div className="hint" style={{ marginBottom: 6 }}>Group members — uncheck anyone you don't want running this task:</div>
              {groupMembers.length > 8 && (
                <input className="input mono" placeholder="Search members…" style={{ marginBottom: 10, maxWidth: 320 }} value={memberQuery} onChange={e => setMemberQuery(e.target.value)}/>
              )}
              <div className="row" style={{ gap: 14, flexWrap: "wrap", maxHeight: 180, overflowY: "auto", alignContent: "flex-start" }}>
                {filteredMembers.map(m => (
                  <div key={m.gid + m.node_id} className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }} onClick={() => toggleIn(blocked, setBlocked, m.node_id)}>
                    <Chk on={!blocked.includes(m.node_id)}/>
                    <span style={{ fontSize: 12 }}>{m.name} <span className="dim">({m.gname})</span></span>
                  </div>
                ))}
              </div>
              {blocked.length > 0 && <div className="hint" style={{ marginTop: 6 }}>{blocked.length} member{blocked.length === 1 ? "" : "s"} excluded</div>}
            </div>
          )}
          <hr className="divider" style={{ margin: "16px 0" }}/>
        </>
      )}

      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <Toggle on={manual} onChange={setManual}/>
        <span style={{ fontSize: 13 }}>Manually pick target workers</span>
        <span className="hint">{manual ? "only the checked workers below are eligible" : "auto mode picks the best available worker by RAM/CPU/capacity"}</span>
      </div>
      {manual && (
        <div style={{ marginTop: 12 }}>
          {workerPool.length > 6 && (
            <input className="input mono" placeholder="Filter by name, IP, or role…" style={{ marginBottom: 10, maxWidth: 320 }} value={workerQuery} onChange={e => setWorkerQuery(e.target.value)}/>
          )}
          {filteredPool.length === 0 && <div className="hint">No trusted workers{workerQuery ? " match the filter" : " yet — pair with nodes in the Network screen"}.</div>}
          <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
            {filteredPool.map(w => (
              <div key={w.ip} className="row" style={{ gap: 8, alignItems: "center", cursor: "pointer" }} onClick={() => toggleIn(selWorkers, setSelWorkers, w.ip)}>
                <Chk on={selWorkers.includes(w.ip)}/>
                <span style={{ fontSize: 13 }}>{w.name || w.display_ip}</span>
                <Pill tone="ghost">{w.role}</Pill>
                <span className="hint mono">{w.display_ip}</span>
              </div>
            ))}
          </div>
          {selWorkers.length > 0 && <div className="hint" style={{ marginTop: 8 }}>{selWorkers.length} worker{selWorkers.length === 1 ? "" : "s"} selected</div>}
        </div>
      )}
    </>
  );
};

const TemplateEditor = ({ editing, onApply, onSave, onDelete, onBack }) => {
  const [json, setJson] = React.useState(editing.json || "[]");
  const [name, setName] = React.useState(editing.isNew ? "" : editing.name);
  const [copyName, setCopyName] = React.useState("");
  const nodes = parseDag(json);
  const issues = nodes ? dagIssues(nodes) : ["Blueprint is not valid JSON."];
  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <button className="icon-btn" onClick={onBack} title="Back to templates"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>{editing.isNew ? "New blueprint" : editing.name}</div>
            <div className="page-sub">View or edit the blueprint JSON, then load it or save.</div>
          </div>
        </div>
        <div className="page-tools">
          {nodes
            ? <Pill tone={issues.length ? "amber" : "emerald"}>{issues.length ? `${issues.length} issue${issues.length > 1 ? "s" : ""}` : `✓ ${nodes.length} steps`}</Pill>
            : <Pill tone="rose">invalid JSON</Pill>}
        </div>
      </div>
      <div className="card pad-lg">
        {editing.isNew && (
          <div style={{ marginBottom: 12 }}>
            <Field label="Template name" hint="letters, digits, - and _">
              <input className="input mono" value={name} maxLength={80} autoFocus placeholder="my_pipeline" onChange={e => setName(e.target.value)}/>
            </Field>
          </div>
        )}
        <CodeField label="Blueprint (JSON)" language="json" rows={18} value={json} onChange={setJson}/>
        {nodes && issues.length > 0 && (
          <div className="col" style={{ gap: 3, marginTop: 8 }}>
            {issues.slice(0, 8).map((m, i) => <div key={i} className="hint" style={{ color: "var(--amber, #fbbf24)" }}>• {m}</div>)}
          </div>
        )}
        {nodes && issues.length === 0 && (
          <div style={{ marginTop: 12 }}>
            <div className="label" style={{ marginBottom: 6 }}>Graph preview</div>
            <DagGraph nodes={nodes} height={Math.max(120, nodes.length * 28)}/>
          </div>
        )}
        <div className="row" style={{ gap: 8, marginTop: 16, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn accent" disabled={!nodes} onClick={() => onApply(nodes)}><I.download size={14}/> Load into dispatcher</button>
          <button className="btn ghost" disabled={!nodes || (editing.isNew && !name.trim())}
                  onClick={() => { onSave((editing.isNew ? name : editing.name).trim(), json); onBack(); }}>
            <I.check size={14}/> {editing.isNew ? "Save blueprint" : "Save changes"}
          </button>
          {!editing.isNew && <>
            <input className="input mono" placeholder="Save as copy…" style={{ width: 170 }} value={copyName} onChange={e => setCopyName(e.target.value)}/>
            <button className="btn ghost" disabled={!nodes || !copyName.trim()} onClick={() => { onSave(copyName.trim(), json); onBack(); }}><I.copy size={14}/> Save copy</button>
            <button className="btn ghost u-danger" style={{ marginLeft: "auto" }} onClick={() => { onDelete(editing.name); onBack(); }}><I.trash size={14}/> Delete</button>
          </>}
        </div>
      </div>
    </>
  );
};

/* DAG #4: full-page template manager (gallery + editor) — not a popup, so many
 * blueprints scroll cleanly. Open a card to edit/view; multi-select to merge. */
const TemplateManager = ({ templates, currentJson, onApply, onSave, onDelete, onClose }) => {
  const [editing, setEditing] = React.useState(null);  // {name, json, isNew}
  const [sel, setSel] = React.useState([]);

  if (editing) {
    return <TemplateEditor editing={editing} onApply={onApply} onSave={onSave}
                           onDelete={onDelete} onBack={() => setEditing(null)}/>;
  }

  const toggle = (n) => setSel(s => s.includes(n) ? s.filter(x => x !== n) : [...s, n]);
  const open = (t) => setEditing({ name: t.name, json: JSON.stringify(t.steps || [], null, 2), isNew: false });
  const mergeAndApply = () => {
    const chosen = templates.filter(t => sel.includes(t.name));
    if (!chosen.length) return;
    let steps;
    if (chosen.length === 1) { steps = chosen[0].steps || []; }
    else {
      steps = [];
      chosen.forEach(tpl => {
        const prefix = tpl.name.replace(/[^a-zA-Z0-9_]/g, "") + "_";
        (tpl.steps || []).forEach(s => {
          const ns = { ...s, id: prefix + (s.id || "step") };
          if (Array.isArray(s.depends_on)) ns.depends_on = s.depends_on.map(d => prefix + d);
          steps.push(ns);
        });
      });
    }
    onApply(steps);
  };

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <button className="icon-btn" onClick={onClose} title="Back to dispatcher"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>DAG templates</div>
            <div className="page-sub">Reusable blueprints — open one to view/edit its JSON, load it, or save a copy.</div>
          </div>
        </div>
        <div className="page-tools">
          {sel.length > 0 && <button className="btn ghost" onClick={mergeAndApply}><I.download size={14}/> {sel.length > 1 ? `Load & merge ${sel.length}` : "Load selected"}</button>}
          <button className="btn accent" onClick={() => setEditing({ name: "", json: currentJson, isNew: true })}><I.plus size={14}/> New blueprint</button>
        </div>
      </div>
      {templates.length === 0
        ? <div className="card pad-lg dim" style={{ textAlign: "center" }}>No templates yet — click “New blueprint” to create one from the current dispatcher blueprint.</div>
        : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
            {templates.map(t => {
              const on = sel.includes(t.name);
              return (
                <div key={t.name} className="card" style={{ padding: 16, borderColor: on ? "var(--accent)" : undefined }}>
                  <div className="row" style={{ alignItems: "center", gap: 8, marginBottom: 6 }}>
                    <Chk on={on} onChange={() => toggle(t.name)}/>
                    <code className="mono name" style={{ fontSize: 14, flex: 1, wordBreak: "break-all", cursor: "pointer" }} onClick={() => open(t)}>{t.name}</code>
                  </div>
                  <div className="hint" style={{ fontSize: 11.5, minHeight: 30 }}>{t.description || "—"}</div>
                  <div className="row" style={{ alignItems: "center", marginTop: 8, gap: 8 }}>
                    <Pill tone="ghost">{(t.steps || []).length} step{(t.steps || []).length === 1 ? "" : "s"}</Pill>
                    <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => open(t)}><I.maximize size={12}/> Open</button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
    </>
  );
};

/* #3: full-page editor for one dispatch-settings profile — mirrors TemplateEditor.
 * Settings are edited with the SAME bounded controls as the dispatcher form
 * (sliders / selects / toggles), never raw JSON, so a profile can't carry an
 * out-of-range or mis-typed value. Captured targeting (groups / pinned nodes /
 * blocked) is shown as a read-only summary and preserved untouched on save. */
const ProfileEditor = ({ editing, groups, workerPool, onApply, onSave, onDelete, onBack }) => {
  const [s, setS] = React.useState(() => ({ ...(editing.settings || {}) }));
  const [name, setName] = React.useState(editing.isNew ? "" : editing.name);
  const [desc, setDesc] = React.useState(editing.description || "");
  const [copyName, setCopyName] = React.useState("");
  // Accepts a value, an input event, or a functional updater (Targeting uses the
  // latter), so the same setters drive plain controls and the targeting pickers.
  const set = (k) => (val) => setS(prev => ({
    ...prev,
    [k]: typeof val === "function" ? val(prev[k]) : (val && val.target ? val.target.value : val),
  }));
  const v = (k, d) => (s[k] === undefined ? d : s[k]);

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <button className="icon-btn" onClick={onBack} title="Back to profiles"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>{editing.isNew ? "New profile" : editing.name}</div>
            <div className="page-sub">A reusable preset of resources, scheduling and where-it-runs — apply it to the form or save.</div>
          </div>
        </div>
      </div>
      <div className="card pad-lg">
        {editing.isNew && (
          <div style={{ marginBottom: 12 }}>
            <Field label="Profile name" hint="letters, digits, - and _">
              <input className="input mono" value={name} maxLength={80} autoFocus placeholder="gpu-ml" onChange={e => setName(e.target.value)}/>
            </Field>
          </div>
        )}
        <div style={{ marginBottom: 16 }}>
          <Field label="Description (optional)">
            <input className="input" value={desc} maxLength={200} placeholder="what this preset is for" onChange={e => setDesc(e.target.value)}/>
          </Field>
        </div>

        <div className="label" style={{ marginBottom: 8 }}>Resources</div>
        <div className="field-row tri">
          <Field label="Batch size (clones)" hint="identical copies of this task">
            <input className="input" type="number" min={1} max={100} value={v("batch", 1)} onChange={e => set("batch")(Math.max(1, Math.min(100, +e.target.value || 1)))}/>
          </Field>
          <Field label="Target RAM (MB)">
            <input className="input mono" type="number" min={128} max={65536} step={128} value={v("ram", 1024)} onChange={e => set("ram")(Math.max(128, Math.min(65536, +e.target.value || 128)))}/>
          </Field>
          <Field label="Target CPU (%)" hint={v("cpu", 100) + " % — over 100% uses multiple cores"}>
            <input type="range" min={10} max={400} step={10} value={v("cpu", 100)} onChange={e => set("cpu")(+e.target.value)} style={{ width: "100%" }}/>
          </Field>
        </div>

        <div className="label" style={{ margin: "16px 0 8px" }}>Scheduling</div>
        <div className="field-row tri">
          <Field label="Priority" hint={PRIORITY_HINTS[String(v("priority", "60"))]}>
            <select className="input" value={String(v("priority", "60"))} onChange={set("priority")}>
              <option value="40">Normal</option>
              <option value="60">Medium</option>
              <option value="80">High</option>
              <option value="95">Very high</option>
            </select>
          </Field>
          <Field label="Retry budget" hint="automatic retries after worker failure (1–6)">
            <input className="input" type="number" min={1} max={6} value={v("retryMax", 2)} onChange={e => set("retryMax")(Math.max(1, Math.min(6, +e.target.value || 1)))}/>
          </Field>
          <Field label="Queue timeout (s)" hint="0 = node default">
            <input className="input" type="number" min={0} max={86400} value={v("queueTimeout", 0)} onChange={e => set("queueTimeout")(Math.max(0, Math.min(86400, +e.target.value || 0)))}/>
          </Field>
        </div>
        <div className="field-row" style={{ marginTop: 14 }}>
          <Field label="Prefer reliable workers" hint="rank nodes by finished-to-fail ratio">
            <select className="input" value={String(v("preferReliable", ""))} onChange={set("preferReliable")}>
              <option value="">Node default</option>
              <option value="on">On — prefer reliable</option>
              <option value="off">Off</option>
            </select>
          </Field>
          <Field label="Verify each step (DAG)" hint="approve each level before the next runs">
            <select className="input" value={String(v("stepGate", ""))} onChange={set("stepGate")}>
              <option value="">Node default</option>
              <option value="on">On — approve each level</option>
              <option value="off">Off</option>
            </select>
          </Field>
          <Field label="If my node disconnects" hint="what the worker does with the result">
            <select className="input" value={String(v("orphan", "retry"))} onChange={set("orphan")}>
              <option value="retry">Hold &amp; retry upload</option>
              <option value="drop">Drop task</option>
            </select>
          </Field>
        </div>
        <div className="row" style={{ gap: 18, marginTop: 14, alignItems: "center", flexWrap: "wrap" }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Toggle on={!!v("gpu", false)} onChange={set("gpu")}/>
            <span style={{ fontSize: 13 }}>Require a GPU-capable worker</span>
          </div>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Toggle on={!!v("oneStepPerNode", false)} onChange={set("oneStepPerNode")}/>
            <span style={{ fontSize: 13 }}>One step per node <span className="hint">(DAG)</span></span>
          </div>
        </div>
        <Disclosure id="profile-advanced" label="Advanced — region, tags, overrides, capability & security">
          <div className="field-row tri" style={{ marginTop: 14 }}>
            <Field label="Preferred region (optional)" hint="only workers with this region label">
              <input className="input mono" placeholder="e.g. us-east, local" value={v("region", "")} onChange={set("region")}/>
            </Field>
            <Field label="Required worker tags (CSV)" hint="worker must have all listed tags">
              <input className="input mono" placeholder="python,highmem,avx2" value={v("tags", "")} onChange={set("tags")}/>
            </Field>
            <Field label="Cross-region workers" hint="your own routing preference">
              <select className="input" value={String(v("xRegion", ""))} onChange={set("xRegion")}>
                <option value="">Node default</option>
                <option value="allow">Allow cross-region</option>
                <option value="deny">Local region only</option>
              </select>
            </Field>
          </div>
          <div className="field-row tri" style={{ marginTop: 14 }}>
            <Field label="Lease override (s)" hint="blank/0 = node default">
              <input className="input" type="number" min={0} max={3600} placeholder="node default" value={v("leaseOverride", "")} onChange={set("leaseOverride")}/>
            </Field>
            <Field label="Retry backoff override (s)" hint="blank/0 = node default">
              <input className="input" type="number" min={0} max={600} placeholder="node default" value={v("backoffOverride", "")} onChange={set("backoffOverride")}/>
            </Field>
            <Field label="Sandbox profile request" hint="worker default or stricter">
              <select className="input" value={String(v("reqProfile", ""))} onChange={set("reqProfile")}>
                <option value="">Worker default</option>
                <option value="standard">At least standard</option>
                <option value="maximum">Maximum</option>
              </select>
            </Field>
          </div>
          <div className="row" style={{ gap: 24, marginTop: 14, flexWrap: "wrap" }}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={!!v("reqNetwork", false)} onChange={set("reqNetwork")}/><span style={{ fontSize: 13 }}>Needs network access</span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={!!v("reqVenvIso", false)} onChange={set("reqVenvIso")}/><span style={{ fontSize: 13 }}>Force venv isolation</span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={!!v("noVenvCache", false)} onChange={set("noVenvCache")}/><span style={{ fontSize: 13 }}>Skip venv cache</span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={!!v("reqScan", false)} onChange={set("reqScan")}/><span style={{ fontSize: 13 }}>Force code scan</span>
            </div>
          </div>
        </Disclosure>

        <div className="label" style={{ margin: "16px 0 8px" }}>Where it runs <span className="hint" style={{ fontWeight: 400 }}>— empty = auto-pick the best trusted worker</span></div>
        <Targeting groups={groups} workerPool={workerPool}
                   selGroups={v("selGroups", [])} setSelGroups={set("selGroups")}
                   blocked={v("blocked", [])} setBlocked={set("blocked")}
                   manual={!!v("manual", false)} setManual={set("manual")}
                   selWorkers={v("selWorkers", [])} setSelWorkers={set("selWorkers")}/>

        <hr className="divider" style={{ margin: "18px 0" }}/>
        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn accent" onClick={() => onApply(s)}><I.check size={14}/> Apply</button>
          <button className="btn ghost" disabled={editing.isNew && !name.trim()}
                  onClick={() => { onSave((editing.isNew ? name : editing.name).trim(), s, desc); onBack(); }}>
            <I.check size={14}/> {editing.isNew ? "Save profile" : "Save changes"}
          </button>
          {!editing.isNew && (
            <button className="btn ghost u-danger" style={{ marginLeft: "auto" }} onClick={() => { onDelete(editing.name); onBack(); }}><I.trash size={14}/> Delete</button>
          )}
        </div>
        {!editing.isNew && (
          <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center" }}>
            <input className="input mono" placeholder="Save as a copy named…" style={{ width: 220 }} value={copyName} onChange={e => setCopyName(e.target.value)}/>
            <button className="btn ghost" disabled={!copyName.trim()} onClick={() => { onSave(copyName.trim(), s, desc); onBack(); }}><I.copy size={14}/> Save copy</button>
          </div>
        )}
      </div>
    </>
  );
};

/* #3: full-page dispatch-profile manager (gallery + editor) — same shape as the
 * DAG TemplateManager so the two feel consistent. */
const ProfileManager = ({ profiles, currentSettings, groups, workerPool, onApply, onSave, onDelete, onClose }) => {
  const [editing, setEditing] = React.useState(null);  // {name, settings, description, isNew}
  if (editing) {
    return <ProfileEditor editing={editing} groups={groups} workerPool={workerPool}
                          onApply={onApply} onSave={onSave}
                          onDelete={onDelete} onBack={() => setEditing(null)}/>;
  }
  const open = (p) => setEditing({ name: p.name, settings: p.settings || {}, description: p.description || "", isNew: false });
  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <button className="icon-btn" onClick={onClose} title="Back to dispatcher"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>Dispatch profiles</div>
            <div className="page-sub">Reusable resources + scheduling + targeting presets — open one to view/edit, apply, or save a copy.</div>
          </div>
        </div>
        <div className="page-tools">
          <button className="btn accent" onClick={() => setEditing({ name: "", settings: currentSettings(), description: "", isNew: true })}><I.plus size={14}/> Save current as profile</button>
        </div>
      </div>
      {profiles.length === 0
        ? <div className="card pad-lg dim" style={{ textAlign: "center" }}>No profiles yet — configure the dispatcher, then click “Save current as profile”.</div>
        : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
            {profiles.map(p => (
              <div key={p.name} className="card" style={{ padding: 16 }}>
                <div className="row" style={{ alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <code className="mono name" style={{ fontSize: 14, flex: 1, wordBreak: "break-all", cursor: "pointer" }} onClick={() => open(p)}>{p.name}</code>
                </div>
                <div className="hint" style={{ fontSize: 11.5, minHeight: 30 }}>{p.description || "—"}</div>
                <div className="row" style={{ alignItems: "center", marginTop: 8, gap: 8 }}>
                  <Pill tone="ghost">{Object.keys(p.settings || {}).length} fields</Pill>
                  <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => onApply(p.settings)}><I.check size={12}/> Apply</button>
                  <button className="btn ghost sm" onClick={() => open(p)}><I.maximize size={12}/> Open</button>
                </div>
              </div>
            ))}
          </div>
        )}
    </>
  );
};

const DispatcherScreen = ({ setRoute, prefill, clearPrefill }) => {
  const [mode, setMode] = React.useState("simple");
  // What to run
  const [workflowId, setWorkflowId] = React.useState("");
  const [runtime, setRuntime] = React.useState("docker");
  const [image, setImage] = React.useState("python:3.11-slim");
  const [entrypoint, setEntrypoint] = React.useState("python main.py");
  const [setupCmd, setSetupCmd] = React.useState("");
  const [autoDeps, setAutoDeps] = React.useState(false);
  const [dagJson, setDagJson] = React.useState(DEFAULT_DAG);
  const [dagView, setDagView] = React.useState("builder");  // builder | graph | json
  const [oneStepPerNode, setOneStepPerNode] = React.useState(false);  // DAG anti-affinity
  const [savedTemplates, setSavedTemplates] = React.useState([]);     // node-saved DAG templates
  const [templateOpen, setTemplateOpen] = React.useState(false);      // full-page template manager
  const [profiles, setProfiles] = React.useState([]);                 // node-saved dispatch-settings profiles
  const [profileOpen, setProfileOpen] = React.useState(false);        // full-page profile manager
  // Resources
  const [batch, setBatch] = React.useState(1);
  const [ram, setRam] = React.useState(1024);
  const [cpu, setCpu] = React.useState(100);
  // Scheduling
  const [priority, setPriority] = React.useState("60");
  const [retryMax, setRetryMax] = React.useState(2);
  const [preferReliable, setPreferReliable] = React.useState("");  // "" inherit | "on" | "off"
  const [stepGate, setStepGate] = React.useState("");  // DAG only: "" inherit | "on" | "off"
  const [nodeStepGate, setNodeStepGate] = React.useState(false);  // this node's step_gate default (for the "Node default" label/hint)
  const [queueTimeout, setQueueTimeout] = React.useState(0);
  const [leaseOverride, setLeaseOverride] = React.useState("");
  const [backoffOverride, setBackoffOverride] = React.useState("");
  const [reqNetwork, setReqNetwork] = React.useState(false);
  const [reqVenvIso, setReqVenvIso] = React.useState(false);
  const [noVenvCache, setNoVenvCache] = React.useState(false);
  const [reqScan, setReqScan] = React.useState(false);
  const [reqProfile, setReqProfile] = React.useState("");   // "" = worker default
  const [xRegion, setXRegion] = React.useState("");         // "" = node default
  const [region, setRegion] = React.useState("");
  const [tags, setTags] = React.useState("");
  const [gpu, setGpu] = React.useState(false);
  const [orphan, setOrphan] = React.useState("retry");
  // Targets — group/member/worker pickers live in the shared <Targeting> component.
  const [groups, setGroups] = React.useState([]);              // [{group_id,name}]
  const [selGroups, setSelGroups] = React.useState([]);        // [group_id]
  const [blocked, setBlocked] = React.useState([]);            // [node_id]
  const [manual, setManual] = React.useState(false);
  const [workerPool, setWorkerPool] = React.useState([]);      // [{ip,display_ip,name,role}]
  const [selWorkers, setSelWorkers] = React.useState([]);      // [ip]
  // Workspace
  const [files, setFiles] = React.useState([]);
  // Cloud workspace / data sources (Wave 9.5 parity — gdrive credentials)
  const [creds, setCreds] = React.useState([]);
  const [cloudWs, setCloudWs] = React.useState(false);
  const [cloudCred, setCloudCred] = React.useState("");
  const [cloudFolder, setCloudFolder] = React.useState("");
  const [dataSources, setDataSources] = React.useState([]); // [{credential_id,type,folder_id,mount_path}]
  const [dsDraft, setDsDraft] = React.useState({ credential_id: "", folder_id: "", mount_path: "" });
  // Flow state
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState(null);                  // {tone, text}
  const [verify, setVerify] = React.useState(null);            // {language,outputFile,packagesText,scannedFiles,ctx}
  const [terms, setTerms] = React.useState(null);              // {version,terms,ctx}
  const fileRef = React.useRef(null);

  // Clone-from-telemetry: apply the source task's manifest once, then clear.
  React.useEffect(() => {
    if (!prefill) return;
    const m = prefill.manifest || {};
    setMode("simple");
    setWorkflowId(prefill.suggestedId || "");
    if (m.runtime) setRuntime(m.runtime);
    if (m.image) setImage(m.image);
    if (m.entrypoint != null) setEntrypoint(m.entrypoint);
    if (m.setup_cmd != null) setSetupCmd(m.setup_cmd);
    if (m.ram_limit_mb) setRam(m.ram_limit_mb);
    if (m.cpu_limit_pct) setCpu(m.cpu_limit_pct);
    setMsg({ tone: "info", text: "Dispatcher pre-filled from the cloned task — pick the workspace folder and queue." });
    clearPrefill && clearPrefill();
  }, [prefill]);

  React.useEffect(() => {
    api.get("/local/groups").then(r => setGroups((r && r.groups) || [])).catch(() => {});
    api.get("/local/dag_templates").then(r => setSavedTemplates((r && r.templates) || [])).catch(() => {});
    api.get("/local/dispatch_templates").then(r => setProfiles((r && r.templates) || [])).catch(() => {});
    api.get("/local/foreign_storage/cloud_credentials").then(r => {
      const g = ((r && r.credentials) || []).filter(c => c.provider === "gdrive");
      setCreds(g);
      if (g.length) { setCloudCred(prev => prev || String(g[0].id)); setDsDraft(d => d.credential_id ? d : { ...d, credential_id: String(g[0].id) }); }
    }).catch(() => {});
    api.get("/local/network").then(r => setNodeStepGate(!!(r && r.settings && r.settings.step_gate))).catch(() => {});
    api.get("/local/peers").then(r => {
      const trusted = ((r && r.peers) || []).filter(p => p.status === "trusted" && (p.role === "worker" || p.role === "dual"));
      setWorkerPool(trusted.map(p => ({
        ip: p.internal_ip || p.ip, display_ip: p.ip,
        name: p.display_name || "", role: p.role === "dual" ? "dual" : "worker",
      })));
    }).catch(() => {});
  }, []);

  const cleanId = workflowId.replace(/[^a-zA-Z0-9_-]/g, "");
  const totalSize = files.reduce((s, f) => s + (f.size || 0), 0);

  const note = (tone, text) => { setMsg({ tone, text }); if (tone !== "danger") setTimeout(() => setMsg(null), 6000); };

  /* Build the workspace zip from the picked folder (top folder name stripped,
   * same as classic). */
  const buildZip = () => {
    const zip = new JSZip();
    for (const f of files) {
      const parts = (f.webkitRelativePath || f.name).split("/");
      if (parts.length > 1) parts.shift();
      const rel = parts.join("/");
      if (rel) zip.file(rel, f);
    }
    return zip;
  };

  /* Capability requests apply to every step (simple task or DAG node)
   * unless a DAG node already sets the key itself. */
  const applyCaps = (t) => {
    if (reqNetwork && t.network_required === undefined) t.network_required = true;
    if (reqVenvIso && t.require_venv_isolation === undefined) t.require_venv_isolation = true;
    if (noVenvCache && t.no_venv_cache === undefined) t.no_venv_cache = true;
    if (reqScan && t.enable_task_scanning === undefined) t.enable_task_scanning = true;
    if (reqProfile && t.security_profile === undefined) t.security_profile = reqProfile;
    if (xRegion && t.allow_cross_region === undefined) t.allow_cross_region = xRegion === "allow";
    return t;
  };

  const buildWorkflow = () => {
    if (mode === "dag") {
      try {
        const parsed = JSON.parse(dagJson);
        return Array.isArray(parsed) ? parsed.map(applyCaps) : parsed;
      }
      catch (_) { note("danger", "DAG blueprint is not valid JSON."); return null; }
    }
    const task = {
      id: "task", runtime, image: image || "python:3.11-slim",
      entrypoint, setup_cmd: setupCmd,
      ram_limit: Number(ram) || 1024, cpu_limit: Number(cpu) || 100,
      slice_count: Math.max(1, Math.min(100, Number(batch) || 1)),
      depends_on: [],
    };
    if (cloudWs && cloudCred && cloudFolder.trim()) {
      task.workspace_source = { type: "gdrive", credential_id: cloudCred, folder_id: cloudFolder.trim() };
    } else if (dataSources.length) {
      task.data_sources = dataSources.slice();
    }
    return [applyCaps(task)];
  };

  /* DAG #4: apply a (possibly merged) step list from the template manager into
   * the dispatcher blueprint, then return to the dispatcher in DAG mode. */
  const applyBlueprint = (steps) => {
    setDagJson(JSON.stringify(steps, null, 2));
    setTemplateOpen(false);
    setMode("dag"); setDagView("builder");
    note("ok", `Loaded ${steps.length} step${steps.length === 1 ? "" : "s"} into the blueprint.`);
  };
  const saveTemplate = async (name, json) => {
    name = (name || "").trim();
    if (!name) return;
    try {
      await api.post("/local/dag_templates", { name, workflow_json: json || dagJson });
      const r = await api.get("/local/dag_templates");
      setSavedTemplates((r && r.templates) || []);
      note("ok", `Template “${name}” saved.`);
    } catch (e) { note("danger", "Save failed: " + (e.detail || e.message || "")); }
  };
  const deleteTemplate = async (name) => {
    try {
      await api.del(`/local/dag_templates/${encodeURIComponent(name)}`);
      const r = await api.get("/local/dag_templates");
      setSavedTemplates((r && r.templates) || []);
    } catch (e) { note("danger", "Delete failed: " + (e.detail || e.message || "")); }
  };

  /* #3: dispatch-settings profiles — snapshot the resources + scheduling +
   * targeting fields, save them node-side, and re-apply later. Workload, files
   * and the DAG blueprint are deliberately NOT captured (those are per-run). */
  const currentSettings = () => ({
    batch, ram, cpu,
    priority, retryMax, preferReliable, stepGate, queueTimeout, leaseOverride, backoffOverride,
    reqNetwork, reqVenvIso, noVenvCache, reqScan, reqProfile, xRegion,
    region, tags, gpu, orphan, oneStepPerNode,
    selGroups, blocked, manual, selWorkers,
  });
  const applySettings = (s) => {
    if (!s || typeof s !== "object") return;
    const setters = {
      batch: setBatch, ram: setRam, cpu: setCpu,
      priority: setPriority, retryMax: setRetryMax, preferReliable: setPreferReliable, stepGate: setStepGate,
      queueTimeout: setQueueTimeout, leaseOverride: setLeaseOverride, backoffOverride: setBackoffOverride,
      reqNetwork: setReqNetwork, reqVenvIso: setReqVenvIso, noVenvCache: setNoVenvCache,
      reqScan: setReqScan, reqProfile: setReqProfile, xRegion: setXRegion,
      region: setRegion, tags: setTags, gpu: setGpu, orphan: setOrphan, oneStepPerNode: setOneStepPerNode,
      selGroups: setSelGroups, blocked: setBlocked, manual: setManual, selWorkers: setSelWorkers,
    };
    for (const k in setters) if (k in s) setters[k](s[k]);
  };
  const reloadProfiles = async () => {
    try { const r = await api.get("/local/dispatch_templates"); setProfiles((r && r.templates) || []); } catch (_) {}
  };
  const saveProfile = async (name, settings, description) => {
    name = (name || "").trim();
    if (!name) { note("danger", "Name the profile first."); return; }
    try {
      await api.post("/local/dispatch_templates", { name, settings: settings || {}, description: description || "" });
      await reloadProfiles();
      note("ok", `Profile “${name}” saved.`);
    } catch (e) { note("danger", "Save failed: " + (e.detail || e.message || "")); }
  };
  const applyProfile = (settings) => {
    applySettings(settings || {});
    setProfileOpen(false);
    note("ok", "Profile applied to the form.");
  };
  const deleteProfile = async (name) => {
    try {
      await api.del(`/local/dispatch_templates/${encodeURIComponent(name)}`);
      await reloadProfiles();
    } catch (e) { note("danger", "Delete failed: " + (e.detail || e.message || "")); }
  };

  const cloudWsActive = mode === "simple" && cloudWs && cloudCred && cloudFolder.trim();

  /* Final POST. On 412 the server wants the cloud task-data terms accepted
   * first — surface them as an in-page consent panel and retry on accept. */
  const submit = async (zipBlob, workflow) => {
    const fd = new FormData();
    fd.append("file", zipBlob, "workspace.zip");
    fd.append("workflow_id", cleanId);
    fd.append("workflow_json", JSON.stringify(workflow));
    fd.append("preferred_workers", JSON.stringify(manual ? selWorkers : []));
    fd.append("target_groups", JSON.stringify(selGroups));
    fd.append("blocked_members", JSON.stringify(blocked));
    fd.append("one_step_per_node", oneStepPerNode ? "true" : "false");
    fd.append("priority", priority);
    fd.append("retry_max", String(Math.max(1, Math.min(6, Number(retryMax) || 2))));
    fd.append("prefer_reliable_workers", preferReliable);
    fd.append("step_gate", mode === "dag" ? stepGate : "");
    fd.append("required_tags", tags);
    fd.append("require_gpu", gpu ? "true" : "false");
    fd.append("preferred_region", region);
    fd.append("orphan_policy", orphan);
    fd.append("queue_timeout_sec", String(Number(queueTimeout) || 0));
    fd.append("lease_seconds", String(Number(leaseOverride) || 0));
    fd.append("retry_backoff_base_sec", String(Number(backoffOverride) || 0));
    try {
      const res = await api.post("/local/add_workflow", fd);
      note("ok", (res && res.message) || "Workflow queued.");
      toast((res && res.message) || "Workflow queued.", "info", { label: "View in telemetry", hash: "#/telemetry" });
      setFiles([]); if (fileRef.current) fileRef.current.value = "";
      setWorkflowId("");
    } catch (e) {
      if (e.status === 412 && e.detail && typeof e.detail === "object") {
        setTerms({ version: e.detail.version, terms: e.detail.terms || "", ctx: { zipBlob, workflow } });
      } else {
        note("danger", "Dispatch failed: " + (typeof e.detail === "string" ? e.detail : (e.detail && e.detail.message) || e.message));
      }
    }
  };

  /* After dependency verification: write the deps file into the zip, default
   * the install command if blank, then submit. */
  const confirmVerify = async () => {
    const { language, outputFile, ctx } = verify;
    const text = verify.packagesText;
    const zip = ctx.zip;
    let setup = setupCmd;
    if (text.trim()) {
      if (language === "javascript") {
        const deps = {};
        text.split("\n").map(l => l.trim()).filter(Boolean).forEach(raw => {
          let name = raw, version = "*";
          const sep = raw.indexOf("@", raw.startsWith("@") ? 1 : 0);
          if (sep > 0) { name = raw.slice(0, sep); version = raw.slice(sep + 1) || "*"; }
          if (name) deps[name] = version;
        });
        zip.file("package.json", JSON.stringify({ name: "workspace", version: "1.0.0", dependencies: deps }, null, 2));
        if (!setup.trim()) setup = "npm install";
      } else if (language === "cpp") {
        zip.file(outputFile, text);
      } else {
        zip.file("requirements.txt", text);
        if (!setup.trim()) setup = "pip install -r requirements.txt";
      }
    }
    if (setup !== setupCmd) setSetupCmd(setup);
    const workflow = ctx.workflow.map(t => (!t.setup_cmd || !String(t.setup_cmd).trim()) && setup ? { ...t, setup_cmd: setup } : t);
    setVerify(null); setBusy(true);
    try { await submit(await zip.generateAsync({ type: "blob" }), workflow); } finally { setBusy(false); }
  };

  const dispatch = async () => {
    setMsg(null);
    if (!cleanId) { note("danger", "Give the deployment an ID (letters, numbers, - and _)."); return; }
    if (!files.length && !cloudWsActive) { note("danger", "Pick a workspace folder — or switch the workspace to a cloud folder below."); return; }
    const workflow = buildWorkflow();
    if (!workflow) return;
    setBusy(true);
    try {
      const zip = buildZip();
      let zipBlob = await zip.generateAsync({ type: "blob" });

      // Optional: scan the workspace for third-party imports and offer a
      // generated dependency file (verified in-page before anything is sent).
      if (mode === "simple" && autoDeps && !zip.file("requirements.txt") && !zip.file("package.json")) {
        const fd = new FormData();
        fd.append("file", zipBlob, "workspace.zip");
        fd.append("entrypoint", entrypoint);
        try {
          const scan = await api.post("/local/scan_imports", fd);
          if (scan && scan.packages && scan.packages.length) {
            setVerify({
              language: scan.language || "python",
              outputFile: scan.output_file || "requirements.txt",
              packagesText: scan.packages.join("\n"),
              scannedFiles: scan.scanned_files || [],
              ctx: { zip, workflow },
            });
            setBusy(false);
            return; // continues via the verify panel
          }
          note("info", "No third-party dependencies detected — dispatching without a dependency file.");
        } catch (e) {
          note("warn", "Dependency scan failed (" + (e.detail || e.message) + ") — dispatching without it.");
        }
      }

      // Safety net (same as classic): a deps file without an install command
      // does nothing, so default the setup command and sync it onto tasks.
      let setup = setupCmd;
      if (!setup.trim()) {
        if (zip.file("requirements.txt")) setup = "pip install -r requirements.txt";
        else if (zip.file("package.json")) setup = "npm install";
        if (setup) setSetupCmd(setup);
      }
      const finalWorkflow = setup
        ? workflow.map(t => (!t.setup_cmd || !String(t.setup_cmd).trim()) ? { ...t, setup_cmd: setup } : t)
        : workflow;
      if (setup && mode === "simple") zipBlob = await zip.generateAsync({ type: "blob" });

      await submit(zipBlob, finalWorkflow);
    } catch (e) {
      note("danger", "Dispatch failed: " + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const acceptTerms = async () => {
    const ctx = terms.ctx;
    setTerms(null); setBusy(true);
    try {
      await api.post("/local/task_data_terms/accept");
      await submit(ctx.zipBlob, ctx.workflow);
    } catch (e) {
      note("danger", "Could not record terms acceptance: " + (e.detail || e.message));
    } finally { setBusy(false); }
  };

  if (templateOpen) {
    return <TemplateManager templates={savedTemplates} currentJson={dagJson}
                            onApply={applyBlueprint} onSave={saveTemplate} onDelete={deleteTemplate}
                            onClose={() => setTemplateOpen(false)}/>;
  }

  if (profileOpen) {
    return <ProfileManager profiles={profiles} currentSettings={currentSettings}
                           groups={groups} workerPool={workerPool}
                           onApply={applyProfile} onSave={saveProfile} onDelete={deleteProfile}
                           onClose={() => setProfileOpen(false)}/>;
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Dispatch a workload</div>
          <div className="page-sub">Send code to trusted workers — a simple one-step task, or a DAG pipeline with dependencies.</div>
        </div>
        <div className="page-tools">
          <div className="seg">
            <button className={mode === "simple" ? "on" : ""} onClick={() => setMode("simple")}>Simple task</button>
            <button className={mode === "dag" ? "on" : ""} onClick={() => setMode("dag")}>DAG workflow</button>
          </div>
        </div>
      </div>

      {msg && (
        <div className={"banner " + (msg.tone === "danger" ? "danger" : "info")} style={{ marginBottom: 14 }}>
          <I.info size={14}/><span>{msg.text}</span>
        </div>
      )}

      {/* In-page consent: verify auto-generated dependencies before dispatch */}
      {verify && (
        <div className="card pad-lg" style={{ marginBottom: 14, borderColor: "var(--accent)" }}>
          <CardHead icon={<I.shield size={14}/>} tone="amber" title="Verify detected dependencies"
                    meta={<span>{verify.language}</span>}/>
          <div className="hint" style={{ marginBottom: 8 }}>
            These third-party packages were detected in your workspace. Edit the list if needed —
            confirming writes <span className="mono">{verify.outputFile}</span> into the workspace before dispatch.
            {verify.scannedFiles.length > 0 && <> Scanned: <span className="mono">{verify.scannedFiles.join(", ")}</span></>}
          </div>
          <textarea className="input mono" rows={6} style={{ width: "100%", resize: "vertical" }}
                    value={verify.packagesText} onChange={e => setVerify({ ...verify, packagesText: e.target.value })}/>
          <div className="row" style={{ gap: 10, marginTop: 12 }}>
            <button className="btn accent" disabled={busy} onClick={confirmVerify}><I.check size={14}/> Confirm &amp; dispatch</button>
            <button className="btn ghost" onClick={() => { setVerify(null); note("info", "Dispatch cancelled."); }}>Cancel</button>
          </div>
        </div>
      )}

      {/* In-page consent: cloud task-data terms (server replied 412) */}
      {terms && (
        <div className="card pad-lg" style={{ marginBottom: 14, borderColor: "var(--accent)" }}>
          <CardHead icon={<I.shield size={14}/>} tone="amber" title="Cloud task-data terms"
                    meta={<span>v{terms.version || "?"}</span>}/>
          <div className="hint" style={{ whiteSpace: "pre-wrap", marginBottom: 12 }}>{terms.terms}</div>
          <div className="row" style={{ gap: 10 }}>
            <button className="btn accent" disabled={busy} onClick={acceptTerms}><I.check size={14}/> Accept &amp; dispatch</button>
            <button className="btn ghost" onClick={() => { setTerms(null); note("info", "Dispatch cancelled — terms not accepted."); }}>Cancel</button>
          </div>
        </div>
      )}

      <div className="col" style={{ gap: 14, marginBottom: 24, opacity: verify || terms ? 0.45 : 1, pointerEvents: verify || terms ? "none" : "auto" }}>
        <Sec icon={<I.terminal size={14}/>} tone="cyan" title="What to run"
             sub={mode === "dag" ? "DAG blueprint JSON" : "execution environment"}>
          <div className="field-row" style={{ marginBottom: 14 }}>
            <Field label="Deployment ID" hint="names this workflow in telemetry">
              <input className="input mono" placeholder="e.g. ai_pipeline_v1" value={workflowId}
                     onChange={e => setWorkflowId(e.target.value)}/>
            </Field>
            {mode === "simple" && (
              <Field label="Execution architecture">
                <select className="input" value={runtime} onChange={e => setRuntime(e.target.value)}>
                  <option value="docker">Docker sandbox (standard)</option>
                  <option value="wasm">WebAssembly sandbox (wasmtime)</option>
                  <option value="native">Native host (GPU / Blender / Unreal)</option>
                </select>
              </Field>
            )}
          </div>

          {mode === "simple" ? (
            <>
              {runtime === "docker" && (
                <Field label="Docker image">
                  <input className="input mono" value={image} onChange={e => setImage(e.target.value)}/>
                </Field>
              )}
              <div className="field-row" style={{ marginTop: 14 }}>
                <Field label="Run command" hint="what the worker executes">
                  <textarea className="input mono" rows={2} style={{ resize: "vertical" }}
                            value={entrypoint} onChange={e => setEntrypoint(e.target.value)}/>
                </Field>
                <Field label="Setup command (optional)" hint="runs once before the task, e.g. pip install -r requirements.txt">
                  <textarea className="input mono" rows={2} style={{ resize: "vertical" }} placeholder="pip install numpy"
                            value={setupCmd} onChange={e => setSetupCmd(e.target.value)}/>
                </Field>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 14, alignItems: "center" }}>
                <Chk on={autoDeps} onChange={setAutoDeps}/>
                <span style={{ fontSize: 13 }}>Auto-detect dependencies</span>
                <span className="hint">scans your files for third-party imports and asks you to verify a generated dependency file</span>
              </div>
            </>
          ) : (
            <>
              <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
                <div className="seg">
                  <button className={dagView === "builder" ? "on" : ""} onClick={() => setDagView("builder")}>Builder</button>
                  <button className={dagView === "graph" ? "on" : ""} onClick={() => setDagView("graph")}>Graph</button>
                  <button className={dagView === "json" ? "on" : ""} onClick={() => setDagView("json")}>JSON</button>
                </div>
                <select className="input" style={{ width: 250 }} value=""
                        onChange={e => { if (e.target.value) { setDagJson(DAG_TEMPLATES[e.target.value]); } }}>
                  <option value="">Load a template…</option>
                  {Object.keys(DAG_TEMPLATES).map(k => <option key={k} value={k}>{k}</option>)}
                </select>
                {dagView === "json" && (
                  <button className="btn ghost sm" onClick={() => {
                    const p = parseDag(dagJson);
                    if (p) setDagJson(JSON.stringify(p, null, 2)); else note("danger", "Can't format — invalid JSON.");
                  }}>Format JSON</button>
                )}
                <span style={{ marginLeft: "auto" }}>{(() => {
                  const nodes = parseDag(dagJson);
                  if (!nodes) return <Pill tone="rose">invalid JSON</Pill>;
                  const issues = dagIssues(nodes);
                  return issues.length
                    ? <Pill tone="amber">{issues.length} issue{issues.length > 1 ? "s" : ""}</Pill>
                    : <Pill tone="emerald">✓ {nodes.length} step{nodes.length > 1 ? "s" : ""}</Pill>;
                })()}</span>
              </div>

              {/* DAG #4: open the template gallery (browse / load / merge / save). */}
              <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 10 }}>
                <button className="btn ghost sm" onClick={() => setTemplateOpen(true)}>
                  <I.layers size={13}/> Templates{savedTemplates.length ? ` (${savedTemplates.length})` : ""}
                </button>
                <span className="hint">view / edit / load saved DAG blueprints (opens full-screen)</span>
              </div>

              {dagView === "builder"
                ? <DagBuilder value={dagJson} onChange={setDagJson}/>
                : dagView === "graph"
                ? <DagCanvas value={dagJson} onChange={setDagJson} note={note}/>
                : <CodeField label="DAG blueprint (JSON)" language="json" rows={20}
                             hint="depends_on chains steps; slice_count fans a step out in parallel — Expand for full-screen"
                             value={dagJson} onChange={setDagJson}/>}

              {(() => {
                const nodes = parseDag(dagJson);
                const issues = nodes ? dagIssues(nodes) : ["Blueprint is not valid JSON."];
                if (issues.length) return (
                  <div className="col" style={{ gap: 3, marginTop: 10 }}>
                    {issues.slice(0, 8).map((m, i) => (
                      <div key={i} className="hint" style={{ color: "var(--amber, #fbbf24)" }}>• {m}</div>
                    ))}
                  </div>
                );
                // The Graph view IS the graph — skip the redundant read-only preview.
                if (dagView === "graph") return null;
                return (
                  <div style={{ marginTop: 10 }}>
                    <div className="label" style={{ marginBottom: 6 }}>Workflow graph preview</div>
                    <DagGraph nodes={nodes} height={Math.max(120, nodes.length * 28)}/>
                  </div>
                );
              })()}
              <div className="row" style={{ gap: 8, marginTop: 14, alignItems: "center" }}>
                <Toggle on={oneStepPerNode} onChange={setOneStepPerNode}/>
                <span style={{ fontSize: 13 }}>One step per node</span>
                <span className="hint">a node already running a step of this workflow won't be given another — spreads steps across nodes</span>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ fontSize: 13 }}>Verify each step</span>
                <select className="input" style={{ width: 160 }} value={stepGate} onChange={e => setStepGate(e.target.value)}>
                  <option value="">Node default ({nodeStepGate ? "On" : "Off"})</option>
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
                <span className="hint" style={{ flex: 1, minWidth: 240 }}>{(() => {
                  const eff = stepGate === "" ? (nodeStepGate ? "on" : "off") : stepGate;
                  const prefix = stepGate === "" ? `Using this node's default (${nodeStepGate ? "On" : "Off"}) — ` : "";
                  return prefix + (eff === "on"
                    ? "runs one level at a time: when a level finishes, the next waits for your approval before its nodes are assigned, so you can stop early if something looks wrong (parallel steps in a level run together)."
                    : "runs straight through: each level is dispatched as soon as its dependencies finish — no approval pauses.");
                })()}</span>
              </div>
            </>
          )}
        </Sec>

        <Sec icon={<I.upload size={14}/>} tone="emerald" title="Workspace files" sub="sent to the worker as the task's working directory">
          {mode === "simple" && (
            <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 12 }}>
              <Toggle on={cloudWs} onChange={setCloudWs}/>
              <span style={{ fontSize: 13 }}>Use a Google Drive folder as the workspace</span>
              <span className="hint">the worker pulls it at run time — no local upload needed</span>
            </div>
          )}
          {mode === "simple" && cloudWs && (
            <div className="field-row" style={{ marginBottom: 12 }}>
              <Field label="Credential" hint={creds.length ? "" : "no gdrive credentials — add one in Foreign Storage → Cloud credentials"}>
                <select className="input" value={cloudCred} onChange={e => setCloudCred(e.target.value)}>
                  {creds.length === 0 && <option value="">(none)</option>}
                  {creds.map(c => <option key={c.id} value={c.id}>{c.provider} — {c.label || "(no label)"}</option>)}
                </select>
              </Field>
              <Field label="Drive folder ID" hint="from the folder's URL">
                <input className="input mono" value={cloudFolder} onChange={e => setCloudFolder(e.target.value)}/>
              </Field>
            </div>
          )}
          {!cloudWs && (
            <>
              <input ref={fileRef} type="file" webkitdirectory="" directory="" multiple
                     style={{ display: "none" }} id="v3-folder-upload"
                     onChange={e => setFiles(Array.from(e.target.files || []))}/>
              <div className="row" style={{ gap: 12, alignItems: "center" }}>
                <button className="btn ghost" onClick={() => fileRef.current && fileRef.current.click()}>
                  <I.upload size={14}/> Choose folder…
                </button>
                {files.length
                  ? <span className="hint"><span className="mono">{files.length}</span> files · {fmtBytes(totalSize)} — zipped in your browser before upload</span>
                  : <span className="hint">No folder selected yet. The folder's contents become the worker's workspace.</span>}
              </div>
              {mode === "simple" && (
                <div style={{ marginTop: 14 }}>
                  <div className="label" style={{ marginBottom: 6 }}>Extra cloud data sources (optional) <span className="hint" style={{ fontWeight: 400 }}>— Drive folders mounted into the workspace at run time</span></div>
                  {dataSources.map((s, i) => (
                    <div key={i} className="row" style={{ gap: 8, alignItems: "center", marginBottom: 6 }}>
                      <span className="mono" style={{ fontSize: 11 }}>{s.type} folder {s.folder_id} → {s.mount_path || "/"}</span>
                      <button className="btn ghost sm" onClick={() => setDataSources(dataSources.filter((_, j) => j !== i))}><I.x size={12}/></button>
                    </div>
                  ))}
                  <div className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
                    <Field label="Credential">
                      <select className="input" style={{ width: 200 }} value={dsDraft.credential_id} onChange={e => setDsDraft({ ...dsDraft, credential_id: e.target.value })}>
                        {creds.length === 0 && <option value="">(no gdrive credentials)</option>}
                        {creds.map(c => <option key={c.id} value={c.id}>{c.provider} — {c.label || "(no label)"}</option>)}
                      </select>
                    </Field>
                    <Field label="Folder ID"><input className="input mono" style={{ width: 200 }} value={dsDraft.folder_id} onChange={e => setDsDraft({ ...dsDraft, folder_id: e.target.value })}/></Field>
                    <Field label="Mount path"><input className="input mono" style={{ width: 140 }} placeholder="e.g. data/" value={dsDraft.mount_path} onChange={e => setDsDraft({ ...dsDraft, mount_path: e.target.value })}/></Field>
                    <button className="btn ghost" disabled={!dsDraft.credential_id || !dsDraft.folder_id.trim()}
                            onClick={() => { setDataSources([...dataSources, { type: "gdrive", credential_id: dsDraft.credential_id, folder_id: dsDraft.folder_id.trim(), mount_path: dsDraft.mount_path.trim() }]); setDsDraft({ ...dsDraft, folder_id: "", mount_path: "" }); }}>
                      <I.plus size={13}/> Add source
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </Sec>

        <Sec icon={<I.layers size={14}/>} tone="cyan" title="Dispatch profile"
             sub="reusable resources + scheduling + targeting presets">
          <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <button type="button" className="btn ghost" onClick={() => setProfileOpen(true)}>
              <I.layers size={13}/> Profiles{profiles.length ? ` (${profiles.length})` : ""}
            </button>
            <span className="hint">view / apply / save dispatch presets — resources, scheduling and where-it-runs (opens full-screen)</span>
          </div>
        </Sec>

        {mode === "simple" && (
          <Sec icon={<I.cpu size={14}/>} tone="amber" title="Resources" sub="per task clone">
            <div className="field-row tri">
              <Field label="Batch size (clones)" hint="identical copies of this task">
                <input className="input" type="number" min={1} max={100} value={batch} onChange={e => setBatch(e.target.value)}/>
              </Field>
              <Field label="Target RAM (MB)">
                <input className="input mono" type="number" min={128} max={65536} step={128} value={ram} onChange={e => setRam(e.target.value)}/>
              </Field>
              <Field label="Target CPU (%)" hint={cpu + " % — over 100% uses multiple cores"}>
                <input type="range" min={10} max={400} step={10} value={cpu} onChange={e => setCpu(+e.target.value)} style={{ width: "100%" }}/>
              </Field>
            </div>
          </Sec>
        )}

        <Sec icon={<I.target size={14}/>} tone="purple" title="Scheduling" sub="how the grid treats this work">
          <div className="field-row tri">
            <Field label="Priority" hint={PRIORITY_HINTS[priority]}>
              <select className="input" value={priority} onChange={e => setPriority(e.target.value)}>
                <option value="40">Normal</option>
                <option value="60">Medium</option>
                <option value="80">High</option>
                <option value="95">Very high</option>
              </select>
            </Field>
            <Field label="Retry budget" hint="automatic retries after worker failure (1–6)">
              <input className="input" type="number" min={1} max={6} value={retryMax} onChange={e => setRetryMax(e.target.value)}/>
            </Field>
            <Field label="Queue timeout (s)" hint="0 = node default; fails if no worker picks it up in time"
                   help="How long this task may sit in the queue waiting for a worker before it gives up. Leave 0 to use your node's default setting.">
              <input className="input" type="number" min={0} max={86400} value={queueTimeout} onChange={e => setQueueTimeout(e.target.value)}/>
            </Field>
          </div>
          <div className="row" style={{ gap: 8, marginTop: 14, alignItems: "center" }}>
            <Toggle on={gpu} onChange={setGpu}/>
            <span style={{ fontSize: 13 }}>Require a GPU-capable worker</span>
          </div>
          <div className="field-row" style={{ marginTop: 14 }}>
            <Field label="Prefer reliable workers" hint="rank nodes by finished-to-fail ratio"
                   help="When on, the scheduler favours workers with a better track record of finishing tasks for this node, above raw fitness. 'Node default' uses your global setting; this choice overrides it for this dispatch only (task / service / DAG).">
              <select className="input" value={preferReliable} onChange={e => setPreferReliable(e.target.value)}>
                <option value="">Node default</option>
                <option value="on">On — prefer reliable</option>
                <option value="off">Off</option>
              </select>
            </Field>
          </div>
          <Disclosure id="dispatch-advanced" label="Advanced — region, overrides, capability & security">
          <div className="field-row tri" style={{ marginTop: 14 }}>
            <Field label="Preferred region (optional)" hint="only workers with this region label">
              <input className="input mono" placeholder="e.g. us-east, local" value={region} onChange={e => setRegion(e.target.value)}/>
            </Field>
            <Field label="Required worker tags (CSV)" hint="worker must have all listed tags"
                   help="Tags are capability labels workers declare in their config (e.g. python, highmem). The task only goes to workers carrying every tag you list.">
              <input className="input mono" placeholder="python,highmem,avx2" value={tags} onChange={e => setTags(e.target.value)}/>
            </Field>
            <Field label="If my node disconnects" hint="what the worker does with the finished result"
                   help="A finished result normally uploads back to you. If you're offline at that moment, the worker can hold it and keep retrying, or discard it.">
              <select className="input" value={orphan} onChange={e => setOrphan(e.target.value)}>
                <option value="retry">Hold &amp; retry upload</option>
                <option value="drop">Drop task</option>
              </select>
            </Field>
          </div>
          <div className="field-row" style={{ marginTop: 14 }}>
            <Field label="Lease override (s)" hint="blank/0 = node default"
                   help="A lease is the heartbeat window: if the worker goes silent for this many seconds the task is declared lost and re-queued. Long-running steps may want a bigger lease; this applies to this dispatch only.">
              <input className="input" type="number" min={0} max={3600} placeholder="node default"
                     value={leaseOverride} onChange={e => setLeaseOverride(e.target.value)}/>
            </Field>
            <Field label="Retry backoff override (s)" hint="blank/0 = node default"
                   help="Wait before the first automatic retry after a failure; each further retry waits exponentially longer. Applies to this dispatch only.">
              <input className="input" type="number" min={0} max={600} placeholder="node default"
                     value={backoffOverride} onChange={e => setBackoffOverride(e.target.value)}/>
            </Field>
          </div>
          <hr className="divider" style={{ margin: "16px 0" }}/>
          <div className="label" style={{ marginBottom: 8 }}>Capability &amp; security for this dispatch <span className="hint" style={{ fontWeight: 400 }}>— requests can only tighten a worker's posture, never relax it; DAG nodes that set their own keys win</span></div>
          <div className="field-row" style={{ marginBottom: 12 }}>
            <Field label="Sandbox profile request" hint="worker default or stricter"
                   help="Ask workers to run this task under a stricter sandbox than their default. A worker already at maximum stays at maximum; requests to relax are ignored.">
              <select className="input" value={reqProfile} onChange={e => setReqProfile(e.target.value)}>
                <option value="">Worker default</option>
                <option value="standard">At least standard</option>
                <option value="maximum">Maximum</option>
              </select>
            </Field>
            <Field label="Cross-region workers" hint="your own routing preference"
                   help="Whether THIS dispatch may use relay-only workers outside your region. This is your preference for your own task, so both directions work; blank uses your node setting.">
              <select className="input" value={xRegion} onChange={e => setXRegion(e.target.value)}>
                <option value="">Node default</option>
                <option value="allow">Allow cross-region</option>
                <option value="deny">Local region only</option>
              </select>
            </Field>
          </div>
          <div className="row" style={{ gap: 24, flexWrap: "wrap" }}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={reqNetwork} onChange={setReqNetwork}/>
              <span style={{ fontSize: 13 }}>Needs network access<Help text="The task declares it needs internet. It will only run on workers whose own settings allow network tasks — a worker's security posture is never overridden."/></span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={reqVenvIso} onChange={setReqVenvIso}/>
              <span style={{ fontSize: 13 }}>Force venv isolation<Help text="Demand an isolated Python environment for this task even on workers that don't isolate by default. Stricter-than-default requests are always honored."/></span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={noVenvCache} onChange={setNoVenvCache}/>
              <span style={{ fontSize: 13 }}>Skip venv cache<Help text="Build a fresh environment for this task instead of reusing the worker's cached one — useful when you suspect a stale dependency."/></span>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Chk on={reqScan} onChange={setReqScan}/>
              <span style={{ fontSize: 13 }}>Force code scan<Help text="Ask the worker to static-scan this task's code before running even if its own scanning is off. Workers that already scan keep scanning."/></span>
            </div>
          </div>
          </Disclosure>
        </Sec>

        <Sec icon={<I.users size={14}/>} tone="cyan" title="Where it runs" sub="empty = auto-pick the best trusted worker">
          <Targeting groups={groups} workerPool={workerPool}
                     selGroups={selGroups} setSelGroups={setSelGroups}
                     blocked={blocked} setBlocked={setBlocked}
                     manual={manual} setManual={setManual}
                     selWorkers={selWorkers} setSelWorkers={setSelWorkers}/>
        </Sec>

        <div className="row" style={{ justifyContent: "flex-end", gap: 10 }}>
          <button className="btn accent" disabled={busy} onClick={dispatch} style={{ minWidth: 160, justifyContent: "center" }}>
            {busy ? "Dispatching…" : <><I.zap size={14}/> Queue workload</>}
          </button>
        </div>
      </div>

    </>
  );
};

export { DispatcherScreen };
