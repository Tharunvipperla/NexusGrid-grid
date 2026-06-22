/* Live Topology — the interaction graph. The local node sits at the center;
 * trusted peers (circles) and full groups (rounded tiles) orbit it. An edge
 * shows direction: an arrow toward an entity means "we use them" (tasks we
 * dispatched, services we hold, deposits we store there); an arrow toward
 * the center means "they use us". Active traffic animates the edge.
 *
 * Clicking a group node drills INTO the group: the group becomes the center
 * and its members orbit it, marked online/paired, with the same usage
 * arrows for members we actually exchange with. A peer who is both a direct
 * friend and a group member is one identity — it shows once per view (as a
 * peer at the top level, as a member inside the group).
 *
 * Scale: the ring shows the most relevant nodes (active > online > rest)
 * and folds the remainder into a "+N more" chip, so the screen stays
 * readable no matter how many peers or groups the node knows. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, Bar, Avatar, Verified } from "../components.jsx";

const PEER_COLORS = ["#60a5fa", "#a78bfa", "#22d3ee", "#f472b6", "#fbbf24", "#34d399", "#c084fc"];
// Only genuinely in-flight work animates (flowing dashes). A held grant or a
// deposit that's merely stored is a relationship (static line), not live use.
const LIVE_STATES = ["processing", "serving", "transferring"];
const TERMINAL_TASK = ["completed", "failed", "cancelled", "disrupted"];
const TERMINAL_STORAGE = ["withdrawn", "deleted", "expired", "evicted", "completed", "failed"];
const MAX_RING = 2000;          // hard cap on nodes drawn at once (rest via search / roster)
const PRESENCE_WINDOW_MS = 150000;
// How many nodes to draw — configured in interface settings.
const topoMaxNodes = () => {
  try { const n = Number(JSON.parse(localStorage.getItem("nexus-ui-settings") || "{}").topoMaxNodes); return n >= 10 && n <= MAX_RING ? n : 100; }
  catch (_) { return 100; }
};

const short = (s) => (s && s.length > 14 ? s.slice(0, 12) + "…" : s || "");
const gpuPct = (gs) => {
  if (!gs || typeof gs !== "object") return null;
  const v = gs.utilization ?? gs.util ?? gs.gpu_util ?? gs.load;
  return typeof v === "number" ? v : null;
};
const fmtBytes = (n) => {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
};

/* Build the interaction model: for every peer/group, what we use of theirs
 * and what they use of ours, from tasks + service grants + storage rows. */
function buildGraph({ net, peers, grants, myDeposits, hosted, groups }) {
  const lw = (net && net.local_worker) || {};
  const tasks = (net && net.tasks) || {};
  const workers = (net && net.workers) || {};
  const names = (net && net.peer_names) || {};

  const entities = new Map(); // id -> entity
  const aliasTo = new Map();  // any alias -> entity id

  peers.forEach((p, i) => {
    const id = p.peer_uuid || p.internal_ip || p.ip;
    const e = {
      id, type: "peer",
      name: p.display_name || names[id] || p.ip || id,
      color: PEER_COLORS[i % PEER_COLORS.length],
      online: false, cpu: null, ram: null, gpu: null,
      weUse: [], theyUse: [],
    };
    entities.set(id, e);
    for (const a of [p.peer_uuid, p.internal_ip, p.ip, p.resolved_ip, p.display_name]) {
      if (a) aliasTo.set(a, id);
    }
  });

  // live hardware from the workers map
  for (const [wid, w] of Object.entries(workers)) {
    const id = aliasTo.get(wid) || aliasTo.get((w || {}).display_ip) || null;
    const e = id && entities.get(id);
    if (e) { e.online = !!w.online; e.cpu = w.cpu; e.ram = w.ram; e.gpu = gpuPct(w.gpu_stats); }
  }

  const me = new Set([lw.node_identity, "me"].filter(Boolean));
  const findEntity = (key) => {
    if (!key || me.has(key)) return null;
    const id = aliasTo.get(key);
    return id ? entities.get(id) : null;
  };

  // full groups as first-class nodes (chat groups live in Messages, not here)
  groups.forEach((g) => {
    entities.set(g.id, {
      id: g.id, type: "group", name: g.name || short(g.id),
      color: "#a78bfa", online: true,
      weUse: [], theyUse: [],
    });
  });

  // tasks → directional edges (skip terminal — they're history, not a live link)
  for (const [tid, t] of Object.entries(tasks)) {
    if (TERMINAL_TASK.includes(t.status)) continue;
    const label = t.display_id || tid;
    const active = LIVE_STATES.includes(t.status);
    const w = findEntity(t.worker);
    if (w) w.weUse.push({ kind: "task", label: `runs ${label}`, status: t.status, active });
    const r = findEntity(t.requested_by);
    if (r) r.theyUse.push({ kind: "task", label: `asked us to run ${label}`, status: t.status, active });
    for (const gid of (t.target_groups || [])) {
      const g = entities.get(gid);
      if (g) g.weUse.push({ kind: "task", label: `group task ${label}`, status: t.status, active });
    }
  }

  // service grants → a held grant is a standing relationship (static line); we
  // have no "actively streaming now" signal, so it never animates.
  for (const g of grants.held || []) {
    if (!(g.status === "approved" || g.status === "active")) continue;
    const e = findEntity(g.provider_uuid) || findEntity(g.provider_name);
    if (e) e.weUse.push({ kind: "service", label: `service “${g.service_name}”`, active: false });
  }
  for (const g of grants.issued || []) {
    if (!(g.status === "approved" || g.status === "active")) continue;
    const e = findEntity(g.consumer_uuid) || findEntity(g.grantee_name) || findEntity(g.grantee_pubkey);
    if (e) e.theyUse.push({ kind: "service", label: `uses our “${g.service_name}”`, active: false });
  }

  // storage → a stored deposit is a relationship; only a live transfer animates
  for (const d of myDeposits) {
    if (TERMINAL_STORAGE.includes(d.status)) continue;
    const e = findEntity(d.host_uuid) || findEntity(d.host_display_name);
    if (e) e.weUse.push({ kind: "storage", label: `stores our “${d.filename || d.deposit_id}” (${d.status})`, active: d.status === "transferring" });
  }
  for (const d of hosted) {
    if (TERMINAL_STORAGE.includes(d.status)) continue;
    const e = findEntity(d.depositor_uuid) || findEntity(d.depositor_display_name);
    if (e) e.theyUse.push({ kind: "storage", label: `their “${d.filename || d.deposit_id}” on our disk (${d.status})`, active: d.status === "transferring" });
  }

  const list = [...entities.values()];
  for (const e of list) {
    e.activity = e.weUse.filter(x => x.active).length + e.theyUse.filter(x => x.active).length;
  }
  return {
    meName: (lw.user_display_name || "").trim() || lw.node_identity || "this node",
    entities: list,
    aliasTo,
  };
}

