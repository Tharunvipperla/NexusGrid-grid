/* Task Telemetry — the task list from /local/network with status filtering
 * and the full classic action set: logs (live tail while processing), output
 * download, re-queue, clone-to-dispatcher, disrupt, preempt, delete, and
 * clear-database. Destructive actions use a two-click inline confirm instead
 * of popups. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, CardHead, Modal, DagGraph } from "../components.jsx";
import { toast, notify } from "../toast.jsx";

const fmtDur = (s) => {
  s = Math.round(Number(s) || 0);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
};

const tone = (s) => ({
  completed: "emerald", processing: "cyan", serving: "cyan",
  queued: "amber", waiting: "amber", retrying: "amber", preempted: "amber", lease_expired: "amber",
  awaiting_approval: "purple",
  failed: "rose", disrupted: "rose", cancelled: "ghost",
}[s] || "ghost");

const GROUPS = {
  active: ["processing", "serving", "queued", "waiting", "awaiting_approval", "retrying", "preempted", "lease_expired"],
  done: ["completed"],
  failed: ["failed", "disrupted", "cancelled"],
};
const TERMINAL = ["completed", "failed", "disrupted", "cancelled"];

const elapsedLabel = (startedAt) => {
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - (Number(startedAt) || Date.now() / 1000)));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
};

/* Two-click destructive button: first click arms it, second fires. ``big``
 * renders it at the normal button size (to sit uniformly beside Refresh etc.). */
const Danger = ({ label, confirmLabel = "Sure?", onFire, big = false }) => {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 3500);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <button className={"btn " + (big ? "" : "sm ") + (armed ? "accent" : "ghost")}
            onClick={() => { if (armed) { setArmed(false); onFire(); } else setArmed(true); }}>
      {armed ? confirmLabel : label}
    </button>
  );
};

/* Inline log pane. Seeds from the task's rolling log; while the task is
 * active it live-tails /local/task_log_tail (same poll the classic UI uses). */
