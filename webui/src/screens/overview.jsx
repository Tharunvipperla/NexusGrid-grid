/* Overview — the landing screen, wired to live data.
 *
 * Props (from App):
 *   node    {name, addr, online, cpu, ram, gpu}
 *   peers   [{name, addr, online, cpu, ram, color}]
 *   metrics {queue_depth, active_workers, tasks_dispatched, tasks_completed, tasks_failed, ...}
 *   alerts  [string | {message,...}]
 *   relay   {running, suggested_url, lan_only}
 *   gdrive  bool   (Drive configured?)
 *   loading bool   (first /local/network fetch still in flight)
 *   setRoute fn
 */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import JSZip from "jszip";
import { Kpi, CardHead, Spark, Skel, Chk, Verified } from "../components.jsx";
import { toast } from "../toast.jsx";

const FIRSTRUN_KEY = "nexus-firstrun-dismissed";

/* One-click guided first dispatch: a built-in hello-world zipped in the
 * browser and sent through the normal /local/add_workflow path, then the
 * user is dropped on the task's telemetry to watch the full loop
 * (dispatch → run → result). Nothing to type, nothing to download. */
const runHelloWorld = async (setRoute) => {
  const zip = new JSZip();
  zip.file("main.py",
    'print("Hello from NexusGrid!")\n' +
    'print("This task was zipped in your browser, dispatched to the grid,")\n' +
    'print("and its result was uploaded back to your node.")\n');
  const blob = await zip.generateAsync({ type: "blob" });
  const wid = "hello_world_" + Date.now().toString(36);
  const fd = new FormData();
  fd.append("workflow_id", wid);
  fd.append("workflow_json", JSON.stringify([{
    id: "hello", runtime: "docker", image: "python:3.11-slim",
    entrypoint: "python main.py", ram_limit: 256, cpu_limit: 50,
  }]));
  fd.append("file", new File([blob], "hello.zip", { type: "application/zip" }));
  await api.post("/local/add_workflow", fd);
  toast("Hello-world dispatched — watch it run");
  setRoute("telemetry", wid + "_hello");
};

/* Getting-started checklist for a fresh node: each step is computed from
 * live data, so it ticks itself off as the user actually does things. */
const FirstRun = ({ node, peers, metrics, setRoute, onDismiss }) => {
  const [helloBusy, setHelloBusy] = React.useState(false);
  const hello = async (e) => {
    e.stopPropagation();
    setHelloBusy(true);
    try { await runHelloWorld(setRoute); }
    catch (err) { toast("Hello-world failed: " + (err.detail || err.message), "danger"); }
    finally { setHelloBusy(false); }
  };
  const steps = [
    { done: !!node.name && node.name !== node.addr && node.name !== "this node",
      title: "Name your node", sub: "so peers see a name, not a key", route: "config" },
    { done: peers.length > 0,
      title: "Connect a peer or join a group", sub: "pair on the LAN or redeem an invite link", route: "network" },
    { done: (metrics.tasks_dispatched ?? 0) > 0 || (metrics.tasks_completed ?? 0) > 0,
      title: "Dispatch your first task", sub: "any folder with an entrypoint works", route: "dispatcher",
      extra: (
        <button className="btn ghost sm" disabled={helloBusy} onClick={hello}
                title="Dispatch a built-in hello-world and watch the full loop — nothing to prepare">
          <I.zap size={12}/> {helloBusy ? "Dispatching…" : "Try a hello-world"}
        </button>
      ) },
  ];
  if (steps.every(s => s.done)) return null;
  return (
    <div className="card pad-lg" style={{ marginBottom: 16, borderColor: "var(--accent)" }}>
      <div className="row" style={{ marginBottom: 10 }}>
        <div className="ico-tile cyan" style={{ width: 28, height: 28 }}><I.zap size={14}/></div>
        <div className="grow" style={{ fontWeight: 600, fontSize: 13.5 }}>Getting started</div>
        <button className="icon-btn" title="Dismiss" onClick={onDismiss}><I.x size={13}/></button>
      </div>
      {steps.map((s, i) => (
        <div key={i} className="row" style={{ gap: 10, padding: "7px 0", cursor: s.done ? "default" : "pointer", opacity: s.done ? 0.55 : 1 }}
             onClick={() => !s.done && setRoute(s.route)}>
          <Chk on={s.done}/>
          <span style={{ fontSize: 13, textDecoration: s.done ? "line-through" : "none" }}>{s.title}</span>
          <span className="hint">{s.sub}</span>
          {!s.done && s.extra}
          {!s.done && <I.arr size={13} style={{ marginLeft: "auto", color: "var(--t-dim)" }}/>}
        </div>
      ))}
    </div>
  );
};

