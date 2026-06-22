/* Shared atoms used across screens */
import React from "react";
import { I } from "./icons.jsx";

const Avatar = ({ name, color = "#10b981", size = 32, seed }) => {
  // With a stable key (pubkey/uuid) render an identicon instead of a letter:
  // the picture is derived from the key itself, so it can't be impersonated
  // by picking the same display name.
  if (seed) return <Identicon seed={seed} size={size}/>;
  const letter = (name || "?").charAt(0).toUpperCase();
  // Subtle: tinted background + colored letter (Linear-style)
  const bg = `color-mix(in oklab, ${color} 18%, var(--bg-card))`;
  const br = `color-mix(in oklab, ${color} 35%, transparent)`;
  return (
    <div className={"avatar" + (size <= 24 ? " sm" : "")}
         style={{ background: bg, color: color, border: `1px solid ${br}`, width: size, height: size, fontSize: size <= 24 ? 10 : 12 }}>
      {letter}
    </div>
  );
};

const Pill = ({ tone = "ghost", dot = false, children }) => (
  <span className={"pill " + tone}>
    {children}
  </span>
);

const Trend = ({ direction = "up", value }) => {
  const cls = direction === "up" ? "delta up" : direction === "down" ? "delta down" : "delta flat";
  const arrow = direction === "up" ? "↗" : direction === "down" ? "↘" : "—";
  return <span className={cls}>{arrow} {value}</span>;
};

const Kpi = ({ icon, tone, label, value, trend, trendDir = "up", sub }) => {
  const arrow = trendDir === "up" ? "↗" : trendDir === "down" ? "↘" : "—";
  return (
    <div className="kpi">
      <div className="kpi-head">
        <span>{label}</span>
      </div>
      <div className="kpi-value">{value}</div>
      <div className="kpi-trend">
        {trend && <span className={"delta-chip " + trendDir}>{arrow} {trend}</span>}
        {sub && <span>{sub}</span>}
      </div>
    </div>
  );
};

const CardHead = ({ icon, tone, title, meta, children }) => (
  <div className="card-head">
    {icon && <div className={"ico-tile " + (tone || "solid")} style={{ width: 28, height: 28 }}>{icon}</div>}
    <h3>{title}</h3>
    {meta && <div className="head-meta">{meta}</div>}
    {children}
  </div>
);

const Toggle = ({ on, onChange }) => (
  <div className={"tgl " + (on ? "on" : "")} onClick={() => onChange && onChange(!on)} />
);

const Radio = ({ on }) => <div className={"radio " + (on ? "on" : "")} />;

const RadioTile = ({ on, title, sub, onClick }) => (
  <div className={"radio-tile " + (on ? "on" : "")} onClick={onClick}>
    <Radio on={on} />
    <div>
      <div className="rt-title">{title}</div>
      {sub && <div className="rt-sub">{sub}</div>}
    </div>
  </div>
);

/* Hover tooltip for jargon — a small "?" that explains the concept in plain
 * words. Pure CSS (data-tip), keyboard-focusable. */
const Help = ({ text }) => <span className="help" data-tip={text} tabIndex={0}>?</span>;

const Field = ({ label, hint, help, children }) => (
  <div className="field">
    {label && <label className="label">{label}{help && <Help text={help}/>}</label>}
    {children}
    {hint && <span className="hint">{hint}</span>}
  </div>
);

const Bar = ({ value, color, lg = false, threshold = false }) => {
  // threshold mode: pick class based on value (assumed 0-100, where higher = more loaded)
  let cls = "";
  if (threshold) {
    if (value >= 95)      cls = "crit";
    else if (value >= 88) cls = "warn";
    else                  cls = "ok";
  }
  return (
    <div className={"bar " + (lg ? "lg " : "") + cls}>
      <div style={{ width: value + "%", background: cls ? undefined : color }} />
    </div>
  );
};

const Chk = ({ on, onChange }) => (
  <div className={"chk " + (on ? "on" : "")} onClick={() => onChange && onChange(!on)} />
);

/* Collapsible "Advanced" section for forms: the 90% path stays short and
 * the expert knobs sit one click away. Open/closed is remembered per id so
 * power users aren't punished with a click every time. */