const LogPane = ({ taskId, seed, active }) => {
  const [lines, setLines] = React.useState(() => (seed ? String(seed).split("\n") : []));
  const cursor = React.useRef(0);
  const boxRef = React.useRef(null);

  React.useEffect(() => {
    if (!active) return;
    let dead = false, inFlight = false;
    const tick = async () => {
      if (dead || inFlight) return;
      inFlight = true;
      try {
        const d = await api.get(`/local/task_log_tail/${encodeURIComponent(taskId)}?since=${cursor.current}`);
        if (dead) return;
        if (typeof d.cursor === "number") cursor.current = d.cursor;
        if (Array.isArray(d.lines) && d.lines.length) {
          setLines(prev => [...prev, ...d.lines].slice(-800));
        }
      } catch (_) {} finally { inFlight = false; }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => { dead = true; clearInterval(id); };
  }, [taskId, active]);

  React.useEffect(() => {
    const el = boxRef.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight;
  }, [lines]);

  const [saving, setSaving] = React.useState(false);
  const saveArtifact = async () => {
    setSaving(true);
    try {
      const r = await api.post(`/local/task_log_tail/${encodeURIComponent(taskId)}/save`);
      notify((r && r.message) || "Log saved as artifact");
      window.dispatchEvent(new Event("nexus-results-changed"));
    } catch (e) { notify("Save log failed: " + (e.detail || e.message)); }
    finally { setSaving(false); }
  };

  return (
    <div>
      <div className="row" style={{ marginBottom: 6, alignItems: "center", gap: 8 }}>
        <span className="hint">
          {active ? <><Pill tone="cyan" dot>live</Pill> streaming this task's output</> : "Final log tail"}
        </span>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} disabled={saving || !lines.length}
                onClick={saveArtifact} title="Write the current log buffer to this task's result artifacts">
          <I.box size={12}/> {saving ? "Saving…" : "Save as artifact"}
        </button>
      </div>
      <pre ref={boxRef} className="mono" style={{
        margin: 0, padding: 12, fontSize: 11.5, lineHeight: 1.5, maxHeight: 260, overflow: "auto",
        background: "var(--bg-base, rgba(0,0,0,0.35))", border: "1px solid var(--br)", borderRadius: 8,
        whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>{lines.length ? lines.join("\n") : (active ? "Waiting for log output…" : "No logs available.")}</pre>
    </div>
  );
};

/* "Why is this queued?" pane — the server re-runs the real scheduler gates
 * per worker and we show its verdicts verbatim, so the user always sees the
 * actual blocker (and what to do about it), not a frozen "queued" pill. */
const QueueInsight = ({ taskId, name }) => {
  const [d, setD] = React.useState(null);
  React.useEffect(() => {
    let dead = false;
    const tick = () => api.get(`/local/task_queue_insight/${encodeURIComponent(taskId)}`)
      .then(x => !dead && setD(x)).catch(() => {});
    tick();
    const id = setInterval(tick, 5000);
    return () => { dead = true; clearInterval(id); };
  }, [taskId]);
  if (!d) return <div className="hint">Checking the scheduler…</div>;
  return (
    <div>
      <div style={{ fontSize: 12.5, marginBottom: (d.workers || []).length ? 8 : 0 }}>{d.summary}</div>
      {(d.workers || []).map(w => (
        <div key={w.worker} className="row" style={{ gap: 8, alignItems: "center", padding: "3px 0", fontSize: 12 }}>
          <span style={{ width: 7, height: 7, borderRadius: "50%", flexShrink: 0,
                         background: w.ok ? "var(--emerald, #34d399)" : "var(--amber, #fbbf24)" }}/>
          <span className="mono" style={{ minWidth: 120 }}>{name(w.worker)}</span>
          <span className="dim">{w.reason}</span>
        </div>
      ))}
      {(d.notes || []).map((n, i) => (
        <div key={i} className="hint" style={{ marginTop: 6 }}><I.info size={11}/> {n}</div>
      ))}
    </div>
  );
};

/* Detail modal: everything about one task in one place — status, routing,
 * manifest, and the full log. */
const TaskDetail = ({ task, live, name, onClose, actions }) => {
  const [manifest, setManifest] = React.useState(null);
  React.useEffect(() => {
    let dead = false;
    api.get(`/local/task_manifest/${encodeURIComponent(task.id)}`)
      .then(d => { if (!dead) setManifest((d && d.manifest) || null); })
      .catch(() => {});
    return () => { dead = true; };
  }, [task.id]);
  const meta = [
    ["Status", task.status], ["Worker", task.worker ? name(task.worker) : "—"],
    ["Priority", "P" + (task.priority ?? 50)], ["Retries", `${task.retry_count ?? 0}/${task.retry_max ?? 0}`],
    ["Duration", task.elapsed_secs != null ? fmtDur(task.elapsed_secs) + (["completed", "failed", "disrupted", "cancelled"].includes(task.status) ? "" : " (running)") : "—"],
    ["Route", String(task.coordination_text || task.coordination || "awaiting route").replace(/nexus_[0-9a-f]+/gi, (m) => name(m))],
  ];
  const dagNodes = Array.isArray(manifest) ? manifest
                 : (manifest && Array.isArray(manifest.tasks)) ? manifest.tasks : null;
  return (
    <Modal title={task.display_id || task.id} icon={<I.list size={15}/>} tone="cyan" width={760} onClose={onClose}>
      <div className="row" style={{ gap: 16, flexWrap: "wrap", marginBottom: 12 }}>
        {meta.map(([k, v]) => (
          <div key={k}><div className="label">{k}</div><div className="mono" style={{ fontSize: 12.5 }}>{String(v)}</div></div>
        ))}
      </div>
      {task.status === "queued" && (
        <div style={{ marginBottom: 12 }}>
          <div className="label" style={{ marginBottom: 6 }}>Why is this queued?</div>
          <QueueInsight taskId={task.id} name={name}/>
        </div>
      )}
      {dagNodes && dagNodes.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <div className="label" style={{ marginBottom: 6 }}>Workflow graph</div>
          <DagGraph nodes={dagNodes} height={Math.max(140, dagNodes.length * 26)}/>
        </div>
      )}
      {manifest && !Array.isArray(manifest) && (
        <div style={{ marginBottom: 12 }}>
          <div className="label" style={{ marginBottom: 6 }}>Manifest</div>
          <pre className="mono" style={{ margin: 0, padding: 10, fontSize: 11, maxHeight: 160, overflow: "auto",
                                         background: "var(--bg-base, rgba(0,0,0,0.35))", border: "1px solid var(--br)", borderRadius: 8 }}>
            {JSON.stringify(manifest, null, 2)}
          </pre>
        </div>
      )}
      <LogPane taskId={task.id} seed={task.logs} active={task.status === "processing" || !!live}/>
      <div className="row" style={{ gap: 6, marginTop: 14, justifyContent: "flex-end", flexWrap: "wrap" }}>{actions}</div>
    </Modal>
  );
};

/* Filter + search survive navigation within the session (module-level, like
 * the overview sample buffer) — coming back to the screen keeps your view. */
const VIEW_STATE = { filter: "all", q: "" };

const fmtBytes = (n) => {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};

/* B3 — result/artifact browser: list completed-task bundles, expand one to
 * browse its files, preview small text files inline, download any file. */
const ResultArtifacts = () => {
  const [bundles, setBundles] = React.useState(null);
  const [open, setOpen] = React.useState(null);     // task_id expanded
  const [files, setFiles] = React.useState({});      // task_id -> [{path,bytes}]
  const [preview, setPreview] = React.useState(null); // {tid, path, text}

  const load = React.useCallback(() => {
    api.get("/local/results").then(r => setBundles(r.bundles || [])).catch(() => setBundles([]));
  }, []);
  React.useEffect(() => {
    load();
    // Refresh when another part of the screen changes artifacts (e.g. "Clear
    // database" wipes bundles too) so the list updates without a tab switch.
    const onChange = () => load();
    window.addEventListener("nexus-results-changed", onChange);
    return () => window.removeEventListener("nexus-results-changed", onChange);
  }, [load]);

  const expand = async (tid) => {
    setPreview(null);
    if (open === tid) { setOpen(null); return; }
    setOpen(tid);
    if (!files[tid]) {
      try {
        const r = await api.get(`/local/results/${encodeURIComponent(tid)}/files`);
        setFiles(f => ({ ...f, [tid]: r.files || [] }));
      } catch (_) { setFiles(f => ({ ...f, [tid]: [] })); }
    }
  };

  const fileUrl = (tid, p) =>
    `/local/results/${encodeURIComponent(tid)}/file?path=${encodeURIComponent(p)}&local_token=${encodeURIComponent(api.token)}`;

  const showPreview = async (tid, p) => {
    try {
      const res = await fetch(fileUrl(tid, p));
      const text = await res.text();
      setPreview({ tid, path: p, text });
    } catch (_) { toast("Preview failed", "danger"); }
  };

  const delBundle = async (tid) => {
    try {
      const r = await api.del(`/local/results/${encodeURIComponent(tid)}`);
      notify((r && r.message) || "Result artifacts deleted");
      window.dispatchEvent(new Event("nexus-results-changed"));   // keep Diagnostics storage in sync
    }
    catch (e) { notify("Delete failed: " + (e.detail || e.message)); }
    finally { if (open === tid) setOpen(null); load(); }
  };

  const isText = (p) => /\.(txt|log|json|csv|md|yaml|yml|py|js|ts|html|xml|cfg|ini|env|tsv)$/i.test(p);

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <CardHead icon={<I.box size={14}/>} tone="purple" title="Result artifacts"
                meta={<span>{bundles ? `${bundles.length} bundle${bundles.length === 1 ? "" : "s"}` : "…"}</span>}>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={load}><I.refresh size={13}/> Refresh</button>
      </CardHead>
      {bundles && bundles.length === 0 &&
        <div className="dim" style={{ padding: 16, fontSize: 12 }}>No result bundles yet — completed task outputs appear here.</div>}
      {bundles && bundles.length > 0 && (
        <table className="t">
          <tbody>
            {bundles.map((b) => (
              <React.Fragment key={b.task_id}>
                <tr>
                  <td className="mono name" style={{ fontSize: 12, cursor: "pointer" }} onClick={() => expand(b.task_id)}>
                    <I.chevronRight size={11} style={{ transform: open === b.task_id ? "rotate(90deg)" : "none", transition: "transform .15s" }}/> {b.task_id}
                  </td>
                  <td className="dim mono" style={{ fontSize: 11 }}>{b.file_count} file{b.file_count === 1 ? "" : "s"}</td>
                  <td className="dim mono" style={{ fontSize: 11 }}>{fmtBytes(b.total_bytes)}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <a className="btn ghost sm" href={`/local/download/${encodeURIComponent(b.task_id)}?local_token=${encodeURIComponent(api.token)}`}><I.download size={12}/> Zip</a>
                    <span style={{ marginLeft: 6, display: "inline-block" }}>
                      <Danger label={<I.trash size={12}/>} confirmLabel={<I.trash size={12}/>} onFire={() => delBundle(b.task_id)}/>
                    </span>
                  </td>
                </tr>
                {open === b.task_id && (
                  <tr><td colSpan={4} style={{ padding: "4px 14px 12px" }}>
                    {!files[b.task_id] && <div className="dim" style={{ fontSize: 12 }}>Loading…</div>}
                    {files[b.task_id] && files[b.task_id].length === 0 && <div className="dim" style={{ fontSize: 12 }}>Empty bundle.</div>}
                    {files[b.task_id] && files[b.task_id].map((f) => (
                      <div key={f.path}>
                        <div className="row" style={{ gap: 8, alignItems: "center", padding: "3px 0" }}>
                          <span className="mono" style={{ fontSize: 11.5, flex: 1, wordBreak: "break-all" }}>{f.path}</span>
                          <span className="dim mono" style={{ fontSize: 10.5 }}>{fmtBytes(f.bytes)}</span>
                          {isText(f.path) && f.bytes <= 256 * 1024 &&
                            <button className="btn ghost sm" onClick={() => showPreview(b.task_id, f.path)}><I.eye size={12}/> Preview</button>}
                          <a className="btn ghost sm" href={fileUrl(b.task_id, f.path)} download><I.download size={12}/></a>
                        </div>
                      </div>
                    ))}
                  </td></tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      )}
      {preview && (
        <Modal title={preview.path} icon={<I.box size={14}/>} tone="purple" width={860}
               onClose={() => setPreview(null)}
               foot={<a className="btn ghost sm" href={fileUrl(preview.tid, preview.path)} download><I.download size={13}/> Download</a>}>
          <pre className="mono" style={{ fontSize: 12, margin: 0, maxHeight: "70vh", overflow: "auto", whiteSpace: "pre", wordBreak: "normal" }}>{preview.text}</pre>
        </Modal>
      )}
    </div>
  );
};

const TelemetryScreen = ({ onClone, initialTask }) => {
  const [net, setNet] = React.useState({});
  const [filter, setFilterRaw] = React.useState(VIEW_STATE.filter);
  const [detail, setDetail] = React.useState(initialTask || null); // task id with the detail modal open
  const [view, setView] = React.useState("dispatch");  // dispatch | dag | services
  const [services, setServices] = React.useState([]);
  const [q, setQRaw] = React.useState(VIEW_STATE.q);
  const setFilter = (f) => { VIEW_STATE.filter = f; setFilterRaw(f); };
  const setQ = (v) => { VIEW_STATE.q = v; setQRaw(v); };

  const load = React.useCallback(async () => {
    setNet(await api.get("/local/network").catch(() => ({})));
    api.get("/local/services").then(r => setServices((r && r.services) || [])).catch(() => {});
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const tasks = net.tasks || {};
  const names = net.peer_names || {};
  const liveByTid = {};
  for (const t of ((net.local_worker || {}).active_tasks || [])) liveByTid[t.task_id] = t;

  const rows = Object.entries(tasks).map(([id, t]) => ({ id, ...t }));
  const ql = q.trim().toLowerCase();
  const matches = rows.filter(t =>
    (filter === "all" || (GROUPS[filter] || []).includes(t.status)) &&
    (!ql || `${t.display_id || ""} ${t.id} ${t.worker || ""} ${t.status}`.toLowerCase().includes(ql)));
  // Scale guard: the DOM renders at most 300 rows; search narrows the rest.
  const MAX_ROWS = 300;
  const shown = matches.slice(0, MAX_ROWS);
  const count = (f) => f === "all" ? rows.length : rows.filter(t => (GROUPS[f] || []).includes(t.status)).length;
  const name = (k) => names[k] || k || "";

  // `bell` routes the success confirmation to the notification bell instead of
  // a toast popup — used for destructive actions (delete, clear database) so
  // they're logged quietly rather than flashing a popup. Failures still toast.
  const act = async (label, fn, bell = false) => {
    try { const r = await fn(); (bell ? notify : toast)((r && r.message) || label); }
    catch (e) { toast(label + " failed: " + (e.detail || e.message), "danger"); }
    finally { load(); }
  };

  const clone = async (taskId) => {
    try {
      const d = await api.get(`/local/task_manifest/${encodeURIComponent(taskId)}`);
      onClone && onClone(d.manifest || {}, `${taskId}_clone_${Date.now().toString(36)}`);
    } catch (e) { toast("Clone failed: " + (e.detail || e.message), "danger"); }
  };

  const actionsFor = (t) => {
    const s = t.status;
    const out = [];
    // Logs / dependencies / reasons open in the detail modal (a bounded, scrollable
    // overlay) instead of expanding inline — a long log can't shove the table around.
    const logBtn = (label) => (
      <button key="log" className="btn ghost sm" onClick={() => setDetail(t.id)}>{label}</button>
    );
    if (TERMINAL.includes(s)) {
      out.push(logBtn("Logs"));
      if (t.has_download) out.push(<a key="dl" className="btn ghost sm" href={`/local/download/${encodeURIComponent(t.id)}?local_token=${encodeURIComponent(api.token)}`}><I.download size={13}/> Output</a>);
      if (t.can_requeue) out.push(<button key="rq" className="btn ghost sm" onClick={() => act("Re-queued", () => api.post(`/local/requeue_task/${encodeURIComponent(t.id)}`))}>Re-queue</button>);
      if ((s === "failed" || s === "disrupted" || s === "cancelled") && t.parent_id)
        out.push(<button key="rdag" className="btn ghost sm" title="Re-queue this workflow's failed steps and continue the DAG"
                         onClick={() => act("Workflow resumed", () => api.post(`/local/workflows/${encodeURIComponent(t.parent_id)}/resume`))}>Resume DAG</button>);
      out.push(<button key="cl" className="btn ghost sm" title="Pre-fill the dispatcher with this task's config" onClick={() => clone(t.id)}>Clone</button>);
      if (t.can_delete) out.push(<Danger key="del" label="Delete" confirmLabel="Delete?" onFire={() => act("Deleted", () => api.del(`/local/task/${encodeURIComponent(t.id)}`), true)}/>);
    } else if (s === "waiting") {
      out.push(logBtn("Dependencies"));
      if (t.can_cancel) out.push(<Danger key="dis" label="Disrupt" onFire={() => act("Disrupted", () => api.post(`/local/cancel_task/${encodeURIComponent(t.id)}`))}/>);
    } else if (s === "processing") {
      out.push(logBtn("Live logs"));
      if (t.can_disrupt) out.push(<Danger key="dis" label="Disrupt" onFire={() => act("Disrupted", () => api.post(`/local/disrupt_task/${encodeURIComponent(t.id)}`))}/>);
      if (t.can_preempt_local) out.push(<Danger key="pre" label="Preempt" confirmLabel="Checkpoint?" onFire={() => act("Preempting", () => api.post(`/local/preempt_local_worker_task/${encodeURIComponent(t.id)}`))}/>);
    } else { // queued / retrying / preempted / lease_expired
      if (s === "queued") out.push(
        <button key="why" className="btn ghost sm" onClick={() => setDetail(t.id)}>Why queued?</button>
      );
      if (t.can_cancel) out.push(<Danger key="dis" label="Disrupt" onFire={() => act("Disrupted", () => api.post(`/local/cancel_task/${encodeURIComponent(t.id)}`))}/>);
    }
    return out;
  };

  // Coordination text can embed raw node ids (e.g. "Serving nexus_06e6…") —
  // swap any known id for its display name.
  const resolveIds = (text) => String(text || "").replace(/nexus_[0-9a-f]+/gi, (m) => names[m] || m);

  const detailFor = (t) => {
    const bits = [];
    bits.push(resolveIds(t.coordination_text || t.coordination || "awaiting route"));
    if (Array.isArray(t.target_groups) && t.target_groups.length) bits.push(`${t.target_groups.length} group${t.target_groups.length === 1 ? "" : "s"} only`);
    bits.push(`P${t.priority ?? 50}`);
    bits.push(`retry ${t.retry_count ?? 0}/${t.retry_max ?? 0}`);
    if (t.elapsed_secs != null) {
      const done = ["completed", "failed", "disrupted", "cancelled"].includes(t.status);
      bits.push((done ? "took " : "running ") + fmtDur(t.elapsed_secs));
    }
    if (t.status === "queued" && t.queue_timeout > 0 && t.queued_at > 0) {
      const remaining = Math.max(0, t.queue_timeout - Math.floor(Date.now() / 1000 - t.queued_at));
      bits.push(remaining > 0 ? `timeout in ${remaining}s` : "timing out…");
    }
    return bits.join(" · ");
  };

  // One task row (+ its log / why-queued expanders), reused by the Dispatch
  // table and the per-workflow DAG step tables.
  const taskRow = (t) => {
    const live = t.status === "processing" ? (liveByTid[t.id] || liveByTid[t.display_id || ""]) : null;
    return (
      <React.Fragment key={t.id}>
        <tr>
          <td className="mono name" style={{ fontSize: 12, cursor: "pointer" }}
              title="Open task detail" onClick={() => setDetail(t.id)}>{t.display_id || t.id}</td>
          <td><Pill tone={tone(t.status)} dot>{t.status}</Pill></td>
          <td className="mono" style={{ fontSize: 12 }}>{t.worker ? name(t.worker) : "—"}</td>
          <td className="dim" style={{ fontSize: 12 }}>
            {detailFor(t)}
            {live && (
              <div style={{ marginTop: 4, display: "flex", gap: 6, flexWrap: "wrap" }}>
                <Pill tone="cyan">{live.stage || "running"}</Pill>
                <Pill tone="ghost">⏱ {elapsedLabel(live.started_at)}</Pill>
                {Array.isArray(live.children) && live.children.length > 0 &&
                  <Pill tone="ghost">{live.children.length} child proc{live.children.length === 1 ? "" : "s"}</Pill>}
              </div>
            )}
          </td>
          <td>
            <div className="row" style={{ gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
              {actionsFor(t)}
            </div>
          </td>
        </tr>
      </React.Fragment>
    );
  };

  // Split standalone dispatches from DAG steps, and group DAG steps by workflow.
  const dispatchRows = matches.filter(t => !t.parent_id);
  const dagAll = rows.filter(t => t.parent_id);
  const dagGroups = {};
  for (const t of dagAll) (dagGroups[t.parent_id] = dagGroups[t.parent_id] || []).push(t);
  const dagList = Object.entries(dagGroups)
    .filter(([wid, steps]) => !ql || (wid + " " + steps.map(s => s.id).join(" ")).toLowerCase().includes(ql))
    .sort((a, b) => a[0].localeCompare(b[0]));
  const dispatchShown = dispatchRows.slice(0, MAX_ROWS);
  const wfStatus = (steps) => {
    if (steps.some(s => ["failed", "disrupted", "cancelled"].includes(s.status))) return "failed";
    if (steps.some(s => s.status === "processing")) return "processing";
    if (steps.some(s => s.status === "awaiting_approval")) return "awaiting_approval";
    if (steps.every(s => s.status === "completed")) return "completed";
    return "running";
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Task telemetry</div>
          <div className="page-sub">What this node is running — standalone dispatches, DAG workflows, and live services.</div>
        </div>
        <div className="page-tools">
          {(view !== "services") && rows.length > 12 && (
            <input className="input mono" placeholder="Search…" style={{ width: 160 }}
                   value={q} onChange={e => setQ(e.target.value)}/>
          )}
          {/* The three views the user asked for. */}
          <div className="seg">
            <button className={view === "dispatch" ? "on" : ""} onClick={() => setView("dispatch")}>Dispatch ({dispatchRows.length})</button>
            <button className={view === "dag" ? "on" : ""} onClick={() => setView("dag")}>DAG ({dagList.length})</button>
            <button className={view === "services" ? "on" : ""} onClick={() => setView("services")}>Services ({services.length})</button>
          </div>
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
          <Danger big label="Clear database" confirmLabel="Erase all history?" onFire={() => act("Database cleared", () => api.del("/local/database"), true).then(() => window.dispatchEvent(new Event("nexus-results-changed")))}/>
        </div>
      </div>

      {/* status filter applies to the task views, not services */}
      {view === "dispatch" && (
        <div className="seg" style={{ marginBottom: 12 }}>
          {["all", "active", "done", "failed"].map(f => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f[0].toUpperCase() + f.slice(1)}
            </button>
          ))}
        </div>
      )}

      {view === "dispatch" && (
        <div className="card">
          <CardHead icon={<I.list size={14}/>} tone="cyan" title="Standalone dispatches"
                    meta={<span>{dispatchShown.length} shown</span>}/>
          {dispatchShown.length === 0 && <div className="dim" style={{ padding: 18, fontSize: 12 }}>No standalone tasks{filter !== "all" ? " in this state" : " yet — dispatch one from the Dispatcher"}.</div>}
          {dispatchShown.length > 0 && (
            <table className="t">
              <thead><tr><th>Task</th><th>Status</th><th>Worker</th><th>Details</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
              <tbody>{dispatchShown.map(taskRow)}</tbody>
            </table>
          )}
        </div>
      )}

      {view === "dag" && (
        <div className="col" style={{ gap: 14 }}>
          {dagList.length === 0 && <div className="card dim" style={{ padding: 18, fontSize: 12 }}>No DAG workflows{ql ? " match the search" : " yet — deploy one from the Dispatcher (DAG workflow)"}.</div>}
          {dagList.map(([wid, steps]) => {
            const st = wfStatus(steps);
            const failed = steps.some(s => ["failed", "disrupted", "cancelled"].includes(s.status));
            const gated = steps.filter(s => s.status === "awaiting_approval").length;
            const done = steps.filter(s => s.status === "completed").length;
            return (
              <div key={wid} className="card">
                <CardHead icon={<I.share size={14}/>} tone={tone(st)} title={wid}
                          meta={<span>{done}/{steps.length} done</span>}>
                  <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
                    <Pill tone={tone(st)} dot>{st === "awaiting_approval" ? "awaiting approval" : st}</Pill>
                    {gated > 0 && <button className="btn accent sm" title="Release the steps waiting for your approval and continue to the next level"
                                       onClick={() => act(`Step approved — ${gated} step${gated > 1 ? "s" : ""} released`, () => api.post(`/local/workflows/${encodeURIComponent(wid)}/approve_step`), true)}>
                      <I.check size={12}/> Approve &amp; continue ({gated})</button>}
                    {failed && <button className="btn ghost sm" title="Re-queue failed steps and continue this workflow"
                                       onClick={() => act("Workflow resumed", () => api.post(`/local/workflows/${encodeURIComponent(wid)}/resume`))}>
                      <I.refresh size={12}/> Resume DAG</button>}
                  </span>
                </CardHead>
                <table className="t">
                  <tbody>{steps.sort((a, b) => a.id.localeCompare(b.id)).map(taskRow)}</tbody>
                </table>
              </div>
            );
          })}
        </div>
      )}

      {view === "services" && (
        <div className="card">
          <CardHead icon={<I.box size={14}/>} tone="emerald" title="Services in use"
                    meta={<span>{services.length}</span>}/>
          {services.length === 0 && <div className="dim" style={{ padding: 18, fontSize: 12 }}>No services running or used yet.</div>}
          {services.length > 0 && (
            <table className="t">
              <thead><tr><th>Service</th><th>Kind</th><th>Status</th><th>Endpoint</th><th style={{ textAlign: "right" }}>Logs</th></tr></thead>
              <tbody>
                {services.map(s => (
                  <React.Fragment key={s.task_id}>
                    <tr>
                      <td className="mono name" style={{ fontSize: 12, cursor: "pointer" }} onClick={() => setDetail(s.task_id)}>{s.task_id}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{s.service_kind || s.image || "—"}</td>
                      <td><Pill tone={s.status === "active" ? "emerald" : tone(s.raw_status)} dot>{s.status}</Pill></td>
                      <td className="mono dim" style={{ fontSize: 11, wordBreak: "break-all" }}>{(s.ports || []).join(", ") || s.connection_string || "—"}</td>
                      <td style={{ textAlign: "right" }}>
                        <button className="btn ghost sm" onClick={() => setDetail(s.task_id)}>Logs</button>
                      </td>
                    </tr>
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {(view === "dispatch" || view === "dag") && <ResultArtifacts/>}

      {(() => {
        if (!detail) return null;
        // Prefer the full task row; fall back to a service (separate endpoint, may
        // not be in `rows`) normalised to the shape TaskDetail expects.
        let t = rows.find(x => x.id === detail);
        if (!t) {
          const svc = services.find(s => s.task_id === detail);
          if (svc) t = { id: svc.task_id, display_id: svc.task_id,
                         status: svc.status === "active" ? "serving" : (svc.raw_status || "serving"), logs: "" };
        }
        if (!t) return null;
        return (
          <TaskDetail task={t} live={liveByTid[detail]} name={name} onClose={() => setDetail(null)}
                      actions={actionsFor(t).filter(a => a.key !== "log" && a.key !== "why")}/>
        );
      })()}
    </>
  );
};

export { TelemetryScreen };