const SkeletonOverview = () => (
  <>
    <div className="page-head">
      <div>
        <div className="page-title">Cluster overview</div>
        <div className="page-sub"><Skel w={220} h={11}/></div>
      </div>
    </div>
    <div className="kpi-row">
      {[0, 1, 2, 3, 4].map(i => (
        <div key={i} className="kpi"><Skel w={70} h={10} style={{ marginBottom: 10 }}/><Skel w={48} h={22}/></div>
      ))}
    </div>
    <div className="split-2">
      <div className="card pad-lg">
        {[0, 1, 2].map(i => (
          <div key={i} className="row" style={{ gap: 10, padding: "10px 0" }}>
            <Skel w={28} h={28} r={"50%"}/><Skel w={"55%"} h={12}/>
          </div>
        ))}
      </div>
      <div className="card pad-lg">
        <Skel w={"40%"} h={12} style={{ marginBottom: 14 }}/>
        <Skel w={"85%"} h={11} style={{ marginBottom: 8 }}/>
        <Skel w={"70%"} h={11}/>
      </div>
    </div>
  </>
);

const fmtSecs = (v) => {
  v = Math.round(Number(v) || 0);
  if (v < 90) return v + "s";
  if (v < 5400) return Math.round(v / 60) + "m";
  return (v / 3600).toFixed(1) + "h";
};

/* ── insight charts ────────────────────────────────────────────────
 * Rolling client-side samples (module-level: navigating away and back
 * keeps this session's history). One sample per network poll, capped. */
const SAMPLES = { t: [], cpu: [], ram: [], gpu: [], up: [], down: [], done: [], failed: [] };
const pushSample = (lw, metrics) => {
  const now = Date.now();
  if (SAMPLES.t.length && now - SAMPLES.t[SAMPLES.t.length - 1] < 4000) return;
  const io = lw.net_io || {};
  const gs = lw.gpu_stats || {};
  const gpu = gs.utilization ?? gs.util ?? gs.gpu_util ?? gs.load;
  SAMPLES.t.push(now);
  SAMPLES.cpu.push(Math.round(Number(lw.cpu) || 0));
  SAMPLES.ram.push(Math.round(Number(lw.ram) || 0));
  SAMPLES.gpu.push(typeof gpu === "number" ? Math.round(gpu) : 0);
  SAMPLES.up.push(Math.round(Number(io.sent_per_sec) || 0));
  SAMPLES.down.push(Math.round(Number(io.recv_per_sec) || 0));
  SAMPLES.done.push(Number(metrics.tasks_completed) || 0);
  SAMPLES.failed.push(Number(metrics.tasks_failed) || 0);
  for (const k of Object.keys(SAMPLES)) if (SAMPLES[k].length > 480) SAMPLES[k].shift();
};

const fmtRate = (v) => {
  v = Number(v) || 0;
  if (v < 1024) return Math.round(v) + " B/s";
  if (v < 1048576) return (v / 1024).toFixed(1) + " KB/s";
  return (v / 1048576).toFixed(1) + " MB/s";
};