const KIND_ICON = { task: I.cpu, service: I.terminal, storage: I.hdd };

const FlowList = ({ title, items, tone }) => (
  <div style={{ marginTop: 12 }}>
    <div className="label" style={{ marginBottom: 6 }}>{title}</div>
    {items.length === 0 && <div className="hint">nothing right now</div>}
    {items.map((it, i) => {
      const Ico = KIND_ICON[it.kind] || I.dot3;
      return (
        <div key={i} className="row" style={{ gap: 8, alignItems: "center", padding: "4px 0" }}>
          <span className={"ico-tile " + tone} style={{ width: 22, height: 22 }}><Ico size={12}/></span>
          <span style={{ fontSize: 12.5 }}>{it.label}</span>
          {it.active && <Pill tone="cyan" dot>live</Pill>}
        </div>
      );
    })}
  </div>
);

/* Lazy group breakdown: members + verified pool numbers. */
const GroupDetail = ({ gid }) => {
  const [detail, setDetail] = React.useState(null);
  const [pool, setPool] = React.useState(null);
  React.useEffect(() => {
    let dead = false;
    api.get(`/local/groups/${encodeURIComponent(gid)}`).then(d => !dead && setDetail(d)).catch(() => {});
    api.get(`/local/groups/${encodeURIComponent(gid)}/pool_stats`).then(d => !dead && setPool(d)).catch(() => {});
    return () => { dead = true; };
  }, [gid]);
  if (!detail) return <div className="hint" style={{ marginTop: 12 }}>Loading group…</div>;
  const stats = new Map(((pool && pool.members) || []).map(m => [m.pubkey, m]));
  return (
    <div style={{ marginTop: 12 }}>
      <div className="label" style={{ marginBottom: 6 }}>Members & pool usage<Verified/></div>
      {(detail.members || []).map((m, i) => {
        const st = stats.get(m.pubkey) || {};
        return (
          <div key={i} className="row" style={{ gap: 8, alignItems: "center", padding: "4px 0" }}>
            <Avatar name={m.display_name || m.pubkey} seed={m.pubkey} color="#a78bfa" size={22}/>
            <span style={{ fontSize: 12.5, flex: 1 }}>
              {m.display_name || short(m.pubkey)}{m.pubkey === detail.my_pubkey ? " (you)" : ""}
            </span>
            <span className="hint mono">↗ {st.tasks_contributed ?? 0} · ↙ {st.tasks_consumed ?? 0}</span>
          </div>
        );
      })}
      <div className="hint" style={{ marginTop: 6 }}>↗ tasks contributed · ↙ tasks consumed</div>
      <div className="hint" style={{ marginTop: 6 }}>{(detail.relays || []).length} relay{(detail.relays || []).length === 1 ? "" : "s"} bound{(detail.relays || []).some(r => r.content_share) ? " · one is content-readable" : " · all E2E-blind"}</div>
    </div>
  );
};

/* Geometry shared by both views. Nodes fan out onto concentric rings — each
 * outer ring holds more — so the layout stays readable as the count grows and
 * you zoom/pan to reach the outer rings. Beyond what's rendered, search +
 * filters + the "all" roster cover an effectively unbounded mesh. */
const W = 760, H = 460, CX = W / 2, CY = H / 2;
const ringCap = (k) => 8 + k * 8;             // ring 0:8, 1:16, 2:24, …
const ringPos = (i) => {
  let idx = i, k = 0;
  while (idx >= ringCap(k)) { idx -= ringCap(k); k++; }
  const cap = ringCap(k);
  const a = (idx / cap) * Math.PI * 2 - Math.PI / 2 + (k % 2 ? Math.PI / cap : 0);
  const r = 96 + k * 56;
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
};
/* Trim a center→node segment so it starts/ends at the circle borders
 * instead of running through the shapes. */
const clip = (x1, y1, x2, y2, r1, r2) => {
  const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  return [x1 + ux * r1, y1 + uy * r1, x2 - ux * r2, y2 - uy * r2];
};
const chevron = (x1, y1, x2, y2, t, color) => {
  const px = x1 + (x2 - x1) * t, py = y1 + (y2 - y1) * t;
  const ang = Math.atan2(y2 - y1, x2 - x1);
  const s = 4;
  const a1 = ang + Math.PI * 0.82, a2 = ang - Math.PI * 0.82;
  return <path d={`M ${px + s * Math.cos(a1)} ${py + s * Math.sin(a1)} L ${px} ${py} L ${px + s * Math.cos(a2)} ${py + s * Math.sin(a2)}`}
               fill="none" stroke={color} strokeWidth="1.8" strokeLinecap="round"/>;
};
const NODE_FILL = "var(--bg-card, #15171c)";

/* Zoom + pan shared by both graph views: the wheel zooms around the cursor,
 * dragging pans, the buttons/reset return to the full view. The wheel
 * listener is attached natively (non-passive) so preventDefault works. */