const Disclosure = ({ id, label = "Advanced options", children }) => {
  const key = "nexus-disclosure:" + id;
  const [open, setOpen] = React.useState(() => localStorage.getItem(key) === "1");
  const toggle = () => {
    const next = !open;
    setOpen(next);
    try { localStorage.setItem(key, next ? "1" : "0"); } catch (_) {}
  };
  return (
    <div style={{ marginTop: 14 }}>
      <div className="row" style={{ gap: 6, cursor: "pointer", userSelect: "none" }} onClick={toggle}>
        <I.arr size={12} style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform 0.12s", color: "var(--t-mute)" }}/>
        <span className="label" style={{ cursor: "pointer" }}>{label}</span>
        {!open && <span className="hint">blank fields use this node's defaults</span>}
      </div>
      {open && <div style={{ marginTop: 4 }}>{children}</div>}
    </div>
  );
};

/* Counterparty-signed marker: sits next to ANY number derived from usage
 * receipts so "verified" reads as one consistent visual everywhere. */
const Verified = ({ text }) => (
  <span className="verified" tabIndex={0}
        data-tip={text || "Counterparty-signed receipts — the other side signed these numbers, so a node can't edit its own record."}>
    <I.shield size={9}/> verified
  </span>
);

/* Deterministic identicon from a pubkey/uuid: a 5×5 mirrored grid whose
 * pattern and hue are derived from the seed, so an unnamed node is still
 * visually distinct and the picture itself is tied to the key. */
const Identicon = ({ seed, size = 32, square = false }) => {
  const s = String(seed || "?");
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  let bits = h >>> 0;
  const next = () => { bits = (Math.imul(bits, 1103515245) + 12345) >>> 0; return bits; };
  const hue = (h >>> 0) % 360;
  const cells = [];
  for (let x = 0; x < 3; x++) for (let y = 0; y < 5; y++) {
    if (next() % 5 < 2) { cells.push([x, y]); if (x < 2) cells.push([4 - x, y]); }
  }
  return (
    <svg width={size} height={size} viewBox="-0.8 -0.8 6.6 6.6"
         style={{ borderRadius: square ? 6 : "50%", background: `hsl(${hue} 30% 17%)`, flexShrink: 0 }}>
      {cells.map(([x, y], i) => <rect key={i} x={x} y={y} width={1.04} height={1.04} fill={`hsl(${hue} 65% 62%)`}/>)}
    </svg>
  );
};

/* Shimmer placeholder shown while a screen's first fetch is in flight. */
const Skel = ({ w = "100%", h = 12, r = 6, style }) => (
  <span className="skel" style={{ width: w, height: h, borderRadius: r, ...style }}/>
);

/* Layered DAG renderer for workflow manifests: nodes are placed in
 * dependency-depth columns, edges drawn as curves. Status tints each node
 * when a statusOf(id) resolver is supplied. */
const DagGraph = ({ nodes, statusOf, height = 220 }) => {
  if (!Array.isArray(nodes) || !nodes.length) return null;
  // Scale guard: past this the drawing stops being information.
  if (nodes.length > 150) {
    return <div className="hint" style={{ padding: "10px 0" }}>
      Workflow has {nodes.length} steps — too many to draw usefully; the step list and telemetry still cover it.
    </div>;
  }
  const byId = {};
  for (const n of nodes) byId[n.id] = n;
  const depth = {};
  const depthOf = (id, seen) => {
    if (depth[id] != null) return depth[id];
    if (!seen) seen = new Set();
    if (seen.has(id)) return 0; // cycle guard
    seen.add(id);
    const deps = (byId[id] && byId[id].depends_on) || [];
    const d = deps.length ? 1 + Math.max(...deps.map(x => depthOf(x, seen))) : 0;
    depth[id] = d;
    return d;
  };
  nodes.forEach(n => depthOf(n.id));
  const cols = [];
  for (const n of nodes) (cols[depth[n.id]] = cols[depth[n.id]] || []).push(n);
  // The canvas grows with the graph (wide for deep chains, tall for big
  // fan-outs) and scrolls inside a capped viewport instead of squashing.
  const maxCol = Math.max(...cols.map(c => (c || []).length));
  const W = Math.max(420, cols.length * 160);
  const H = Math.max(height, maxCol * 40);
  const pos = {};
  cols.forEach((col, ci) => col.forEach((n, ri) => {
    pos[n.id] = { x: 70 + ci * ((W - 140) / Math.max(1, cols.length - 1) || 0), y: (H / (col.length + 1)) * (ri + 1) };
  }));
  const tone = (id) => {
    const st = statusOf ? statusOf(id) : "";
    return st === "completed" ? "var(--emerald, #34d399)"
         : st === "processing" ? "var(--cyan, #22d3ee)"
         : st === "failed" ? "var(--rose, #fb7185)"
         : "var(--t-dim)";
  };
  return (
    <div style={{ maxHeight: 380, overflow: "auto", border: nodes.length > 24 ? "1px solid var(--br-mute)" : "none", borderRadius: 8 }}>
    <svg width={nodes.length > 24 ? W : "100%"} height={nodes.length > 24 ? H : undefined}
         viewBox={`0 0 ${W} ${H}`} style={{ display: "block" }}>
      {nodes.flatMap(n => ((n.depends_on || []).map(d => {
        const a = pos[d], b = pos[n.id];
        if (!a || !b) return null;
        const mx = (a.x + b.x) / 2;
        return <path key={d + ">" + n.id} d={`M ${a.x + 46} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${b.x - 46} ${b.y}`}
                     fill="none" stroke="var(--br)" strokeWidth="1.4"/>;
      })))}
      {nodes.map(n => {
        const p = pos[n.id];
        return (
          <g key={n.id}>
            <rect x={p.x - 46} y={p.y - 16} width={92} height={32} rx={8}
                  fill="var(--bg-card, #15171c)" stroke={tone(n.id)} strokeWidth="1.4"/>
            <text x={p.x} y={p.y + 4} textAnchor="middle"
                  style={{ font: "10.5px var(--f-mono)", fill: "var(--t)" }}>
              {String(n.id).length > 12 ? String(n.id).slice(0, 11) + "…" : n.id}
            </text>
          </g>
        );
      })}
    </svg>
    </div>
  );
};

/* Sparkline with optional gradient fill underneath */
const Spark = ({ data, color = "var(--cyan)", w = 80, h = 22, fill = false }) => {
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 2) - 1;
    return [x, y];
  });
  const linePts = pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const gradId = `sg-${Math.random().toString(36).slice(2, 8)}`;
  const areaD = `M ${pts[0][0]} ${h} L ${pts.map(p => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" L ")} L ${pts[pts.length-1][0]} ${h} Z`;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      {fill && (
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"  stopColor={color} stopOpacity="0.35"/>
            <stop offset="100%" stopColor={color} stopOpacity="0"/>
          </linearGradient>
        </defs>
      )}
      {fill && <path d={areaD} fill={`url(#${gradId})`}/>}
      <polyline points={linePts} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
};

