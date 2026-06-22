/* Diagnostics — system-health deep view from /local/diagnostics, plus the
 * audit-event feed and the venv/node/pip cache manager with pre-warm. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Kpi, CardHead, Bar, Pill, Field } from "../components.jsx";
import { notify } from "../toast.jsx";
import { fmtAgo } from "../notify.js";

const rate = (n) => {
  if (n == null) return "0 B/s";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i] + "/s";
};

const sevTone = (s) => s === "error" || s === "critical" ? "rose" : s === "warning" ? "amber" : "ghost";

/* ── Audit-event feed (security/storage/relay/group actions this node logged) ── */
const AuditCard = () => {
  const [events, setEvents] = React.useState([]);
  const [q, setQ] = React.useState("");
  React.useEffect(() => {
    const load = () => api.get("/local/audit?limit=200").then(r => setEvents(r.events || [])).catch(() => {});
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);
  const shown = events.filter(e =>
    !q || `${e.action} ${e.actor} ${e.details} ${e.task_id}`.toLowerCase().includes(q.toLowerCase())).slice(0, 200);
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <CardHead icon={<I.list size={14}/>} tone="purple" title="Audit feed" meta={<span>{events.length} events</span>}>
        <input className="input mono" placeholder="Filter…" style={{ marginLeft: "auto", width: 200 }}
               value={q} onChange={e => setQ(e.target.value)}/>
        <a className="btn ghost sm" style={{ marginLeft: 8 }} download
           href={`/local/audit/export?format=csv&local_token=${encodeURIComponent(api.token)}`}
           onClick={() => notify(`Audit log exported as CSV (${events.length} events)`)}>
          <I.download size={13}/> Export CSV
        </a>
      </CardHead>
      {shown.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No audit events{q ? " match the filter" : " yet"}.</div>}
      {shown.length > 0 && (
        <div style={{ maxHeight: 320, overflowY: "auto" }}>
          <table className="t">
            <tbody>
              {shown.map((e, i) => (
                <tr key={i}>
                  <td className="mono dim" style={{ fontSize: 10.5, whiteSpace: "nowrap" }}
                      title={e.ts ? new Date(e.ts * 1000).toLocaleString() : ""}>
                    {fmtAgo((e.ts || 0) * 1000)}
                  </td>
                  <td className="mono" style={{ fontSize: 11.5 }}>{e.action}</td>
                  <td><Pill tone={sevTone(e.severity)}>{e.severity || "info"}</Pill></td>
                  <td className="mono dim" style={{ fontSize: 10.5, wordBreak: "break-all" }}>{e.details || e.task_id || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

/* ── Worker caches: venv / node_modules / pip wheels + pre-warm ── */
const CacheCard = () => {
  const [info, setInfo] = React.useState(null);
  const [reqs, setReqs] = React.useState("");
  const [job, setJob] = React.useState(null); // {id, status, log, elapsed}
  const [msg, setMsg] = React.useState("");
  const load = React.useCallback(() => {
    api.get("/local/venv_cache_info").then(setInfo).catch(() => setInfo({}));
  }, []);
  React.useEffect(() => { load(); }, [load]);

  // Poll an active prewarm job until it leaves queued/running.
  React.useEffect(() => {
    if (!job || !job.id || ["done", "failed", "error"].includes(job.status)) return;
    const id = setInterval(async () => {
      try {
        const s = await api.get(`/local/prewarm_status/${encodeURIComponent(job.id)}`);
        setJob(j => ({ ...j, ...s }));
        if (["done", "failed", "error"].includes(s.status)) load();
      } catch (_) {}
    }, 2000);
    return () => clearInterval(id);
  }, [job && job.id, job && job.status, load]);

  const prewarm = async () => {
    if (!reqs.trim()) return;
    const fd = new FormData();
    fd.append("requirements", reqs);
    try {
      const r = await api.post("/local/prewarm_venv", fd);
      setJob({ id: r.job_id, status: "queued", log: "" });
    } catch (e) { setMsg("Pre-warm failed: " + (e.detail || e.message || "")); }
  };
  const clear = async (scope) => {
    const fd = new FormData();
    fd.append("scope", scope);
    try {
      const r = await api.post("/local/clear_venv_cache", fd);
      const m = `Cleared ${r.removed || 0} ${scope} entr${(r.removed || 0) === 1 ? "y" : "ies"}`;
      setMsg(m + " ✓"); notify(m);
      load();
    } catch (e) { const m = "Clear failed: " + (e.detail || e.message || ""); setMsg(m); notify(m); }
  };
  const ClearBtn = ({ scope }) => {
    const [armed, setArmed] = React.useState(false);
    React.useEffect(() => {
      if (!armed) return;
      const id = setTimeout(() => setArmed(false), 3500);
      return () => clearTimeout(id);
    }, [armed]);
    return (
      <button className={"btn sm " + (armed ? "accent" : "ghost")}
              onClick={() => { if (armed) { setArmed(false); clear(scope); } else setArmed(true); }}>
        {armed ? "Wipe & rebuild later?" : `Clear ${scope}`}
      </button>
    );
  };
  const i = info || {};
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <CardHead icon={<I.box size={14}/>} tone="cyan" title="Worker caches"
                meta={info && <span className="hint">
                  venv caching {i.enabled ? "on" : "off"}{i.uv_available ? " · uv available" : ""}
                </span>}/>
      {!info && <div className="dim" style={{ padding: 16, fontSize: 12 }}>Loading…</div>}
      {info && (
        <div className="col" style={{ gap: 10, padding: "10px 14px 14px" }}>
          <div className="field-row tri">
            <div><div className="hint">Python venvs</div><div className="mono name">{(i.entries || []).length} · {i.total_mb || 0} MB</div></div>
            <div><div className="hint">node_modules</div><div className="mono name">{(i.node_entries || []).length} · {i.node_total_mb || 0} MB</div></div>
            <div><div className="hint">pip wheels</div><div className="mono name">{(i.pip_wheels || []).length} · {i.pip_cache_mb || 0} MB</div></div>
          </div>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <ClearBtn scope="venv"/><ClearBtn scope="node"/><ClearBtn scope="pip"/><ClearBtn scope="all"/>
          </div>
          <hr className="divider" style={{ margin: "4px 0" }}/>
          <Field label="Pre-warm a venv" hint="paste requirements.txt lines — builds the env now so the first task using it starts instantly">
            <textarea className="input mono" rows={3} style={{ resize: "vertical", fontSize: 12 }}
                      placeholder={"numpy\npandas==2.2.0"} value={reqs} onChange={e => setReqs(e.target.value)}/>
          </Field>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <button className="btn accent sm" disabled={!reqs.trim() || (job && !["done", "failed", "error"].includes(job.status))} onClick={prewarm}>
              <I.zap size={13}/> Pre-warm
            </button>
            {job && (
              <span className="hint mono">
                job {job.id}: {job.status}{job.elapsed_sec != null ? ` · ${job.elapsed_sec}s` : ""}{job.log ? ` · ${String(job.log).slice(-120)}` : ""}
              </span>
            )}
          </div>
          {msg && <div className="hint" style={{ color: msg.includes("✓") ? "var(--emerald, #34d399)" : "var(--rose, #fb7185)" }}>{msg}</div>}
        </div>
      )}
    </div>
  );
};

const fmtBytes = (n) => {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};

/* Stable colour per storage category — used by both the donut and the legend. */
const STORAGE_COLORS = {
  database: "#22d3ee", identity: "#a78bfa", artifacts: "#34d399",
  hosted: "#60a5fa", caches: "#fbbf24", backups: "#f472b6", stale_db: "#fb7185",
};

/* Per-category file manager — a separate full-screen overlay (its own UI, not
 * the chart card). Scrolls for large lists; delete specific files or all. */
const StorageFilesOverlay = ({ cat, onClose, onChanged }) => {
  const [files, setFiles] = React.useState(null);
  const load = React.useCallback(() => {
    api.get(`/local/storage_usage/files?key=${encodeURIComponent(cat.key)}`)
      .then(r => setFiles(r.files || [])).catch(() => setFiles([]));
  }, [cat.key]);
  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const delOne = async (path) => {
    try {
      const r = await api.post("/local/storage_usage/delete_file", { key: cat.key, path });
      notify(`Freed ${fmtBytes(r.removed_bytes)}`); load(); onChanged && onChanged();
    } catch (e) { notify("Delete failed: " + (e.detail || e.message || "")); }
  };
  const [armed, setArmed] = React.useState(false);
  const delAll = async () => {
    if (!armed) { setArmed(true); setTimeout(() => setArmed(false), 3500); return; }
    setArmed(false);
    try {
      const r = await api.post("/local/storage_usage/clear", { key: cat.key });
      notify(`Freed ${fmtBytes(r.removed_bytes)} — ${cat.key}`); load(); onChanged && onChanged();
    } catch (e) { notify("Delete failed: " + (e.detail || e.message || "")); }
  };
  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
      <div style={{ width: "min(900px, 94vw)", height: "min(86vh, 780px)", background: "var(--bg-card)",
                    border: "1px solid var(--br-s)", borderRadius: 12, display: "flex", flexDirection: "column",
                    overflow: "hidden", boxShadow: "0 24px 64px rgba(0,0,0,0.6)" }}>
        <div className="row" style={{ padding: "12px 16px", borderBottom: "1px solid var(--br)", alignItems: "center", gap: 11 }}>
          <span className="ico-tile blue" style={{ width: 28, height: 28 }}><I.hdd size={14}/></span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 14 }}>{cat.label}</div>
            <div className="hint" style={{ fontSize: 11 }}>{cat.note}{files ? ` · ${files.length} file${files.length === 1 ? "" : "s"}` : ""}</div>
          </div>
          <button className={"btn sm " + (armed ? "accent" : "ghost")} disabled={!files || !files.length} onClick={delAll}>
            <I.trash size={12}/> {armed ? "Delete everything?" : "Delete all"}
          </button>
          <button className="icon-btn" title="Close" onClick={onClose}><I.x size={14}/></button>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "4px 16px 16px" }}>
          {!files && <div className="hint" style={{ padding: "10px 0" }}>Loading…</div>}
          {files && files.length === 0 && <div className="hint" style={{ padding: "10px 0" }}>No files — nothing to delete here.</div>}
          {files && files.map(f => (
            <div key={f.path} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 2px", borderBottom: "1px solid var(--br)" }}>
              <span className="mono" style={{ flex: 1, fontSize: 11.5, wordBreak: "break-all" }}>{f.path}</span>
              <span className="mono hint" style={{ fontSize: 11, flexShrink: 0 }}>{fmtBytes(f.bytes)}</span>
              <button className="icon-btn" style={{ width: 26, height: 26, flexShrink: 0 }} title="Delete this file" onClick={() => delOne(f.path)}><I.trash size={12}/></button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

/* App storage footprint: an interactive donut on the left, colour-keyed legend
 * on the right. Click a category (slice or legend row) to spotlight its share;
 * click anywhere off a category to reset. "Delete…" swaps the chart for a full
 * scrollable file view. Live data / identity / peers' deposits show a "kept"
 * lock. Stays in sync with artifact/database changes elsewhere via the
 * nexus-results-changed event. */
const StorageCard = () => {
  const [data, setData] = React.useState(null);
  const [sel, setSel] = React.useState(null);       // spotlighted category key
  const [filesCat, setFilesCat] = React.useState(null);  // category open in the file view
  const cardRef = React.useRef(null);
  const load = React.useCallback(() => {
    api.get("/local/storage_usage").then(setData).catch(() => setData({ categories: [], total_bytes: 0 }));
  }, []);
  React.useEffect(() => { load(); }, [load]);
  // Reflect changes made elsewhere (telemetry "Clear database", artifact deletes).
  React.useEffect(() => {
    const on = () => load();
    window.addEventListener("nexus-results-changed", on);
    return () => window.removeEventListener("nexus-results-changed", on);
  }, [load]);
  // Click anywhere outside a category (even off this card) resets the spotlight.
  React.useEffect(() => {
    if (!sel) return;
    const on = (e) => { if (!cardRef.current || !cardRef.current.contains(e.target)) setSel(null); };
    document.addEventListener("mousedown", on);
    return () => document.removeEventListener("mousedown", on);
  }, [sel]);
  const cats = (data && data.categories) || [];
  const total = (data && data.total_bytes) || 0;
  const pct = (b) => total > 0 ? Math.round((b / total) * 100) : 0;
  const toggle = (e, k) => { e.stopPropagation(); setSel(s => s === k ? null : k); };
  // Donut geometry. Every non-empty category gets at least a small visible arc
  // (so a 2.9 KB sliver still shows its colour), normalised back to a full ring.
  const R = 66, SW = 26, GAP = 3, C = 2 * Math.PI * R, MINF = 0.03;
  const nz = cats.filter(c => c.bytes > 0);
  const adj = nz.map(c => Math.max(c.bytes / (total || 1), MINF));
  const sumAdj = adj.reduce((a, b) => a + b, 0) || 1;
  let off = 0;
  const segs = nz.map((c, i) => {
    const full = (adj[i] / sumAdj) * C;
    const seg = { key: c.key, len: Math.max(0.6, full - GAP), off }; off += full; return seg;
  });
  const selCat = sel ? cats.find(c => c.key === sel) : null;
  return (
    <div className="card" style={{ marginBottom: 16 }} ref={cardRef}>
      <CardHead icon={<I.hdd size={14}/>} tone="blue" title="Storage usage"
                meta={data && <span className="hint">{fmtBytes(total)} total</span>}>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={load} title="Refresh"><I.refresh size={13}/></button>
      </CardHead>
      {!data && <div className="dim" style={{ padding: 16, fontSize: 12 }}>Loading…</div>}
      {data && cats.length > 0 && (
        // Click anywhere that isn't a slice/row/button resets the spotlight.
        <div className="row" onClick={() => setSel(null)}
             style={{ gap: 22, padding: "14px 16px 16px", alignItems: "center", flexWrap: "wrap" }}>
          {/* LEFT — interactive donut with a soft drop-shadow for depth. Clicking
              the donut while a category is spotlighted returns to normal. */}
          <svg width={210} height={210} viewBox="0 0 210 210" style={{ flexShrink: 0, cursor: sel ? "pointer" : "default" }}
               onClick={(e) => { e.stopPropagation(); setSel(null); }}>
            <defs>
              <filter id="donutShadow" x="-30%" y="-30%" width="160%" height="160%">
                <feDropShadow dx="0" dy="3" stdDeviation="4" floodColor="#000" floodOpacity="0.45"/>
              </filter>
            </defs>
            <circle cx={105} cy={105} r={R} fill="none" stroke="var(--br)" strokeWidth={SW}/>
            <g transform="rotate(-90 105 105)" filter="url(#donutShadow)">
              {segs.map(s => {
                const on = sel === s.key, dim = sel && !on;
                return (
                  <circle key={s.key} cx={105} cy={105} r={R} fill="none"
                          stroke={STORAGE_COLORS[s.key] || "var(--t-dim)"} strokeLinecap="round"
                          strokeWidth={on ? SW + 6 : SW} opacity={dim ? 0.28 : 1}
                          strokeDasharray={`${s.len} ${C - s.len}`} strokeDashoffset={-s.off}
                          style={{ cursor: "pointer", transition: "opacity .15s, stroke-width .15s" }}
                          onClick={(e) => { e.stopPropagation(); setSel(cur => cur ? null : s.key); }}/>
                );
              })}
            </g>
            <text x={105} y={selCat ? 101 : 103} textAnchor="middle" style={{ font: "700 17px var(--f-sans, inherit)", fill: "var(--t)", pointerEvents: "none" }}>
              {selCat ? fmtBytes(selCat.bytes) : fmtBytes(total)}
            </text>
            <text x={105} y={selCat ? 119 : 121} textAnchor="middle" style={{ font: "9.5px var(--f-mono)", fill: "var(--t-dim)", pointerEvents: "none" }}>
              {selCat ? pct(selCat.bytes) + "% · " + selCat.key : "on disk"}
            </text>
          </svg>
          {/* RIGHT — legend (click a row to spotlight) + delete / kept */}
          <div className="col" style={{ gap: 4, flex: 1, minWidth: 300 }}>
            {cats.map(c => (
              <div key={c.key} className="row" onClick={(e) => toggle(e, c.key)}
                   style={{ gap: 9, alignItems: "center", cursor: "pointer", padding: "4px 6px", borderRadius: 7,
                            background: sel === c.key ? "rgba(99,102,241,0.10)" : "transparent",
                            opacity: sel && sel !== c.key ? 0.5 : 1, transition: "opacity .15s, background .15s" }}>
                <span style={{ width: 11, height: 11, borderRadius: 3, flexShrink: 0,
                               background: STORAGE_COLORS[c.key] || "var(--t-dim)" }}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12.5 }}>{c.label}</div>
                  <div className="hint" style={{ fontSize: 11 }}>{c.note}</div>
                </div>
                <span className="hint mono" style={{ width: 34, textAlign: "right", fontSize: 11 }}>{pct(c.bytes)}%</span>
                <span className="mono" style={{ width: 66, textAlign: "right", fontSize: 12 }}>{fmtBytes(c.bytes)}</span>
                <div style={{ width: 84, textAlign: "right", flexShrink: 0 }}>
                  {c.deletable
                    ? <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); setFilesCat(c); }}>Delete…</button>
                    : <span className="hint" title={c.note} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><I.lock size={11}/> kept</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {filesCat && <StorageFilesOverlay cat={filesCat} onClose={() => setFilesCat(null)} onChanged={load}/>}
    </div>
  );
};

const DiagnosticsScreen = () => {
  const [diag, setDiag] = React.useState({});
  const [metrics, setMetrics] = React.useState({});

  const load = React.useCallback(async () => {
    const [d, net] = await Promise.all([
      api.get("/local/diagnostics").catch(() => ({})),
      api.get("/local/network").catch(() => ({})),
    ]);
    setDiag(d || {});
    setMetrics((net && net.metrics) || (d && d.metrics) || {});
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const lw = diag.local_worker || {};
  const io = lw.net_io || {};
  // GPU bar is shown only when this machine actually has one. Usage % rides on
  // gpu_stats (utilization), same source the sidebar/overview use.
  const gs = lw.gpu_stats || {};
  const gpuPct = (typeof (gs.utilization ?? gs.util ?? gs.gpu_util ?? gs.load) === "number")
    ? (gs.utilization ?? gs.util ?? gs.gpu_util ?? gs.load) : null;
  const gpu = { has: !!(lw.gpu || lw.gpu_name || gpuPct != null), pct: gpuPct, name: lw.gpu_name || "" };
  const issues = [...(diag.issues || []), ...(diag.recent_alerts || [])];
  const workers = diag.workers || {};
  const onlineWorkers = Object.values(workers).filter(w => w && w.online).length;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Diagnostics</div>
          <div className="page-sub">Live health of this node and the work it's coordinating.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
        </div>
      </div>

      <div className="kpi-row">
        <Kpi label="Queue depth"      value={String(metrics.queue_depth ?? 0)} />
        <Kpi label="Processing"       value={String(metrics.processing_depth ?? 0)} />
        <Kpi label="Active workers"   value={String(metrics.active_workers ?? 0)} />
        <Kpi label="Tasks completed"  value={String(metrics.tasks_completed ?? 0)} />
        <Kpi label="Tasks failed"     value={String(metrics.tasks_failed ?? 0)} />
      </div>

      <div className="split-2" style={{ marginBottom: 16 }}>
        <div className="card pad-lg">
          <div className="fsec-head">
            <span className="ico-tile emerald" style={{ width: 28, height: 28 }}><I.cpu size={14}/></span>
            <h4>This node</h4>
            <Pill tone={lw.node_online ? "emerald" : "rose"} dot style={{ marginLeft: "auto" }}>{lw.status || (lw.node_online ? "online" : "offline")}</Pill>
          </div>
          <div className="node-bars" style={{ gap: 10 }}>
            <div className="node-bar-row"><span style={{ width: 38 }}>CPU</span><Bar value={lw.cpu || 0} threshold/><span className="mono">{lw.cpu != null ? Math.round(lw.cpu) + "%" : "—"}</span></div>
            <div className="node-bar-row"><span style={{ width: 38 }}>RAM</span><Bar value={lw.ram || 0} threshold/><span className="mono">{lw.ram != null ? Math.round(lw.ram) + "%" : "—"}</span></div>
            {gpu.has && (
              <div className="node-bar-row" title={gpu.name}>
                <span style={{ width: 38 }}>GPU</span>
                <Bar value={gpu.pct != null ? gpu.pct : 0} threshold/>
                <span className="mono">{gpu.pct != null ? Math.round(gpu.pct) + "%" : "—"}</span>
              </div>
            )}
          </div>
          {gpu.has && gpu.name && <div className="hint mono" style={{ marginTop: 6 }}>{gpu.name}</div>}
          <div className="field-row tri" style={{ marginTop: 14 }}>
            <div><div className="hint">Free RAM</div><div className="mono name">{lw.free_ram != null ? lw.free_ram + " MB" : "—"}</div></div>
            <div><div className="hint">App RAM</div><div className="mono name">{lw.process_ram_mb != null ? lw.process_ram_mb + " MB" : "—"}</div></div>
            <div><div className="hint">Dispatch cap</div><div className="mono name">{lw.dispatch_ram_cap_mb != null ? lw.dispatch_ram_cap_mb + " MB" : "—"}</div></div>
          </div>
          <div className="field-row tri" style={{ marginTop: 14 }}>
            <div><div className="hint">Net out</div><div className="mono name">{rate(io.sent_per_sec)}</div></div>
            <div><div className="hint">Net in</div><div className="mono name">{rate(io.recv_per_sec)}</div></div>
          </div>
        </div>

        <div className="card">
          <CardHead icon={<I.pulse size={14}/>} tone="amber" title="Issues" meta={<span>{issues.length}</span>}/>
          {issues.length === 0 && (
            <div className="rail-item">
              <div className="rail-icon"><I.check size={14}/></div>
              <div className="rail-text">No issues detected<div className="rail-sub">{onlineWorkers} worker{onlineWorkers === 1 ? "" : "s"} online</div></div>
            </div>
          )}
          {/* Cap the list height and scroll it — a node with many issues must not
              stretch the whole card (and the page) taller and taller. */}
          {issues.length > 0 && (
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              {issues.map((it, i) => {
                const text = typeof it === "string" ? it : (it.message || it.text || it.title || JSON.stringify(it));
                return (
                  <div key={i} className="rail-item alert">
                    <div className="rail-icon alert"><I.alertT size={14}/></div>
                    <div className="rail-text">{text}</div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <StorageCard/>
      <AuditCard/>
      <CacheCard/>
    </>
  );
};

export { DiagnosticsScreen };