const useZoomPan = () => {
  const base = React.useRef({ x: 0, y: 0, w: W, h: H });
  const vbRef = React.useRef(base.current);   // current viewBox (authoritative)
  const svgRef = React.useRef(null);
  const holder = React.useRef(null);
  const [, force] = React.useState(0);        // bump to re-render (zoom/reset/drag-end)

  const applyDOM = () => {
    const v = vbRef.current;
    if (svgRef.current) svgRef.current.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  };
  // Commit a new viewBox: update the DOM + trigger a React re-render.
  const setVb = (next) => {
    vbRef.current = typeof next === "function" ? next(vbRef.current) : next;
    applyDOM();
    force(n => n + 1);
  };
  const fit = React.useCallback((radius) => {
    const w = Math.max(W, (radius + 50) * 2);
    const h = w * (H / W);
    const v = { x: CX - w / 2, y: CY - h / 2, w, h };
    base.current = v;
    setVb(v);
  }, []);
  const zoomBy = React.useCallback((factor, px = 0.5, py = 0.5) => {
    setVb(v => {
      const w = Math.min(base.current.w * 4, Math.max(base.current.w / 8, v.w * factor));
      const h = w * (H / W);
      return { x: v.x + (v.w - w) * px, y: v.y + (v.h - h) * py, w, h };
    });
  }, []);
  React.useEffect(() => {
    const el = holder.current;
    if (!el) return;
    // Plain wheel scrolls the page; Ctrl/⌘ + wheel zooms around the cursor.
    const onWheel = (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      zoomBy(e.deltaY > 0 ? 1.18 : 1 / 1.18, (e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomBy]);
  // Drag-to-pan with the left OR right mouse button. The viewBox is updated
  // straight on the DOM during the drag (no React re-render → no rebuilding the
  // graph/dashboard each frame = smooth); state syncs once on release. A 4px
  // threshold keeps a plain tap selecting a node.
  const onMouseDown = (e) => {
    e.preventDefault();   // also stops the browser's native image-drag ("phantom graph")
    const startX = e.clientX, startY = e.clientY;
    const startVb = { ...vbRef.current };
    let panning = false;
    const move = (ev) => {
      if (!panning && Math.hypot(ev.clientX - startX, ev.clientY - startY) < 4) return;
      panning = true;
      const el = svgRef.current || holder.current;
      const r = el ? el.getBoundingClientRect() : { width: W, height: H };
      const scale = Math.min(r.width / startVb.w, r.height / startVb.h) || 1;
      vbRef.current = {
        ...startVb,
        x: startVb.x - (ev.clientX - startX) / scale,
        y: startVb.y - (ev.clientY - startY) / scale,
      };
      applyDOM();   // DOM only — buttery
    };
    const up = () => {
      if (panning) force(n => n + 1);   // commit to React state once
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  };
  const reset = React.useCallback(() => setVb(base.current), []);
  const v = vbRef.current;
  const zoomed = v.w !== base.current.w || v.x !== base.current.x || v.y !== base.current.y;
  const svgProps = {
    viewBox: `${v.x} ${v.y} ${v.w} ${v.h}`,
    ref: svgRef,
    onMouseDown,
    draggable: false,
    onDragStart: (e) => e.preventDefault(),
    onContextMenu: (e) => e.preventDefault(),    // right-button drag without the menu popping
    style: { width: "100%", height: "100%", display: "block", touchAction: "none", userSelect: "none", cursor: "grab" },
  };
  return { svgProps, holder, zoomBy, reset, fit, zoomed };
};

// Outer radius the rings reach for a given node count (mirrors ringCap/ringPos).
const fitRadius = (count) => {
  let k = 0, c = 0;
  while (c < count && k < 40) { c += ringCap(k); k++; }
  return 96 + Math.max(0, k - 1) * 56 + 36;
};

/* Full-page node detail (opened by clicking a peer) — live hardware, the
 * services/tasks/storage flowing each way right now, and the verified
 * exchange totals + reliability with this peer. Mirrors the Services detail
 * layout (content + metadata sidebar). */
const Stat = ({ label, value, verified, tone }) => (
  <div className="topo-stat">
    <div className="topo-stat-k">{label}{verified && <Verified/>}</div>
    <div className="topo-stat-v" style={tone ? { color: tone } : undefined}>{value}</div>
  </div>
);

const NodeDetailPage = ({ entity, onBack }) => {
  const [profile, setProfile] = React.useState(null);
  React.useEffect(() => {
    let dead = false;
    if (entity.id) api.get(`/local/peers/${encodeURIComponent(entity.id)}/profile`).then(d => !dead && setProfile(d)).catch(() => {});
    return () => { dead = true; };
  }, [entity.id]);
  const ex = (profile && profile.exchange_with_you) || {};
  const rel = (profile && profile.reliability_with_you) || {};
  const we = entity.weUse.length > 0, they = entity.theyUse.length > 0;
  const cnt = (arr, kind) => arr.filter(x => x.kind === kind).length;
  const pct = (v) => (v == null ? "—" : Math.round(v) + "%");
  return (
    <div className="svc-detail">
      <div className="svc-bc">
        <span className="svc-bc-link" onClick={onBack}>Topology</span>
        <span className="svc-bc-sep">/</span><span className="svc-bc-cur">{entity.name}</span>
      </div>
      <div className="svc-grid">
        <div className="svc-main">
          <h1 className="svc-title">{entity.name}</h1>
          <div className="row" style={{ gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <Pill tone={entity.online ? "emerald" : "ghost"} dot>{entity.online ? "online" : "offline"}</Pill>
            {we && they ? <Pill tone="purple">mutual</Pill> : we ? <Pill tone="cyan">you use theirs</Pill> : they ? <Pill tone="amber">they use yours</Pill> : null}
          </div>

          <div className="node-bars" style={{ gap: 8, marginTop: 18, maxWidth: 440 }}>
            <div className="node-bar-row"><span style={{ width: 34 }}>CPU</span><Bar value={entity.cpu || 0} threshold/><span className="mono">{pct(entity.cpu)}</span></div>
            <div className="node-bar-row"><span style={{ width: 34 }}>RAM</span><Bar value={entity.ram || 0} threshold/><span className="mono">{pct(entity.ram)}</span></div>
            <div className="node-bar-row"><span style={{ width: 34 }}>GPU</span><Bar value={entity.gpu || 0} threshold/><span className="mono">{pct(entity.gpu)}</span></div>
          </div>

          <div className="svc-sec">
            <h3>Exchange<Verified/></h3>
            <div className="topo-stats">
              <Stat label="Compute received" value={(ex.they_gave_compute_secs || 0) + "s"}/>
              <Stat label="Compute given" value={(ex.you_gave_compute_secs || 0) + "s"}/>
              <Stat label="They host for you" value={fmtBytes(ex.they_hosted_bytes)}/>
              <Stat label="You host for them" value={fmtBytes(ex.you_hosted_bytes)}/>
              <Stat label="Task success" value={pct(rel.success_rate)} tone={rel.success_rate != null && rel.success_rate < 80 ? "var(--rose, #fb7185)" : undefined}/>
              <Stat label="Tasks ok / failed" value={(rel.ok || 0) + " / " + (rel.failed || 0)}/>
            </div>
          </div>

          <div className="svc-sec">
            <h3>Active now</h3>
            <div className="topo-stats">
              <Stat label="Live flows" value={entity.activity}/>
              <Stat label="Services you hold" value={cnt(entity.weUse, "service")}/>
              <Stat label="Tasks on their node" value={cnt(entity.weUse, "task")}/>
              <Stat label="Deposits you store there" value={cnt(entity.weUse, "storage")}/>
              <Stat label="Their tasks on you" value={cnt(entity.theyUse, "task")}/>
              <Stat label="Their deposits on you" value={cnt(entity.theyUse, "storage")}/>
            </div>
          </div>

          <FlowList title="What you use of theirs →" items={entity.weUse} tone="cyan"/>
          <FlowList title="← What they use of yours" items={entity.theyUse} tone="amber"/>
        </div>
        <aside className="svc-side">
          <div className="svc-meta">
            <div className="svc-meta-k">Status</div>
            <div className="svc-meta-v">{entity.online ? "online" : "offline"}</div>
          </div>
          <div className="svc-meta">
            <div className="svc-meta-k">Relationship</div>
            <div className="svc-meta-v">{we && they ? "mutual use" : we ? "you use theirs" : they ? "they use yours" : "connected · idle"}</div>
          </div>
          <div className="svc-meta">
            <div className="svc-meta-k">Node id</div>
            <div className="svc-meta-v mono" style={{ fontSize: 11, wordBreak: "break-all" }}>{entity.id}</div>
          </div>
          {!profile && entity.id && <div className="hint">Loading verified exchange…</div>}
        </aside>
      </div>
    </div>
  );
};

/* Hover legend — a real styled popover (the native title tooltip was flaky and
 * couldn't show colour swatches). */
const HelpTip = () => {
  const [open, setOpen] = React.useState(false);
  return (
    <span className="topo-help" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button className="icon-btn" aria-label="Legend"><I.help size={15}/></button>
      {open && (
        <div className="topo-help-pop">
          <div className="topo-help-row"><i style={{ background: "#22d3ee" }}/> cyan — you use theirs</div>
          <div className="topo-help-row"><i style={{ background: "#fbbf24" }}/> amber — they use yours</div>
          <div className="topo-help-row"><i style={{ background: "#a78bfa" }}/> purple — mutual / group</div>
          <hr className="divider" style={{ margin: "7px 0" }}/>
          <div className="topo-help-row"><i style={{ background: "#34d399", borderRadius: "50%", width: 8, height: 8 }}/> green border = online · grey = offline</div>
          <div className="topo-help-row">animated dashes = live compute right now</div>
          <div className="topo-help-row">squares are groups — click one to open</div>
          <div className="topo-help-row">drag to pan (left or right button)</div>
          <div className="topo-help-row">Ctrl + scroll to zoom</div>
        </div>
      )}
    </span>
  );
};

const fmtAgoTs = (ms) => {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};
const durStr = (s) => { s = Math.round(Number(s) || 0); if (s < 60) return s + "s"; const m = Math.floor(s / 60); return m < 60 ? m + "m" : Math.floor(m / 60) + "h"; };

/* Operational dashboard shown below the graph when nothing is selected:
 * high value (now running, connectivity, recent activity), medium (top
 * partners, storage, reliability), lower (needs attention). */
const TopoDash = ({ net, relay, tunnel, audit, ents, myDeposits, hosted, setSel }) => {
  const lw = net.local_worker || {};
  const metrics = net.metrics || {};
  const names = net.peer_names || {};
  const tasks = net.tasks || {};
  const LIVE = ["processing", "serving", "transferring"];
  const TERM = ["withdrawn", "deleted", "expired", "evicted", "completed", "failed"];
  const running = Object.entries(tasks).filter(([, t]) => LIVE.includes(t.status)).slice(0, 5);
  const partners = ents.filter(e => e.type === "peer" && (e.weUse.length + e.theyUse.length) > 0)
    .sort((a, b) => (b.weUse.length + b.theyUse.length) - (a.weUse.length + a.theyUse.length)).slice(0, 4);
  const activeDeps = myDeposits.filter(d => !TERM.includes(d.status));
  const activeHost = hosted.filter(d => !TERM.includes(d.status));
  const near = [...activeDeps, ...activeHost].filter(d => {
    let exp = 0;
    if (d.ttl_at) exp = Date.parse(d.ttl_at);
    else if (d.created_at && d.ttl_days) exp = Date.parse(d.created_at) + d.ttl_days * 86400000;
    return exp && exp - Date.now() < 2 * 86400000;
  }).length;
  const completed = metrics.tasks_completed || 0, failed = metrics.tasks_failed || 0;
  const succ = (completed + failed) ? Math.round(completed / (completed + failed) * 100) : null;
  const publicUrl = tunnel.public_url || tunnel.url || "";
  const offline = ents.filter(e => e.type === "peer" && !e.online);
  const alerts = net.alerts || [];

  const Sub = ({ title }) => <div className="topo-sub">{title}</div>;
  const Row = ({ k, v, vTone, dot, onClick, ks }) => (
    <div className="topo-dash-row" style={onClick ? { cursor: "pointer" } : undefined} onClick={onClick}>
      {dot !== undefined && <span className="topo-dash-dot" style={{ background: dot }}/>}
      <span className="topo-dash-name" style={ks}>{k}</span>
      {v != null && <span className="hint mono" style={vTone ? { color: vTone } : undefined}>{v}</span>}
    </div>
  );

  return (
    <div className="topo-dash">
      {/* Live activity */}
      <div className="topo-dash-card">
        <div className="topo-dash-h">Live activity</div>
        <Sub title={"Now running" + (running.length ? " · " + running.length : "")}/>
        {running.length === 0 ? <div className="hint">Idle — no live tasks.</div>
          : running.map(([tid, t]) => (
            <Row key={tid} dot="#22d3ee" k={t.display_id || tid}
                 v={(names[t.worker] || t.worker || "—") + (t.elapsed_secs != null ? " · " + durStr(t.elapsed_secs) : "")}/>
          ))}
        <Sub title="Recent"/>
        {audit.length === 0 ? <div className="hint">Nothing yet.</div>
          : audit.slice(0, 5).map((e, i) => (
            <Row key={i} k={e.action} ks={{ fontSize: 11 }} v={fmtAgoTs((e.ts || 0) * 1000)}/>
          ))}
      </div>

      {/* This node */}
      <div className="topo-dash-card">
        <div className="topo-dash-h">This node</div>
        <Sub title="Connectivity"/>
        <Row k="Relay" v={relay.running ? (relay.lan_only ? "running · LAN-only" : "running") : "stopped"}/>
        <Row k="Reachability" v={publicUrl ? "public" : "LAN-only"}/>
        <Row k="Coordinators" v={(lw.connected_master_count || 0) + " linked"}/>
        <Sub title="Reliability"/>
        <Row k="Task success" v={succ != null ? succ + "%" : "—"} vTone={succ != null && succ < 80 ? "var(--rose, #fb7185)" : undefined}/>
        <Row k="Completed / failed" v={completed + " / " + failed}/>
        <Sub title="Storage"/>
        <Row k="Hosting for peers" v={activeHost.length}/>
        <Row k="Placed on peers" v={activeDeps.length}/>
        {near > 0 && <Row k="Nearing TTL" v={near} vTone="var(--rose, #fb7185)"/>}
      </div>

      {/* Partners & attention */}
      <div className="topo-dash-card">
        <div className="topo-dash-h">Partners &amp; attention</div>
        <Sub title="Top partners"/>
        {partners.length === 0 ? <div className="hint">No active exchanges.</div>
          : partners.map(p => (
            <div key={p.id} className="topo-dash-row" style={{ cursor: "pointer" }} onClick={() => setSel(p)}>
              <Avatar name={p.name} seed={p.id} color={p.color} size={18}/>
              <span className="topo-dash-name">{p.name}</span>
              <span className="hint mono">{p.weUse.length}↗ {p.theyUse.length}↙</span>
            </div>
          ))}
        <Sub title={"Needs attention" + (offline.length + alerts.length ? " · " + (offline.length + alerts.length) : "")}/>
        {offline.length === 0 && alerts.length === 0 ? <div className="hint">All clear.</div>
          : <>
              {offline.slice(0, 4).map(p => (
                <Row key={p.id} dot="var(--t-faint)" k={p.name} v="offline" onClick={() => setSel(p)}/>
              ))}
              {alerts.slice(0, 3).map((a, i) => (
                <div key={"a" + i} className="topo-dash-row"><I.alertT size={12} style={{ color: "var(--amber, #fbbf24)", flexShrink: 0 }}/><span className="topo-dash-name" style={{ fontSize: 11 }}>{typeof a === "string" ? a : (a.message || a.text || "alert")}</span></div>
              ))}
            </>}
      </div>
    </div>
  );
};

const TopologyScreen = () => {
  const [net, setNet] = React.useState({});
  const [peers, setPeers] = React.useState([]);
  const [grants, setGrants] = React.useState({ held: [], issued: [] });
  const [myDeposits, setMyDeposits] = React.useState([]);
  const [hosted, setHosted] = React.useState([]);
  const [groups, setGroups] = React.useState([]);
  const [relay, setRelay] = React.useState({});
  const [tunnel, setTunnel] = React.useState({});
  const [audit, setAudit] = React.useState([]);
  const [sel, setSel] = React.useState(null);
  const [drill, setDrill] = React.useState(null);          // group id when inside a group
  const [drillDetail, setDrillDetail] = React.useState(null);
  const [drillPool, setDrillPool] = React.useState(null);  // verified pool stats for the drilled group
  const [memberSel, setMemberSel] = React.useState(null);  // member clicked inside a drilled group
  const [memberProfile, setMemberProfile] = React.useState(null);
  const [moreOpen, setMoreOpen] = React.useState(false);   // roster of ring-hidden entries
  const [viewNode, setViewNode] = React.useState(null);    // peer entity → full-page detail
  const [q, setQ] = React.useState("");
  const [filter, setFilter] = React.useState("all");
  const [maxNodes, setMaxNodes] = React.useState(topoMaxNodes);
  React.useEffect(() => {
    const on = () => setMaxNodes(topoMaxNodes());
    window.addEventListener("nexus-ui-settings-changed", on);
    return () => window.removeEventListener("nexus-ui-settings-changed", on);
  }, []);
  const zp = useZoomPan();

  const load = React.useCallback(async () => {
    const [n, p, g, md, h, gr, r, t, au] = await Promise.all([
      api.get("/local/network").catch(() => ({})),
      api.get("/local/peers").catch(() => ({})),
      api.get("/local/service_grants").catch(() => ({})),
      api.get("/local/foreign_storage/my_deposits").catch(() => ({})),
      api.get("/local/foreign_storage/hosted").catch(() => ({})),
      api.get("/local/groups").catch(() => ({})),
      api.get("/local/relay/status").catch(() => ({})),
      api.get("/local/relay/tunnel/status").catch(() => ({})),
      api.get("/local/audit?limit=20").catch(() => ({})),
    ]);
    setNet(n || {});
    setPeers(((p && p.peers) || []).filter(x => (x.status || "").startsWith("trusted")));
    setGrants({ held: (g && g.held) || [], issued: (g && g.issued) || [] });
    setMyDeposits((md && md.deposits) || []);
    setHosted((h && h.deposits) || []);
    setRelay(r || {});
    setTunnel(t || {});
    setAudit((au && au.events) || []);
    // Chat groups are conversations, not infrastructure — they live in
    // Messages and never appear on the topology.
    setGroups(((gr && gr.groups) || []).filter(x => (x.kind || "full") !== "chat"));
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  // Drilled group: fetch the roster (members + presence ride the detail) and
  // the receipt-verified pool numbers for the member panel.
  React.useEffect(() => {
    setDrillDetail(null);
    setDrillPool(null);
    setMemberSel(null);
    setMoreOpen(false);
    if (!drill) return;
    let dead = false;
    api.get(`/local/groups/${encodeURIComponent(drill)}`).then(d => !dead && setDrillDetail(d)).catch(() => {});
    api.get(`/local/groups/${encodeURIComponent(drill)}/pool_stats`).then(d => !dead && setDrillPool(d)).catch(() => {});
    return () => { dead = true; };
  }, [drill]);

  // Clicked member: pull their profile for the direct-exchange numbers
  // (counterparty-signed receipts + reliability), when we know their node id.
  React.useEffect(() => {
    setMemberProfile(null);
    if (!memberSel || !memberSel.nodeId || memberSel.isMe) return;
    let dead = false;
    api.get(`/local/peers/${encodeURIComponent(memberSel.nodeId)}/profile`)
      .then(d => !dead && setMemberProfile(d)).catch(() => {});
    return () => { dead = true; };
  }, [memberSel]);

  const model = buildGraph({ net, peers, grants, myDeposits, hosted, groups });
  const ents = model.entities;
  // Resolve a group member to a direct-peer entity on any stable key — node_id
  // can be empty (e.g. the founder's record), but peer_address ↔ resolved_ip and
  // the display name are in the alias map too.
  const memberPeer = (m) => {
    const id = (m.node_id && model.aliasTo.get(m.node_id))
            || (m.peer_address && model.aliasTo.get(m.peer_address))
            || (m.display_name && model.aliasTo.get(m.display_name));
    return id ? ents.find(e => e.id === id) : null;
  };

  // Search + filter narrow the mesh so it scales to very large rosters: type to
  // find a node, or filter by presence / direction / kind.
  const ql = q.trim().toLowerCase();
  const filtered = ents.filter(e => {
    if (ql && !String(e.name).toLowerCase().includes(ql)) return false;
    if (filter === "active") return e.activity > 0;         // ongoing tasks/flows only
    if (filter === "online") return e.online;
    if (filter === "groups") return e.type === "group";
    if (filter === "using") return e.weUse.length > 0;     // you use theirs
    if (filter === "usedby") return e.theyUse.length > 0;   // they use yours
    return true;
  });
  // Scale guard: rank by relevance (live traffic, then online, then named),
  // draw at most the user-chosen count on the rings, fold the rest into a chip.
  const ranked = [...filtered].sort((a, b) =>
    (b.activity - a.activity) || ((b.online ? 1 : 0) - (a.online ? 1 : 0)) || String(a.name).localeCompare(String(b.name)));
  const shown = ranked.slice(0, maxNodes);
  const hidden = ranked.length - shown.length;

  // Fit the view to the chosen budget when the user changes it (or drills),
  // so picking a big count actually frames all the nodes.
  React.useEffect(() => {
    zp.fit(fitRadius(drill ? 80 : Math.max(8, Math.min(ranked.length, maxNodes))));
  }, [maxNodes, drill]); // eslint-disable-line react-hooks/exhaustive-deps

  // A flow only counts as live if the node is reachable — an offline peer with
  // a stale "active" task shouldn't render as a live blue flow.
  const liveOf = (e) => (e.online || e.type === "group") ? e.activity : 0;
  // Edge/node colour encodes who-uses-whom: we use theirs (cyan), they use
  // ours (amber), mutual (purple). null = no interaction (no line drawn).
  const dirColor = (e) => {
    const we = e.weUse.length > 0, they = e.theyUse.length > 0;
    return we && they ? "#a78bfa" : we ? "#22d3ee" : they ? "#fbbf24" : null;
  };

  const drillGroup = drill ? ents.find(e => e.id === drill) : null;

  /* ── group drill-in view: the group is the center, members orbit ── */
  const renderGroupView = () => {
    const members = (drillDetail && drillDetail.members) || [];
    const myPubkey = drillDetail && drillDetail.my_pubkey;
    const rows = members.map((m, i) => {
      const peer = memberPeer(m);
      const seen = m.last_seen_at ? Date.parse(m.last_seen_at) : NaN;
      return {
        key: m.pubkey || i,
        pubkey: m.pubkey,
        nodeId: m.node_id || null,
        name: m.display_name || short(m.pubkey),
        isMe: m.pubkey === myPubkey,
        peer,                                  // same identity as a direct friend
        online: (m.pubkey === myPubkey) || (peer ? peer.online : (!isNaN(seen) && Date.now() - seen < PRESENCE_WINDOW_MS)),
      };
    });
    const shownRows = rows.slice(0, maxNodes);
    return (
      <svg {...zp.svgProps}>
        {/* member edges — same language as the top level: direction colour for
          * direct exchange, faint membership line otherwise, animate only when live */}
        {shownRows.map((m, i) => {
          const [x, y] = ringPos(i);
          const we = m.peer ? m.peer.weUse.length > 0 : false;
          const they = m.peer ? m.peer.theyUse.length > 0 : false;
          const membershipOnly = !we && !they;
          const active = m.peer ? (m.peer.activity > 0 && m.online) : false;
          const color = we && they ? "#a78bfa" : we ? "#22d3ee" : they ? "#fbbf24" : "#a78bfa";
          const [ex1, ey1, ex2, ey2] = clip(CX, CY, x, y, 26, 17);
          return (
            <g key={"l" + m.key}>
              <line x1={ex1} y1={ey1} x2={ex2} y2={ey2} stroke={color}
                    strokeWidth={active ? 1.6 : 1.1}
                    strokeDasharray={active ? "6 5" : membershipOnly ? "1 5" : "none"}
                    opacity={membershipOnly ? 0.5 : 1}>
                {active && <animate attributeName="stroke-dashoffset" values="22;0" dur="1.4s" repeatCount="indefinite"/>}
              </line>
              {we && chevron(ex1, ey1, ex2, ey2, 0.62, color)}
              {they && chevron(ex2, ey2, ex1, ey1, 0.62, color)}
            </g>
          );
        })}
        {/* the group at the center */}
        <g style={{ cursor: "pointer" }} onClick={() => { setDrill(null); setSel(null); }}>
          <rect x={CX - 24} y={CY - 24} width={48} height={48} rx={12} fill={NODE_FILL} stroke="#a78bfa" strokeWidth="1.4"/>
          <text x={CX} y={CY + 3.5} textAnchor="middle" fontSize="10" fontFamily="JetBrains Mono" fill="#c4b5fd">
            {(drillGroup && drillGroup.name || "").slice(0, 7) || "group"}
          </text>
          <text x={CX} y={CY + 40} textAnchor="middle" fontSize="9.5" fill="var(--t-mute)">
            {members.length} member{members.length === 1 ? "" : "s"}
          </text>
        </g>
        {/* members — node border = presence (green online / grey offline / indigo you),
          * matching the top level. Direct peers open the same full-page detail. */}
        {shownRows.map((m, i) => {
          const [x, y] = ringPos(i);
          const ring = m.isMe ? "#6366f1" : m.online ? "#34d399" : "var(--t-faint)";
          return (
            <g key={m.key} opacity={m.online ? 1 : 0.5} style={{ cursor: m.isMe ? "default" : "pointer" }}
               onClick={() => { if (m.isMe) return; if (m.peer) { setMemberSel(null); setViewNode(m.peer); } else { setSel(null); setMemberSel(memberSel && memberSel.key === m.key ? null : m); } }}>
              <circle cx={x} cy={y} r={13} fill={NODE_FILL} stroke={ring} strokeWidth="1.25"/>
              <text x={x} y={y + 3.5} textAnchor="middle" fontSize="9" fontFamily="JetBrains Mono" fill={ring}>
                {(m.name || "?").charAt(0).toUpperCase()}
              </text>
              <text x={x} y={y + 27} textAnchor="middle" fontSize="9" fill="var(--t-dim)">
                {m.isMe ? "you" : (m.name || "").slice(0, 14)}
              </text>
              {m.peer && !m.isMe && (
                <text x={x} y={y - 19} textAnchor="middle" fontSize="7.5" fill="var(--t-faint)">paired</text>
              )}
            </g>
          );
        })}
        {rows.length > shownRows.length && (
          <text x={W - 12} y={H - 12} textAnchor="end" fontSize="9" fill="var(--cyan, #22d3ee)"
                style={{ cursor: "pointer", textDecoration: "underline" }}
                onClick={() => { setSel(null); setMemberSel(null); setMoreOpen(true); }}>
            +{rows.length - shownRows.length} more members — view all
          </text>
        )}
      </svg>
    );
  };

  /* ── top-level grid view: YOU at the center, peers + groups orbit ── */
  const renderGridView = () => (
    <svg {...zp.svgProps}>
      {/* edges first (under the nodes), clipped at the circle borders */}
      {shown.map((e, i) => {
        const [x, y] = ringPos(i);
        const weUse = e.weUse.length > 0, theyUse = e.theyUse.length > 0;
        const isGroup = e.type === "group";
        const membershipOnly = isGroup && !weUse && !theyUse;
        // Peers: a line only when interacting. Groups: always linked (a faint
        // membership line), upgrading to a coloured/animated flow when active.
        if (!weUse && !theyUse && !isGroup) return null;
        const active = liveOf(e) > 0;
        const color = membershipOnly ? "#a78bfa" : (dirColor(e) || "#a78bfa");
        const [ex1, ey1, ex2, ey2] = clip(CX, CY, x, y, isGroup ? 20 : 24, 17);
        return (
          <g key={"l" + e.id}>
            <line x1={ex1} y1={ey1} x2={ex2} y2={ey2} stroke={color}
                  strokeWidth={active ? Math.min(3.2, 1.2 + e.activity * 0.5) : 1.1}
                  strokeDasharray={active ? "6 5" : membershipOnly ? "1 5" : "none"}
                  opacity={membershipOnly ? 0.5 : 1}>
              {active && <animate attributeName="stroke-dashoffset" values="22;0" dur="1.4s" repeatCount="indefinite"/>}
            </line>
            {weUse && chevron(ex1, ey1, ex2, ey2, 0.62, color)}
            {theyUse && chevron(ex2, ey2, ex1, ey1, 0.62, color)}
          </g>
        );
      })}
      {/* center node */}
      <g style={{ cursor: "pointer" }} onClick={() => setSel(null)}>
        <circle cx={CX} cy={CY} r={22} fill={NODE_FILL} stroke="#6366f1" strokeWidth="1.4"/>
        <text x={CX} y={CY + 3.5} textAnchor="middle" fontSize="9" fontFamily="JetBrains Mono" fill="#c7d2fe">YOU</text>
        <text x={CX} y={CY + 38} textAnchor="middle" fontSize="9.5" fill="var(--t-mute)">{(model.meName || "").slice(0, 20)}</text>
      </g>
      {/* entities */}
      {shown.map((e, i) => {
        const [x, y] = ringPos(i);
        // Node border = presence: green when online, grey when offline (groups
        // are infrastructure → purple). Direction lives on the edge colour.
        const ring = e.type === "group" ? "#a78bfa" : e.online ? "#34d399" : "var(--t-faint)";
        const selRing = sel === e.id;
        return (
          <g key={e.id} opacity={e.type === "peer" && !e.online ? 0.45 : 1}
             style={{ cursor: "pointer" }}
             onClick={() => e.type === "group"
               ? (setDrill(e.id), setSel(null))
               : setViewNode(e)}>
            {selRing && (e.type === "group"
              ? <rect x={x - 18} y={y - 18} width={36} height={36} rx={10} fill="none" stroke="#6366f1" strokeWidth="1.5"/>
              : <circle cx={x} cy={y} r={18} fill="none" stroke="#6366f1" strokeWidth="1.5"/>)}
            {e.type === "group"
              ? <rect x={x - 14} y={y - 14} width={28} height={28} rx={8} fill={NODE_FILL} stroke={ring} strokeWidth="1.3"/>
              : <circle cx={x} cy={y} r={13} fill={NODE_FILL} stroke={ring} strokeWidth="1.25"/>}
            <text x={x} y={y + 3.5} textAnchor="middle" fontSize="9" fontFamily="JetBrains Mono" fill={ring}>
              {(e.name || "?").charAt(0).toUpperCase()}
            </text>
            <text x={x} y={y + 28} textAnchor="middle" fontSize="9" fill="var(--t-dim)">
              {(e.name || "").slice(0, 14)}
            </text>
            {liveOf(e) > 0 && (
              <circle cx={x} cy={y} r={17} fill="none" stroke="#22d3ee" strokeWidth="0.75" opacity="0.45">
                <animate attributeName="r" values="15;21" dur="1.8s" repeatCount="indefinite"/>
                <animate attributeName="opacity" values="0.45;0" dur="1.8s" repeatCount="indefinite"/>
              </circle>
            )}
          </g>
        );
      })}
      {hidden > 0 && (
        <text x={W - 12} y={H - 12} textAnchor="end" fontSize="9" fill="var(--cyan, #22d3ee)"
              style={{ cursor: "pointer", textDecoration: "underline" }}
              onClick={() => { setSel(null); setMoreOpen(true); }}>
          +{hidden} more (idle) — view all
        </text>
      )}
    </svg>
  );

  if (viewNode) return <NodeDetailPage entity={viewNode} onBack={() => setViewNode(null)}/>;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Live topology</div>
          <div className="page-sub">
            {drill
              ? <>Inside the group — members, their presence, and what flows between you. </>
              : <>Who's using what, in both directions. Click a group to step inside it. </>}
          </div>
        </div>
        <div className="page-tools">
          {!drill && (
            <>
              <div className="api-search">
                <I.search size={13}/>
                <input placeholder="Search nodes…" value={q} onChange={e => setQ(e.target.value)} style={{ width: 140 }}/>
              </div>
              <div className="seg">
                {[["all", "All"], ["active", "Active"], ["online", "Online"], ["using", "You use"], ["usedby", "Use yours"], ["groups", "Groups"]].map(([f, l]) => (
                  <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>{l}</button>
                ))}
              </div>
            </>
          )}
          <HelpTip/>
          {drill && <button className="btn ghost" onClick={() => { setDrill(null); setSel(null); }}><I.arrUR size={14} style={{ transform: "rotate(225deg)" }}/> All connections</button>}
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
        </div>
      </div>

      <div className="split-2" style={{ alignItems: "flex-start", gap: 14, marginBottom: 16, gridTemplateColumns: (memberSel || moreOpen || drill) ? "1fr 340px" : "1fr" }}>
        <div className="card" style={{ padding: 6, position: "relative", overflow: "hidden", height: "62vh", minHeight: 380 }} ref={zp.holder}>
          {ents.length === 0 ? (
            <div className="dim" style={{ padding: 24, fontSize: 12 }}>
              No trusted peers or groups yet — pair in <strong>Network</strong> or create a group, and they'll appear here.
            </div>
          ) : drill ? (
            drillDetail ? renderGroupView() : <div className="dim" style={{ padding: 24, fontSize: 12 }}>Loading group…</div>
          ) : renderGridView()}
          {zp.zoomed && <button className="topo-reset btn ghost sm" onClick={zp.reset}>Reset view</button>}
        </div>

        {ents.length > 0 && !memberSel && !moreOpen && drill && (
          <div className="card pad-lg" style={{ position: "sticky", top: 14 }}>
            <div className="hint">Click a member to see your exchange, or <strong>All connections</strong> to step back out.</div>
          </div>
        )}

        {memberSel && (() => {
          const st = ((drillPool && drillPool.members) || []).find(x => x.pubkey === memberSel.pubkey) || {};
          const ex = (memberProfile && memberProfile.exchange_with_you) || null;
          const rel = (memberProfile && memberProfile.reliability_with_you) || {};
          return (
            <div className="card pad-lg" style={{ position: "sticky", top: 14 }}>
              <div className="row" style={{ gap: 10, alignItems: "center" }}>
                <Avatar name={memberSel.name} seed={memberSel.pubkey} color="#a78bfa" size={32}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 700, fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{memberSel.name}</div>
                  <div className="hint">group member · {memberSel.online ? "online" : "offline"}{memberSel.peer ? " · also your direct peer" : ""}</div>
                </div>
                <button className="btn ghost sm" onClick={() => setMemberSel(null)}><I.x size={13}/></button>
              </div>

              <div style={{ marginTop: 12 }}>
                <div className="label" style={{ marginBottom: 6 }}>In this group<Verified/></div>
                <div className="mono" style={{ fontSize: 11 }}>
                  contributed {st.tasks_contributed ?? 0} task{(st.tasks_contributed ?? 0) === 1 ? "" : "s"} · consumed {st.tasks_consumed ?? 0}
                </div>
              </div>

              {memberSel.peer && (
                <>
                  <FlowList title="What we use of theirs →" items={memberSel.peer.weUse} tone="cyan"/>
                  <FlowList title="← What they use of ours" items={memberSel.peer.theyUse} tone="amber"/>
                </>
              )}

              {ex && (
                <div style={{ marginTop: 12 }}>
                  <div className="label" style={{ marginBottom: 6 }}>Between you two<Verified/></div>
                  <div className="mono" style={{ fontSize: 11 }}>
                    they gave you {ex.they_gave_compute_secs || 0}s compute · you gave {ex.you_gave_compute_secs || 0}s
                    <br/>they host {fmtBytes(ex.they_hosted_bytes)} of yours · you host {fmtBytes(ex.you_hosted_bytes)} of theirs
                    {rel.success_rate != null && <><br/>reliability on your tasks: {rel.success_rate}% ({rel.ok || 0} ok / {rel.failed || 0} failed)</>}
                  </div>
                </div>
              )}
              {!ex && memberSel.nodeId && <div className="hint" style={{ marginTop: 12 }}>Loading direct exchange…</div>}
              {!memberSel.nodeId && !memberSel.peer && (
                <div className="hint" style={{ marginTop: 12 }}>Not a direct peer — only the group-pool numbers are available. Pair with them in <strong>Network</strong> to see your one-to-one exchange.</div>
              )}
            </div>
          );
        })()}

        {/* Full roster behind the "+N more" chip: everything the ring hides,
          * clickable through to the same detail panels. */}
        {!memberSel && moreOpen && (
          <div className="card pad-lg" style={{ position: "sticky", top: 14 }}>
            <div className="row" style={{ alignItems: "center", marginBottom: 8 }}>
              <div className="grow" style={{ fontWeight: 600, fontSize: 13.5 }}>
                {drill ? `All members (${((drillDetail && drillDetail.members) || []).length})` : `All connections (${ents.length})`}
              </div>
              <button className="btn ghost sm" onClick={() => setMoreOpen(false)}><I.x size={13}/></button>
            </div>
            <div style={{ maxHeight: 420, overflowY: "auto" }}>
              {drill
                ? ((drillDetail && drillDetail.members) || []).map((m, i) => {
                    const peer = memberPeer(m);
                    const isMe = m.pubkey === (drillDetail && drillDetail.my_pubkey);
                    const row = { key: m.pubkey || i, pubkey: m.pubkey, nodeId: m.node_id || null,
                                  name: m.display_name || short(m.pubkey), isMe, peer,
                                  online: isMe || (peer ? peer.online : false) };
                    return (
                      <div key={row.key} className="row" style={{ gap: 8, padding: "5px 0", cursor: isMe ? "default" : "pointer" }}
                           onClick={() => { if (!isMe) { setMoreOpen(false); if (row.peer) setViewNode(row.peer); else setMemberSel(row); } }}>
                        <Avatar name={row.name} seed={m.pubkey} color="#a78bfa" size={20}/>
                        <span style={{ fontSize: 12.5, flex: 1 }}>{row.name}{isMe ? " (you)" : ""}</span>
                        {row.peer && <span className="hint">paired</span>}
                      </div>
                    );
                  })
                : ranked.map(e => (
                    <div key={e.id} className="row" style={{ gap: 8, padding: "5px 0", cursor: "pointer" }}
                         onClick={() => { setMoreOpen(false); if (e.type === "group") { setDrill(e.id); } else { setSel(e.id); } }}>
                      <Avatar name={e.name} seed={e.id} color={e.type === "group" ? "#a78bfa" : e.color} size={20}/>
                      <span style={{ fontSize: 12.5, flex: 1 }}>{e.name}</span>
                      {e.activity > 0 && <Pill tone="cyan" dot>{e.activity} live</Pill>}
                      <span className="hint">{e.type === "group" ? "group" : e.online ? "online" : "offline"}</span>
                    </div>
                  ))}
            </div>
          </div>
        )}
      </div>

      {ents.length > 0 && !memberSel && !moreOpen && !drill && (
        <div style={{ marginBottom: 24 }}>
          <TopoDash net={net} relay={relay} tunnel={tunnel} audit={audit} ents={ents} myDeposits={myDeposits} hosted={hosted} setSel={setViewNode}/>
        </div>
      )}
    </>
  );
};

export { TopologyScreen };