const LineChart = ({ series, h = 92, fmt = (v) => String(Math.round(v)), bars = false }) => {
  const W = 600;
  const all = series.flatMap(s => s.data);
  if (all.length < 2) return <div className="hint" style={{ padding: "18px 0" }}>Collecting samples…</div>;
  const max = Math.max(...all, 1);
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${h}`} style={{ width: "100%", display: "block" }}>
        {[0.25, 0.5, 0.75].map(f => <line key={f} x1={0} y1={h * f} x2={W} y2={h * f} stroke="var(--br-mute)" strokeWidth="1"/>)}
        {bars
          ? series.map((s, si) => {
              const n = s.data.length;
              const bw = Math.max(1, (W / n) / series.length - 0.6);
              return s.data.map((v, i) => (
                <rect key={s.label + i}
                      x={(i / n) * W + si * bw} width={bw}
                      y={h - (v / max) * (h - 8) - 2} height={Math.max(1, (v / max) * (h - 8))}
                      fill={s.color} opacity="0.85"/>
              ));
            })
          : series.map(s => {
              const n = s.data.length;
              if (n < 2) return null;
              const pts = s.data.map((v, i) => `${((i / (n - 1)) * W).toFixed(1)},${(h - (v / max) * (h - 8) - 4).toFixed(1)}`).join(" ");
              return <polyline key={s.label} points={pts} fill="none" stroke={s.color} strokeWidth="1.6" strokeLinejoin="round"/>;
            })}
      </svg>
      <div className="row" style={{ gap: 14, marginTop: 6, flexWrap: "wrap" }}>
        {series.map(s => (
          <span key={s.label} className="hint">
            <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: s.color, marginRight: 5 }}/>
            {s.label}{s.data.length ? ` · ${fmt(s.data[s.data.length - 1])}` : ""}
          </span>
        ))}
        <span className="hint" style={{ marginLeft: "auto" }}>peak {fmt(max)}</span>
      </div>
    </div>
  );
};



const OverviewScreen = ({ node, peers, metrics, alerts, relay, gdrive, loading, setRoute, tasks = {}, lw = {}, peerNames = {} }) => {
  peers = peers || [];
  metrics = metrics || {};
  alerts = alerts || [];
  relay = relay || {};
  const [firstRunGone, setFirstRunGone] = React.useState(() => !!localStorage.getItem(FIRSTRUN_KEY));
  const [spark, setSpark] = React.useState(null); // global pool-usage buckets
  const [usage, setUsage] = React.useState(null); // receipt-verified totals
  // One rolling sample per data refresh feeds the insight charts.
  React.useEffect(() => { if (!loading) pushSample(lw, metrics); });

  React.useEffect(() => {
    let dead = false;
    const load = async () => {
      try {
        const d = await api.get("/local/pool_usage?range=24h");
        if (dead) return;
        const buckets = (d && d.buckets) || [];
        // rows are per member+hour: sum into one series per hour
        const byHour = {};
        for (const b of buckets) {
          const k = b.bucket_start || "";
          byHour[k] = byHour[k] || { c: 0, u: 0 };
          byHour[k].c += Number(b.compute_secs_contributed || 0);
          byHour[k].u += Number(b.compute_secs_consumed || 0);
        }
        const hours = Object.keys(byHour).sort();
        if (hours.length >= 2) {
          setSpark({
            contributed: hours.map(h => byHour[h].c),
            consumed: hours.map(h => byHour[h].u),
          });
        }
      } catch (_) {}
      try {
        const p = await api.get("/local/profile");
        if (!dead) setUsage((p && p.global_usage) || null);
      } catch (_) {}
    };
    load();
    const id = setInterval(load, 30000);
    return () => { dead = true; clearInterval(id); };
  }, []);

  if (loading) return <SkeletonOverview/>;

  // Live work, not lifetime counters: tasks in flight on the grid right now.
  const running = Object.entries(tasks).filter(([, t]) => ["processing", "serving"].includes(t.status));
  const liveLocal = (lw.active_tasks || []);
  const balance = usage ? (usage.compute_secs_contributed || 0) - (usage.compute_secs_consumed || 0) : 0;

  const onlinePeers = peers.filter(p => p.online);

  return (
  <>
    <div className="page-head">
      <div>
        <div className="page-title">Cluster overview</div>
      </div>
      <div className="page-tools">
        <button className="btn ghost" onClick={() => setRoute("dispatcher")}><I.zap size={14}/> Dispatch a task</button>
      </div>
    </div>

    {!firstRunGone && (
      <FirstRun node={node} peers={peers} metrics={metrics} setRoute={setRoute}
                onDismiss={() => { localStorage.setItem(FIRSTRUN_KEY, "1"); setFirstRunGone(true); }}/>
    )}

    {/* The row answers "is my node healthy and pulling its weight" rather
      * than listing lifetime counters: live work, queue, fleet, and the
      * receipt-verified give/take balance. */}
    <div className="kpi-row">
      <Kpi label="Running now" value={String(running.length)}
           sub={running.length ? <span style={{ color: "var(--cyan)" }}>live on the grid</span> : <span>idle</span>}/>
      <Kpi label="Queue" value={String(metrics.queue_depth ?? 0)}
           sub={metrics.queue_depth > 0 ? <span>waiting for workers</span> : <span>clear</span>}/>
      <Kpi label="Peers online" value={`${onlinePeers.length} / ${peers.length}`}/>
      <Kpi label={<>Compute balance<Verified/></>} value={(balance >= 0 ? "+" : "−") + fmtSecs(Math.abs(balance))}
           trend={usage ? `↗ ${fmtSecs(usage.compute_secs_contributed)} · ↙ ${fmtSecs(usage.compute_secs_consumed)}` : undefined}
           trendDir={balance >= 0 ? "up" : "down"}
           sub={spark ? <Spark data={spark.contributed} color="var(--emerald, #34d399)" w={90} h={20}/> : <span>verified compute balance</span>}/>
      <Kpi label="Hosting for peers" value={usage ? Math.round((usage.storage_bytes_hosted || 0) / 1048576) + " MB" : "—"}
           sub={<span>{usage ? `helped ${usage.peers_helped || 0} peer${(usage.peers_helped || 0) === 1 ? "" : "s"}` : ""}</span>}/>
    </div>

    {(running.length > 0 || liveLocal.length > 0) && (
      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.pulse size={14}/>} tone="cyan" title="Happening now"
                  meta={<span>{running.length + liveLocal.length} live</span>}/>
        {liveLocal.map((t, i) => (
          <div key={"l" + i} className="rail-item" style={{ cursor: "pointer" }} onClick={() => setRoute("telemetry", t.task_id)}>
            <div className="rail-icon"><I.cpu size={14}/></div>
            <div className="rail-text">
              <span className="mono" style={{ fontSize: 12 }}>{t.task_id}</span>
              <div className="rail-sub">running on this node · {t.stage || "working"}</div>
            </div>
          </div>
        ))}
        {running.filter(([id]) => !liveLocal.some(t => t.task_id === id)).map(([id, t]) => (
          <div key={id} className="rail-item" style={{ cursor: "pointer" }} onClick={() => setRoute("telemetry", id)}>
            <div className="rail-icon"><I.zap size={14}/></div>
            <div className="rail-text">
              <span className="mono" style={{ fontSize: 12 }}>{t.display_id || id}</span>
              <div className="rail-sub">{t.worker ? `on ${peerNames[t.worker] || t.worker}` : "routing"} · {t.status}</div>
            </div>
          </div>
        ))}
      </div>
    )}


    {/* Insight charts — always shown, no selection or export. */}
    <div className="card" style={{ marginTop: 16 }}>
      <CardHead icon={<I.pulse size={14}/>} tone="cyan" title="Insights"/>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, padding: 18 }}>
        <div>
          <div className="label" style={{ marginBottom: 4 }}>Resources</div>
          <LineChart series={[{ label: "CPU", color: "var(--cyan)", data: SAMPLES.cpu },
                               { label: "RAM", color: "var(--purple)", data: SAMPLES.ram },
                               { label: "GPU", color: "var(--amber)", data: SAMPLES.gpu }]}
                     fmt={(v) => Math.round(v) + "%"}/>
        </div>
        <div>
          <div className="label" style={{ marginBottom: 4 }}>Network bandwidth</div>
          <LineChart series={[{ label: "up", color: "var(--emerald)", data: SAMPLES.up },
                               { label: "down", color: "var(--blue)", data: SAMPLES.down }]}
                     fmt={fmtRate}/>
        </div>
        <div>
          <div className="label" style={{ marginBottom: 4 }}>Task outcomes</div>
          <LineChart series={[{ label: "completed", color: "var(--emerald)", data: SAMPLES.done },
                               { label: "failed", color: "var(--rose)", data: SAMPLES.failed }]}/>
        </div>
        <div>
          <div className="label" style={{ marginBottom: 4 }}>Pool compute — hourly<Verified/></div>
          {spark
            ? <LineChart series={[{ label: "contributed", color: "var(--emerald)", data: spark.contributed },
                                  { label: "consumed", color: "var(--cyan)", data: spark.consumed }]}
                         fmt={fmtSecs}/>
            : <div className="hint" style={{ padding: "18px 0" }}>No pool activity recorded yet.</div>}
        </div>
      </div>
    </div>
  </>
  );
};

export { OverviewScreen };