/* Bar + line chart (Sales-vs-Labor style) */
const MixChart = ({ data, height = 240 }) => {
  // data = [{label, bar1, bar2, line}]
  const max = Math.max(...data.flatMap(d => [d.bar1, d.bar2, d.line]));
  const W = 100; // viewBox units per slot
  const total = data.length * W;
  const H = 200;
  const PAD_T = 20; const PAD_B = 30;
  const inner = H - PAD_T - PAD_B;
  const scale = v => PAD_T + (1 - v / max) * inner;
  // line path with gradient fill
  const linePts = data.map((d, i) => `${i * W + W/2},${scale(d.line)}`);
  const linePath = "M " + linePts.join(" L ");
  const areaPath = linePath + ` L ${(data.length - 1) * W + W/2},${H - PAD_B} L ${W/2},${H - PAD_B} Z`;
  const barW = 14;
  const gridLines = [0, 0.25, 0.5, 0.75, 1].map(p => PAD_T + p * inner);

  return (
    <div style={{ width: "100%", height, overflow: "hidden" }}>
      <svg viewBox={`0 0 ${total} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: "100%" }}>
        <defs>
          <linearGradient id="lineFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f59e0b" stopOpacity="0.25"/>
            <stop offset="100%" stopColor="#f59e0b" stopOpacity="0"/>
          </linearGradient>
        </defs>
        {/* grid */}
        {gridLines.map((y, i) => (
          <line key={i} x1="0" y1={y} x2={total} y2={y} stroke="rgba(255,255,255,0.05)" strokeWidth="0.5"/>
        ))}
        {/* bars */}
        {data.map((d, i) => (
          <g key={i}>
            <rect x={i * W + W/2 - barW - 2} y={scale(d.bar1)} width={barW} height={H - PAD_B - scale(d.bar1)} fill="#a78bfa" rx="2"/>
            <rect x={i * W + W/2 + 2} y={scale(d.bar2)} width={barW} height={H - PAD_B - scale(d.bar2)} fill="#22d3ee" rx="2"/>
          </g>
        ))}
        {/* area */}
        <path d={areaPath} fill="url(#lineFill)"/>
        {/* line */}
        <path d={linePath} fill="none" stroke="#f59e0b" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
        {/* labels */}
        {data.map((d, i) => (
          <text key={i} x={i * W + W/2} y={H - 8} fontFamily="JetBrains Mono" fontSize="10" fill="#5e6470" textAnchor="middle">{d.label}</text>
        ))}
      </svg>
    </div>
  );
};

/* Centered overlay dialog (in-page React, no native popups). Backdrop click
 * and the ✕ both close; Escape too. */
const Modal = ({ title, icon, tone, onClose, width = 560, children, foot }) => {
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose && onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose && onClose(); }}>
      <div className="modal" style={{ width, maxWidth: "94vw" }}>
        <div className="modal-head">
          {icon && <span className={"ico-tile " + (tone || "solid")} style={{ width: 26, height: 26, marginRight: 10 }}>{icon}</span>}
          <h3>{title}</h3>
          <button className="btn ghost sm" onClick={onClose}><I.x size={14}/></button>
        </div>
        <div className="modal-body">{children}</div>
        {foot && <div className="modal-foot">{foot}</div>}
      </div>
    </div>
  );
};

/* Full-screen editor for large code/JSON — used anywhere a cramped textarea
 * isn't enough to see/modify the whole thing. JSON gets a Format button and
 * parse-validation on save. Read-only mode hides the editing controls. */
const CodeModal = ({ title, value = "", language = "json", readOnly = false, onSave, onClose }) => {
  const [text, setText] = React.useState(value);
  const [err, setErr] = React.useState("");
  const isJson = language === "json";
  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose && onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const format = () => {
    try { setText(JSON.stringify(JSON.parse(text), null, 2)); setErr(""); }
    catch (e) { setErr("Invalid JSON: " + (e.message || e)); }
  };
  const save = () => {
    if (isJson && text.trim()) {
      try { JSON.parse(text); } catch (e) { setErr("Invalid JSON: " + (e.message || e)); return; }
    }
    onSave && onSave(text);
    onClose && onClose();
  };
  return (
    <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose && onClose(); }}>
      <div className="modal" style={{ width: 1040, maxWidth: "96vw", height: "90vh", display: "flex", flexDirection: "column" }}>
        <div className="modal-head">
          <span className="ico-tile solid" style={{ width: 26, height: 26, marginRight: 10 }}><I.terminal size={14}/></span>
          <h3>{title || "Edit"}</h3>
          <span className="hint mono" style={{ marginLeft: 8 }}>{language}{readOnly ? " · read-only" : ""}</span>
          <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={onClose}><I.x size={14}/></button>
        </div>
        <div className="modal-body" style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
          <textarea className="input mono" spellCheck={false} readOnly={readOnly}
                    style={{ flex: 1, resize: "none", fontSize: 13, lineHeight: 1.5, width: "100%", minHeight: 0, whiteSpace: "pre", overflowWrap: "normal" }}
                    value={text} onChange={e => { setText(e.target.value); if (err) setErr(""); }}/>
          {err && <div style={{ color: "var(--rose, #fb7185)", fontSize: 12, marginTop: 8 }}>{err}</div>}
        </div>
        <div className="modal-foot" style={{ alignItems: "center" }}>
          {isJson && !readOnly && <button className="btn ghost sm" onClick={format}><I.zap size={13}/> Format JSON</button>}
          <span className="hint mono" style={{ marginLeft: 8 }}>{text.length} chars · {text.split("\n").length} lines</span>
          {readOnly
            ? <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={onClose}>Close</button>
            : <button className="btn accent sm" style={{ marginLeft: "auto" }} onClick={save}><I.check size={13}/> Save</button>}
        </div>
      </div>
    </div>
  );
};

/* A labeled code/JSON input that stays compact inline but has an Expand button
 * opening the full-screen CodeModal. Controlled via value/onChange. */
const CodeField = ({ label, hint, value = "", onChange, language = "json", placeholder, rows = 6, readOnly = false }) => {
  const [open, setOpen] = React.useState(false);
  return (
    <Field label={label} hint={hint}>
      <div style={{ position: "relative" }}>
        <textarea className="input mono" rows={rows} spellCheck={false} placeholder={placeholder} readOnly={readOnly}
                  style={{ resize: "vertical", fontSize: 12, width: "100%", paddingRight: 92 }}
                  value={value} onChange={e => onChange && onChange(e.target.value)}/>
        <button type="button" className="btn ghost sm icon-btn" title="Open full-screen editor"
                style={{ position: "absolute", top: 6, right: 6 }}
                onClick={() => setOpen(true)}><I.maximize size={14}/></button>
      </div>
      {open && <CodeModal title={label} value={value} language={language} readOnly={readOnly}
                          onSave={v => onChange && onChange(v)} onClose={() => setOpen(false)}/>}
    </Field>
  );
};

export { Avatar, Pill, Trend, Kpi, CardHead, Toggle, Radio, RadioTile, Field, Bar, Chk, Spark, MixChart, Modal, CodeModal, CodeField, Identicon, Skel, DagGraph, Help, Disclosure, Verified };
