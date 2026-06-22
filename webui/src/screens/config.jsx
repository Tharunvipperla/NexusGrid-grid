/* Local Config — every classic setting, fully editable. Changes auto-save:
 * a debounced watcher sends only the dirty keys through POST
 * /local/settings_partial (Wave 68), which re-submits everything else
 * unchanged through the classic validation + side-effect path. The Drive
 * key is only sent when the user actually types a new one (the "***" mask
 * never round-trips). Relay/tunnel controls use their dedicated endpoints. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, Field, Toggle, Chk, RadioTile, Help } from "../components.jsx";
import { notify } from "../toast.jsx";

const Section = ({ icon, tone, title, sub, right, children }) => (
  <div className="card pad-lg">
    <div className="fsec-head">
      <span className={"ico-tile " + (tone || "emerald")} style={{ width: 28, height: 28 }}>{icon}</span>
      <h4>{title}</h4>
      {sub && <span className="fsec-sub">{sub}</span>}
      {right && <span style={{ marginLeft: "auto" }}>{right}</span>}
    </div>
    {children}
  </div>
);

const Row = ({ label, hint, help, children }) => (
  <div className="row" style={{ gap: 10, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
    {children}
    <span style={{ fontSize: 13 }}>{label}{help && <Help text={help}/>}</span>
    {hint && <span className="hint">{hint}</span>}
  </div>
);

const fromSettings = (s) => ({
  node_online: !!s.node_online,
  mode: s.mode || "user",
  data_retention: s.data_retention || "delete",
  user_display_name: s.user_display_name || "",
  node_region: s.node_region || "local",
  node_tags: (s.node_tags || []).join(", "),
  hide_profile: !!s.hide_profile,
  max_ram: s.max_ram_pct ?? 80,
  sharing_mode: s.sharing_mode || "shared",
  max_serving_masters: s.max_serving_masters ?? 2,
  node_gpu: !!s.node_gpu,
  max_gpu_pct: s.max_gpu_pct ?? 80,
  lease_seconds: s.lease_seconds ?? 30,
  master_quota_per_origin: s.master_quota_per_origin ?? 3,
  retry_backoff_base_sec: s.retry_backoff_base_sec ?? 5,
  worker_cooldown_sec: s.worker_cooldown_sec ?? 20,
  prefer_reliable_workers: !!s.prefer_reliable_workers,
  step_gate: !!s.step_gate,
  queue_timeout_sec: s.queue_timeout_sec ?? 0,
  security_profile: s.security_profile || "maximum",
  allowed_images: (Array.isArray(s.allowed_images) ? s.allowed_images : []).join("\n"),
  native_runtime_enabled: !!s.native_runtime_enabled,
  require_worker_consent: !!s.require_worker_consent,
  consent_timeout_sec: s.consent_timeout_sec ?? 10,
  consent_max_strikes: s.consent_max_strikes ?? 3,
  require_venv_isolation: !!s.require_venv_isolation,
  cache_venvs: !!s.cache_venvs,
  idle_auto_accept: !!s.idle_auto_accept,
  idle_threshold_sec: s.idle_threshold_sec ?? 300,
  enable_task_scanning: !!s.enable_task_scanning,
  allow_network_tasks: !!s.allow_network_tasks,
  allow_cross_region_workers: !!s.allow_cross_region_workers,
  accept_cross_region_tasks: !!s.accept_cross_region_tasks,
  gdrive_key: "",                       // never prefill the secret
  foreign_storage_accept_offers: !!s.foreign_storage_accept_offers,
  storage_max_total_gb: s.storage_max_total_gb ?? 5,
  storage_window_chunks: s.storage_window_chunks ?? 32,
  fs_auto_offer_timeout_sec: s.fs_auto_offer_timeout_sec ?? 300,
  fs_transit_max_retries: s.fs_transit_max_retries ?? 5,
  fs_transit_chunk_ack_timeout_sec: s.fs_transit_chunk_ack_timeout_sec ?? 30,
  fs_transit_silence_timeout_sec: s.fs_transit_silence_timeout_sec ?? 60,
  fs_transit_abandoned_chunk_ttl_hours: s.fs_transit_abandoned_chunk_ttl_hours ?? 24,
  fs_auto_rescue: s.fs_auto_rescue !== false,
  fs_auto_rescue_mode: s.fs_auto_rescue_mode || "folder_then_cloud",
  fs_auto_rescue_trigger: s.fs_auto_rescue_trigger || "eviction",
  fs_auto_rescue_days: s.fs_auto_rescue_days ?? 2,
  fs_auto_rescue_dir: s.fs_auto_rescue_dir || "",
  fs_auto_rescue_cloud_cred: s.fs_auto_rescue_cloud_cred || "",
  fs_auto_rescue_rclone_targets: (s.fs_auto_rescue_rclone_targets || []).join("\n"),
  relay_enabled: s.relay_enabled !== false,
  relay_grid_key: "",                   // secret — only send when typed
});

/* Searchable, bounded dropdown — picks the relay module to run without a
 * giant <select> when there are many modules. */
const RelayPicker = ({ modules, value, disabled, onChange }) => {
  const [open, setOpen] = React.useState(false);
  const [q, setQ] = React.useState("");
  const ref = React.useRef(null);
  React.useEffect(() => {
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);
  const shown = q.trim() ? modules.filter(m => m.name.toLowerCase().includes(q.trim().toLowerCase())) : modules;
  return (
    <div ref={ref} style={{ position: "relative", width: 200 }}>
      <button type="button" className="input" disabled={disabled} onClick={() => setOpen(o => !o)}
              style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, width: "100%", cursor: disabled ? "not-allowed" : "pointer" }}>
        <span className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</span>
        <I.chevronDown size={14} style={{ flexShrink: 0, color: "var(--t-mute)" }}/>
      </button>
      {open && !disabled && (
        <div className="card" style={{ position: "absolute", top: "calc(100% + 4px)", left: 0, width: "100%", zIndex: 30, padding: 6, maxHeight: 240, display: "flex", flexDirection: "column" }}>
          <input className="input" autoFocus placeholder="Search modules…" value={q}
                 onChange={e => setQ(e.target.value)} style={{ marginBottom: 6, flexShrink: 0 }}/>
          <div style={{ overflowY: "auto" }}>
            {shown.length === 0 && <div className="dim" style={{ padding: "6px 8px", fontSize: 12 }}>No matches</div>}
            {shown.map(m => (
              <div key={m.name} className="mono" onClick={() => { onChange(m.name); setOpen(false); setQ(""); }}
                   style={{ padding: "7px 8px", borderRadius: 6, cursor: "pointer", fontSize: 12.5,
                            background: m.name === value ? "var(--bg-card-2)" : "transparent" }}>
                {m.name}{m.builtin ? " (default)" : ""}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

/* C4: secrets vault — store env secrets once, reference them as
 * secret://NAME in task/service env. Values are write-only (never shown). */
const SecretsCard = () => {
  const [rows, setRows] = React.useState([]);
  const [adding, setAdding] = React.useState(false);
  const [f, setF] = React.useState({ name: "", value: "", description: "" });
  const load = React.useCallback(async () => {
    try { setRows((await api.get("/local/secrets")).secrets || []); } catch (_) {}
  }, []);
  React.useEffect(() => { load(); }, [load]);
  const add = async () => {
    try {
      await api.post("/local/secrets", { name: f.name.trim(), value: f.value, description: f.description.trim() });
      setF({ name: "", value: "", description: "" }); setAdding(false); notify("Secret saved"); load();
    } catch (e) { notify("Secret failed: " + (e.detail || e.message || "")); }
  };
  const del = async (name) => {
    try { await api.del(`/local/secrets/${encodeURIComponent(name)}`); notify("Secret removed"); load(); }
    catch (e) { notify("Remove failed: " + (e.detail || e.message || "")); }
  };
  return (
    <Section icon={<I.key size={14}/>} tone="rose" title="Secrets"
             sub="encrypted env secrets — reference as secret://NAME in task/service env"
             right={<button className="btn ghost sm" onClick={() => setAdding(!adding)}><I.plus size={13}/> Add secret</button>}>
      {adding && (
        <div className="col" style={{ gap: 10, marginTop: 12 }}>
          <div className="field-row tri">
            <Field label="Name (UPPER_SNAKE_CASE)"><input className="input mono" placeholder="OPENAI_API_KEY" value={f.name} onChange={e => setF({ ...f, name: e.target.value })}/></Field>
            <Field label="Value (write-only)"><input className="input mono" type="password" value={f.value} onChange={e => setF({ ...f, value: e.target.value })}/></Field>
            <Field label="Description (optional)"><input className="input" value={f.description} onChange={e => setF({ ...f, description: e.target.value })}/></Field>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn accent sm" disabled={!f.name.trim() || !f.value} onClick={add}><I.check size={13}/> Save</button>
            <button className="btn ghost sm" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {rows.length === 0 && !adding && <div className="dim" style={{ padding: "12px 0", fontSize: 12 }}>No secrets yet. Values are encrypted at rest and never shown again.</div>}
      {rows.length > 0 && (
        <table className="t" style={{ marginTop: 12 }}>
          <thead><tr><th>Name</th><th>Description</th><th>Last used</th><th></th></tr></thead>
          <tbody>
            {rows.map(s => (
              <tr key={s.name}>
                <td className="mono name">{s.name}</td>
                <td style={{ fontSize: 13 }}>{s.description || "—"}</td>
                <td className="mono dim" style={{ fontSize: 11 }}>{(s.last_used_at || "").slice(0, 10) || "never"}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="icon-btn" title="Delete secret" onClick={() => del(s.name)}><I.trash size={14}/></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
};

/* E5 — node backup: one-click export, and upload-to-restore (applied on the
 * next node start, so a running node is never overwritten in place). */
const BackupCard = () => {
  const tok = encodeURIComponent(api.token);
  const normalHref = `/local/backup?local_token=${tok}`;
  const fullHref = `/local/backup?full=1&local_token=${tok}`;
  const fileRef = React.useRef(null);
  const [msg, setMsg] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const onFile = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    setBusy(true); setMsg("");
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await api.post("/local/restore", fd);
      setMsg(r.message || "Backup staged.");
      notify((r.full ? "Full backup" : "Backup") + " uploaded — restart the node to finish restoring");
    } catch (err) {
      setMsg("Restore failed: " + (err.detail || err.message || ""));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <Section icon={<I.hdd size={14}/>} tone="amber" title="Backup & restore"
             sub="export this node, or restore one by uploading it">
      <div className="col" style={{ gap: 10, marginTop: 12 }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <a className="btn accent sm" href={normalHref} download>
            <I.download size={13}/> Download backup
          </a>
          <a className="btn ghost sm" href={fullHref} download>
            <I.box size={13}/> Download full backup
          </a>
          <button className="btn ghost sm" disabled={busy} onClick={() => fileRef.current && fileRef.current.click()}>
            <I.box size={13}/> {busy ? "Uploading…" : "Restore from backup…"}
          </button>
          <input ref={fileRef} type="file" accept=".zip" style={{ display: "none" }} onChange={onFile}/>
        </div>
        <div className="hint" style={{ fontSize: 12 }}>
          <b>Backup</b> = your identity + database (tasks, peers, groups, secrets vault, settings,
          deposit records). Small; everything needed to <i>be</i> this node again.
        </div>
        <div className="hint" style={{ fontSize: 12 }}>
          <b>Full backup</b> = a complete snapshot — the backup above <i>plus</i> on-disk data the
          database only references: your plugin modules, saved result artifacts, and the deposit
          files you host for peers. Larger, but restores the node exactly as it is now.
        </div>
        <div className="hint" style={{ fontSize: 12 }}>
          Both contain your private keys + at-rest secret (a full identity clone) — store them
          safely. <b>Restore:</b> upload either kind here (it's auto-detected), then restart the
          node — it's applied on the next start (your current DB is kept as{" "}
          <span className="mono">.pre_restore</span>).
        </div>
        {msg && <div className="hint" style={{ color: msg.includes("failed") ? "var(--rose, #fb7185)" : "var(--emerald, #34d399)", fontSize: 12 }}>{msg}</div>}
      </div>
    </Section>
  );
};

const ConfigScreen = ({ online, onPower }) => {
  const [relay, setRelay] = React.useState({});
  const [tunnel, setTunnel] = React.useState({});
  const [settings, setSettings] = React.useState(null);
  const [f, setF] = React.useState(null);          // editable form state
  const [base, setBase] = React.useState(null);    // last-saved snapshot for dirty checks
  const [url, setUrl] = React.useState("");
  const [port, setPort] = React.useState(9000);
  const [busy, setBusy] = React.useState("");
  const [relayModule, setRelayModule] = React.useState("default");
  const [modules, setModules] = React.useState([{ name: "default", builtin: true }]);
  const [creds, setCreds] = React.useState([]);

  const load = React.useCallback(async (reinit = false) => {
    try {
      const [r, t, net, mods, cl] = await Promise.all([
        api.get("/local/relay/status").catch(() => ({})),
        api.get("/local/relay/tunnel/status").catch(() => ({})),
        api.get("/local/network").catch(() => ({})),
        api.get("/local/relay/modules").catch(() => ({})),
        api.get("/local/foreign_storage/cloud_credentials").catch(() => ({})),
      ]);
      setRelay(r || {}); setTunnel(t || {});
      setCreds((cl && cl.credentials) || []);
      setModules((mods && mods.modules) || [{ name: "default", builtin: true }]);
      const s = (net && net.settings) || {};
      setSettings(s);
      if (r && r.port) setPort(r.port);
      // Which relay code runs is chosen here; default to the last-saved choice.
      setRelayModule((r && r.running && r.module) || s.local_relay_module || "default");
      setF(prev => (prev && !reinit ? prev : fromSettings(s)));
      setBase(prev => (prev && !reinit ? prev : fromSettings(s)));
    } catch (_) {}
  }, []);

  React.useEffect(() => {
    load();
    const id = setInterval(() => load(false), 10000);
    return () => clearInterval(id);
  }, [load]);

  const note = notify;   // bell-only — no toast popups (project rule)
  const set = (k) => (v) => setF({ ...f, [k]: v && v.target ? v.target.value : v });

  /* Auto-save: any change persists by itself after a short pause — the user
   * never has to think about a Save button. Success is silent; only a
   * failure surfaces (as a toast, with the form left dirty for retry). */
  const saveTimer = React.useRef(null);
  React.useEffect(() => {
    if (!f || !base) return;
    const dirtyKeys = Object.keys(f).filter(k => f[k] !== base[k]);
    if (!dirtyKeys.length) return;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      const body = {};
      for (const k of dirtyKeys) {
        let v = f[k];
        if (k === "node_tags") v = String(v); // server splits csv
        if (k === "allowed_images" || k === "fs_auto_rescue_rclone_targets") v = String(v).split("\n").map(s => s.trim()).filter(Boolean); // newline list → array
        if ((k === "gdrive_key" || k === "relay_grid_key") && !String(v).trim()) continue;
        body[k] = v;
      }
      if (!Object.keys(body).length) return;
      try {
        await api.post("/local/settings_partial", body);
        setBase(prev => ({ ...prev, ...Object.fromEntries(dirtyKeys.map(k => [k, f[k]])) }));
        await load(false);
      } catch (e) { note("Settings save failed: " + (e.detail || e.message)); }
    }, 900);
    return () => clearTimeout(saveTimer.current);
  }, [f, base, load]);

  const act = async (label, fn) => {
    setBusy(label);
    try { await fn(); note(label + " ✓"); await load(false); }
    catch (e) { note(label + " failed: " + (e.detail || e.message || "")); }
    finally { setBusy(""); }
  };

  const s = settings || {};
  const relayState = relay.running ? (relay.lan_only ? "LAN-only" : "Online") : "Stopped";
  const relayTone = relay.running ? (relay.lan_only ? "amber" : "emerald") : "rose";
  const publicUrl = tunnel.public_url || tunnel.url || "";

  if (!f) {
    return <div className="page-head"><div className="page-title">Local node configuration</div><div className="hint">Loading…</div></div>;
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Local node configuration</div>
          <div className="page-sub">Changes save automatically. These are this node's defaults — dispatches and deposits can override the scheduler and transfer values per job.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={() => load(true)}><I.refresh size={14}/> Reload from node</button>
        </div>
      </div>

      <div className="col" style={{ gap: 14, marginBottom: 24 }}>

        {/* Node status */}
        <Section icon={<I.power size={14}/>} tone="emerald" title="Node status">
          <Row label="Accept network work" hint={online ? "this node serves the grid" : "shut down — not serving; running tasks, the relay and the tunnel are stopped"}>
            <Toggle on={online} onChange={(v) => onPower && onPower(v)}/>
          </Row>
          <div className="field-row" style={{ marginTop: 14 }}>
            <div>
              <div className="label" style={{ marginBottom: 8 }}>Control mode</div>
              <RadioTile on={f.mode === "user"} title="User control" sub="watchdog active — you keep priority" onClick={() => set("mode")("user")}/>
              <div style={{ height: 8 }}/>
              <RadioTile on={f.mode === "master"} title="Master control" sub="dedicated compute node" onClick={() => set("mode")("master")}/>
            </div>
            <div>
              <div className="label" style={{ marginBottom: 8 }}>Downloaded data</div>
              <RadioTile on={f.data_retention === "delete"} title="Auto-clean" sub="delete datasets after processing" onClick={() => set("data_retention")("delete")}/>
              <div style={{ height: 8 }}/>
              <RadioTile on={f.data_retention === "keep"} title="Keep cached" sub="retain files on local disk" onClick={() => set("data_retention")("keep")}/>
            </div>
          </div>
        </Section>

        {/* Internet relay — live controls + persisted relay settings */}
        <Section icon={<I.broadcast size={14}/>} tone="cyan"
                 title="Internet relay" sub="discover and reach peers outside your LAN"
                 right={<Pill tone={relayTone} dot>{relayState}</Pill>}>
          <div className="field-row">
            <Field label="Primary relay URL"
                   hint={s.relay_server_url ? <>current: <span className="mono">{s.relay_server_url}</span></> : "ws:// or wss:// — no relay set yet"}
                   help="A relay is a rendezvous server that forwards encrypted frames between nodes that can't reach each other directly. It never sees your content.">
              <input className="input mono" placeholder={s.relay_server_url || "wss://relay.example.com"}
                     value={url} onChange={e => setUrl(e.target.value)}/>
            </Field>
            <Field label="&nbsp;">
              <button className="btn accent" style={{ width: "fit-content" }} disabled={!url || !!busy}
                      onClick={() => act("Set relay URL", () => api.post("/local/relay/set_url", { relay_url: url.trim() }))}>
                <I.check size={14}/> Set as primary
              </button>
            </Field>
          </div>

          <div className="field-row" style={{ marginTop: 14 }}>
            <Field label="Bundled local relay"
                   hint={relay.running && relay.suggested_url
                     ? <>serving <span className="mono">{relay.suggested_url}</span> · code <span className="mono">{relay.module || "default"}</span></>
                     : <>run a relay inside this node so others can use it — pick the code (default or one of your modules from Plugins)</>}>
              <div className="row" style={{ gap: 8 }}>
                <RelayPicker modules={modules} value={relayModule} disabled={relay.running}
                             onChange={setRelayModule}/>
                <input className="input" type="number" style={{ width: 110 }} value={port}
                       onChange={e => setPort(+e.target.value)}/>
                {relay.running
                  ? <button className="btn ghost" disabled={!!busy} onClick={() => act("Stop local relay", () => api.post("/local/relay/stop"))}><I.pause size={14}/> {busy === "Stop local relay" ? "Stopping…" : "Stop"}</button>
                  : <button className="btn accent" disabled={!!busy} onClick={() => act("Start local relay", () => api.post("/local/relay/start", { port: Number(port), module: relayModule }))}><I.play size={14}/> {busy === "Start local relay" ? "Launching…" : "Start"}</button>}
              </div>
            </Field>
            <Field label="Public reachability"
                   hint={publicUrl ? <>public at <span className="mono">{publicUrl}</span></> : "LAN-only — open a tunnel to be reachable from anywhere"}
                   help="Opens a secure outbound tunnel so peers outside your network can reach this node without router changes or port forwarding.">
              <div className="row" style={{ gap: 8 }}>
                {publicUrl ? <Pill tone="emerald" dot>public</Pill> : <Pill tone="amber" dot>LAN-only</Pill>}
                {publicUrl
                  ? <button className="btn ghost" disabled={!!busy} onClick={() => act("Stop tunnel", () => api.post("/local/relay/tunnel/stop"))}><I.linkOff size={14}/> {busy === "Stop tunnel" ? "Closing…" : "Stop tunnel"}</button>
                  : <button className="btn accent" disabled={!!busy} onClick={() => act("Open tunnel", () => api.post("/local/relay/tunnel/start"))}><I.link size={14}/> {busy === "Open tunnel" ? "Opening…" : "Make reachable"}</button>}
              </div>
            </Field>
          </div>

          <div className="field-row" style={{ marginTop: 14 }}>
            <Field label="Relay participation" hint="off = never connect to any relay">
              <div className="row" style={{ height: 38, alignItems: "center" }}>
                <Toggle on={f.relay_enabled} onChange={set("relay_enabled")}/>
                <span className="hint">{f.relay_enabled ? "relays may be used to reach peers" : "direct connections only"}</span>
              </div>
            </Field>
            <Field label="Relay grid key" hint={s.relay_grid_key ? "a key is configured — typing replaces it" : "no key set"}
                   help="A shared secret some private relays require before accepting connections. Only sent when you type a new one.">
              <input className="input mono" type="password" placeholder="••••••" value={f.relay_grid_key} onChange={set("relay_grid_key")}/>
            </Field>
          </div>
        </Section>

        {/* Identity */}
        <Section icon={<I.user size={14}/>} tone="emerald" title="Node identity">
          <div className="field-row tri">
            <Field label="Display name"><input className="input" maxLength={50} value={f.user_display_name} onChange={set("user_display_name")}/></Field>
            <Field label="Region" hint="schedulers can prefer/require regions"><input className="input mono" value={f.node_region} onChange={set("node_region")}/></Field>
            <Field label="Tags (CSV)" hint="capability labels, e.g. python, highmem"><input className="input mono" value={f.node_tags} onChange={set("node_tags")}/></Field>
          </div>
          <Row label="Hide IP from peers" hint="peers see a masked address; routing still works">
            <Toggle on={f.hide_profile} onChange={set("hide_profile")}/>
          </Row>
        </Section>

        {/* Resources & sharing */}
        <Section icon={<I.cpu size={14}/>} tone="amber" title="Resources & sharing">
          <div className="field-row tri">
            <Field label={`Max RAM for tasks — ${f.max_ram}%`}>
              <input type="range" min={10} max={95} value={f.max_ram} onChange={e => set("max_ram")(+e.target.value)} style={{ width: "100%" }}/>
            </Field>
            <Field label={`Max concurrent coordinators — ${f.max_serving_masters}`} hint="when sharing by capacity (1–8)">
              <input type="range" min={1} max={8} step={1} value={f.max_serving_masters} onChange={e => set("max_serving_masters")(+e.target.value)} style={{ width: "100%" }}/>
            </Field>
            <Field label={`Max GPU usage — ${f.max_gpu_pct}%`}>
              <input type="range" min={10} max={95} value={f.max_gpu_pct} onChange={e => set("max_gpu_pct")(+e.target.value)} style={{ width: "100%" }}/>
            </Field>
          </div>
          <div className="field-row" style={{ marginTop: 14 }}>
            <RadioTile on={f.sharing_mode === "shared"} title="Share by capacity" sub="serve multiple coordinators while resources allow" onClick={() => set("sharing_mode")("shared")}/>
            <RadioTile on={f.sharing_mode === "single"} title="Isolate to one" sub="serve a single coordinator at a time" onClick={() => set("sharing_mode")("single")}/>
          </div>
          <Row label="GPU compute" hint="ignored if no GPU is detected">
            <Toggle on={f.node_gpu} onChange={set("node_gpu")}/>
          </Row>
        </Section>

        {/* Scheduler & safety */}
        <Section icon={<I.cog size={14}/>} tone="cyan" title="Scheduler & safety"
                 sub="node-wide defaults — each dispatch can override them">
          <div className="field-row tri">
            <Field label="Lease seconds" hint="heartbeat window before a task is considered lost"
                   help="When a worker runs your task it heartbeats; if it goes silent for this many seconds the task is declared lost and re-queued elsewhere. Each dispatch can override this.">
              <input className="input" type="number" min={5} value={f.lease_seconds} onChange={e => set("lease_seconds")(+e.target.value)}/>
            </Field>
            <Field label="Per-origin quota" hint="max queued tasks per requesting master"
                   help="How many tasks one requesting node may hold in your queue at the same time — stops a single peer from monopolizing this worker.">
              <input className="input" type="number" min={1} value={f.master_quota_per_origin} onChange={e => set("master_quota_per_origin")(+e.target.value)}/>
            </Field>
            <Field label="Retry backoff base (s)"
                   help="Wait before a failed task's first automatic retry; each further retry waits exponentially longer (base, 2x, 4x…). Each dispatch can override this.">
              <input className="input" type="number" min={1} value={f.retry_backoff_base_sec} onChange={e => set("retry_backoff_base_sec")(+e.target.value)}/>
            </Field>
          </div>
          <div className="field-row" style={{ marginTop: 14 }}>
            <Field label="Worker cooldown (s)" hint="rest between tasks on this node"
                   help="A breather after finishing one task before this node accepts the next — keeps a busy grid from pinning your machine at 100% back-to-back.">
              <input className="input" type="number" min={0} value={f.worker_cooldown_sec} onChange={e => set("worker_cooldown_sec")(+e.target.value)}/>
            </Field>
            <Field label="Default queue timeout (s)" hint="0 = wait forever; tasks can override"
                   help="How long a dispatched task may wait for a worker before failing as timed out. 0 disables the limit. Each dispatch can set its own.">
              <input className="input" type="number" min={0} value={f.queue_timeout_sec} onChange={e => set("queue_timeout_sec")(+e.target.value)}/>
            </Field>
          </div>
          <Row label="Prefer reliable workers (default)"
               help="When on, the scheduler ranks each candidate worker's finished-to-fail ratio above raw fitness so more reliable nodes win. This is the node-wide default; any task / service / DAG dispatch can override it.">
            <Chk on={f.prefer_reliable_workers} onChange={set("prefer_reliable_workers")}/>
          </Row>
          <Row label="Verify each DAG step (default)"
               help="When on, a DAG runs one level at a time: once a level's steps finish, the next is held for your approval before its nodes are assigned — so you can stop early if something looks wrong. Node-wide default; any DAG dispatch can override it.">
            <Chk on={f.step_gate} onChange={set("step_gate")}/>
          </Row>
        </Section>

        {/* Capability & security */}
        <Section icon={<I.shield size={14}/>} tone="rose" title="Capability & security">
          <div className="field-row">
            <Field label="Security profile">
              <select className="input" value={f.security_profile} onChange={set("security_profile")}>
                <option value="maximum">Maximum — full sandbox, network cut, code scan</option>
                <option value="standard">Standard — basic hardening, network cut</option>
                <option value="relaxed">Relaxed — legacy behaviour (not recommended)</option>
              </select>
            </Field>
            <Field label="Allowed container images" hint={'one image per line · "*" allows any image — risky'}>
              <textarea className="input mono" rows={4} value={f.allowed_images}
                        onChange={set("allowed_images")}
                        placeholder={"python:3.11-slim\nnode:20-slim\ngcc:latest"}/>
            </Field>
          </div>
          {f.security_profile === "relaxed" && (
            <div className="banner danger" style={{ marginTop: 10 }}>
              <I.alertT size={14}/><span>Relaxed profile disables sandbox hardening for incoming tasks. Only use on a throwaway machine.</span>
            </div>
          )}
          <Row label="Native host runtime" hint="lets tasks run directly on this OS — highest risk surface">
            <Toggle on={f.native_runtime_enabled} onChange={set("native_runtime_enabled")}/>
          </Row>
          {f.native_runtime_enabled && (
            <div className="banner danger" style={{ marginTop: 8 }}>
              <I.alertT size={14}/><span>Native tasks bypass container isolation. Pair this with worker consent below.</span>
            </div>
          )}
          <Row label="Ask before each task (worker consent)">
            <Toggle on={f.require_worker_consent} onChange={set("require_worker_consent")}/>
          </Row>
          {f.require_worker_consent && (
            <div className="field-row" style={{ marginTop: 10 }}>
              <Field label="Consent timeout (s)" hint="3–60; no answer = decline">
                <input className="input" type="number" min={3} max={60} value={f.consent_timeout_sec} onChange={e => set("consent_timeout_sec")(+e.target.value)}/>
              </Field>
              <Field label="Max strikes" hint="auto-decline a master after this many ignored prompts"
                     help="If you ignore consent prompts from the same coordinator this many times, its further requests are declined automatically instead of interrupting you again.">
                <input className="input" type="number" min={0} max={10} value={f.consent_max_strikes} onChange={e => set("consent_max_strikes")(+e.target.value)}/>
              </Field>
            </div>
          )}
          <Row label="Auto-accept while idle" hint="skip consent prompts when you're away">
            <Toggle on={f.idle_auto_accept} onChange={set("idle_auto_accept")}/>
          </Row>
          {f.idle_auto_accept && (
            <div style={{ marginTop: 8, maxWidth: 280 }}>
              <Field label="Idle threshold (s)" hint="30–86400">
                <input className="input" type="number" min={30} max={86400} value={f.idle_threshold_sec} onChange={e => set("idle_threshold_sec")(+e.target.value)}/>
              </Field>
            </div>
          )}
          <div className="row" style={{ gap: 24, marginTop: 12, flexWrap: "wrap" }}>
            <Row label="Scan task code" help="Statically scans incoming task code for dangerous patterns before it runs; suspicious tasks are rejected."><Chk on={f.enable_task_scanning} onChange={set("enable_task_scanning")}/></Row>
            <Row label="Allow tasks network access" help="When off, tasks run with networking cut — they can compute but not phone home. Turn on only if your workloads genuinely need internet."><Chk on={f.allow_network_tasks} onChange={set("allow_network_tasks")}/></Row>
            <Row label="Venv isolation" help="Each Python task gets its own virtual environment, so its packages can't pollute the node or other tasks."><Chk on={f.require_venv_isolation} onChange={set("require_venv_isolation")}/></Row>
            <Row label="Cache venvs" help="Keep built virtual environments on disk so repeat tasks with the same requirements start instantly instead of reinstalling."><Chk on={f.cache_venvs} onChange={set("cache_venvs")}/></Row>
            <Row label="Use cross-region workers" help="Allow your dispatched tasks to run on workers outside your preferred region."><Chk on={f.allow_cross_region_workers} onChange={set("allow_cross_region_workers")}/></Row>
            <Row label="Accept cross-region tasks" help="Allow this node to serve tasks coming from coordinators in other regions."><Chk on={f.accept_cross_region_tasks} onChange={set("accept_cross_region_tasks")}/></Row>
          </div>
        </Section>

        {/* Cloud integration */}
        <Section icon={<I.cloud size={14}/>} tone="cyan" title="Cloud integration"
                 sub="data plane for Drive-backed workspaces">
          <Field label="Google Drive API key"
                 hint={s.gdrive_key === "***" ? "a key is configured — typing here replaces it; leaving blank keeps it" : "no key configured"}>
            <input className="input mono" type="password" placeholder="••••••••" value={f.gdrive_key} onChange={set("gdrive_key")}/>
          </Field>
        </Section>

        {/* C4: secrets vault */}
        <SecretsCard/>

        {/* E5: node backup */}
        <BackupCard/>

        {/* Foreign storage */}
        <Section icon={<I.hdd size={14}/>} tone="emerald" title="Foreign storage pledge"
                 sub="defaults — each deposit can set its own transfer window">
          <Row label="Accept storage offers" hint="host encrypted deposits for trusted peers">
            <Toggle on={f.foreign_storage_accept_offers} onChange={set("foreign_storage_accept_offers")}/>
          </Row>
          <div className="field-row" style={{ marginTop: 12 }}>
            <Field label="Pledged space (GB)" help="The total disk you promise to the grid for hosting other peers' encrypted deposits. Peers see this as your capacity when picking a host.">
              <input className="input" type="number" min={1} value={f.storage_max_total_gb} onChange={e => set("storage_max_total_gb")(+e.target.value)}/>
            </Field>
            <Field label="Transfer window (chunks)" hint="2–128 in-flight chunks per transfer"
                   help="Default number of encrypted chunks in flight (sent but not yet acknowledged) per transfer. A bigger window is faster on good links and uses more memory. New deposits can override this per deposit.">
              <input className="input" type="number" min={2} max={128} value={f.storage_window_chunks} onChange={e => set("storage_window_chunks")(+e.target.value)}/>
            </Field>
          </div>
          <div className="field-row tri" style={{ marginTop: 12 }}>
            <Field label="Chunk ack timeout (s)" hint="5–300"
                   help="How long the sender waits for the host to acknowledge one chunk before treating the transfer as stalled and pausing it for retry.">
              <input className="input" type="number" min={5} max={300} value={f.fs_transit_chunk_ack_timeout_sec} onChange={e => set("fs_transit_chunk_ack_timeout_sec")(+e.target.value)}/>
            </Field>
            <Field label="Transit retries" hint="1–20"
                   help="How many times a paused or failed transfer is resumed automatically before it's marked failed-in-transit for good.">
              <input className="input" type="number" min={1} max={20} value={f.fs_transit_max_retries} onChange={e => set("fs_transit_max_retries")(+e.target.value)}/>
            </Field>
            <Field label="Silence timeout (s)" hint="10–600"
                   help="If the other side goes completely quiet mid-transfer for this long, the transfer is paused so the retry pass can pick it up.">
              <input className="input" type="number" min={10} max={600} value={f.fs_transit_silence_timeout_sec} onChange={e => set("fs_transit_silence_timeout_sec")(+e.target.value)}/>
            </Field>
          </div>
          <div className="field-row" style={{ marginTop: 12 }}>
            <Field label="Fan-out offer timeout (s)" hint="30–86400"
                   help="In fan-out (auto) deposits, how long to wait for any candidate host to accept before the offer is abandoned.">
              <input className="input" type="number" min={30} max={86400} value={f.fs_auto_offer_timeout_sec} onChange={e => set("fs_auto_offer_timeout_sec")(+e.target.value)}/>
            </Field>
            <Field label="Abandoned chunk TTL (h)" hint="1–24"
                   help="How long a host keeps partial chunks of a dead transfer on disk before cleaning them up.">
              <input className="input" type="number" min={1} max={24} value={f.fs_transit_abandoned_chunk_ttl_hours} onChange={e => set("fs_transit_abandoned_chunk_ttl_hours")(+e.target.value)}/>
            </Field>
          </div>
        </Section>

        {/* Auto-recovery — salvage YOUR deposits when a host evicts them */}
        {(() => {
          const mode = f.fs_auto_rescue_mode || "folder_then_cloud";
          const usesFolder = mode !== "cloud_only";
          const usesCloud = mode !== "folder_only";
          return (
        <Section icon={<I.download size={14}/>} tone="amber" title="Auto-recovery"
                 sub="automatically salvage data you stored on peers before a host drops it">
          <Row label="Auto-recovery" hint={f.fs_auto_rescue ? "on — at-risk deposits are recovered automatically" : "off — you'll only be warned in the bell"}>
            <Toggle on={f.fs_auto_rescue} onChange={set("fs_auto_rescue")}/>
          </Row>
          {f.fs_auto_rescue && (
            <>
              <div className="field-row" style={{ marginTop: 12 }}>
                <Field label="When to act"
                       help="“When evicting” acts only once a host starts evicting your deposit. “Before TTL” also recovers a still-healthy deposit when its time-to-live is within the chosen number of days.">
                  <select className="input" value={f.fs_auto_rescue_trigger} onChange={set("fs_auto_rescue_trigger")}>
                    <option value="eviction">When the host starts evicting</option>
                    <option value="days">Also before TTL expires</option>
                  </select>
                </Field>
                {f.fs_auto_rescue_trigger === "days" && (
                  <Field label="Days before TTL" hint="1–30">
                    <input className="input" type="number" min={1} max={30} value={f.fs_auto_rescue_days} onChange={e => set("fs_auto_rescue_days")(+e.target.value)}/>
                  </Field>
                )}
              </div>
              <div className="field-row" style={{ marginTop: 12 }}>
                <Field label="Recovery destination & order"
                       help="Where recovered data goes. ‘then’ options fall back automatically — e.g. local folder first, and cloud only if the local disk is full. Cloud needs a credential or rclone target below.">
                  <select className="input" value={mode} onChange={set("fs_auto_rescue_mode")}>
                    <option value="folder_then_cloud">Local folder, then cloud if full</option>
                    <option value="cloud_then_folder">Cloud, then local folder if it fails</option>
                    <option value="folder_only">Local folder only</option>
                    <option value="cloud_only">Cloud only</option>
                  </select>
                </Field>
                {usesFolder && (
                  <Field label="Rescue folder" hint="blank = a “rescued” folder in this node's data dir">
                    <input className="input mono" placeholder="C:\\Users\\…\\rescued" value={f.fs_auto_rescue_dir} onChange={set("fs_auto_rescue_dir")}/>
                  </Field>
                )}
              </div>
              {usesCloud && (
                <>
                  <div className="field-row" style={{ marginTop: 12 }}>
                    <Field label="Cloud credential (optional)"
                           hint={creds.length ? "host streams the encrypted bundle to your bucket — no password needed" : "none saved — add one in Foreign Storage, or use rclone below"}
                           help="If set, the host ships the still-encrypted data directly to this cloud account. Leave as ‘Use rclone’ to stream to any rclone-configured cloud instead.">
                      <select className="input" value={f.fs_auto_rescue_cloud_cred} onChange={set("fs_auto_rescue_cloud_cred")} disabled={!creds.length}>
                        <option value="">Use rclone targets below</option>
                        {creds.map(c => <option key={c.id} value={c.id}>{c.label || c.provider} — {c.provider}</option>)}
                      </select>
                    </Field>
                  </div>
                  {!f.fs_auto_rescue_cloud_cred && (
                    <Field label="Cloud targets — rclone (one per line, fallback order)"
                           hint="streamed straight to your cloud, never staged locally · needs rclone installed"
                           help="Configure any cloud in 'rclone config' first, then list one or more remote:path targets — they're tried top-to-bottom until one upload succeeds.">
                      <textarea className="input mono" rows={3} style={{ resize: "vertical", fontSize: 12 }}
                                placeholder={"gdrive:nexus/rescued\nwasabi:backups/nexus"}
                                value={f.fs_auto_rescue_rclone_targets} onChange={set("fs_auto_rescue_rclone_targets")}/>
                    </Field>
                  )}
                </>
              )}
              {usesFolder && (
                <div className="banner" style={{ marginTop: 10 }}>
                  <I.lock size={14}/><span>Local recovery pulls files to disk encrypted — no password needed at rescue time. If the deposit is already unlocked it's decrypted straight away; otherwise it lands as a <span className="mono">.enc</span> file and a <strong>Decrypt</strong> button appears in Foreign Storage (paste the password when you're back).</span>
                </div>
              )}
            </>
          )}
        </Section>
          );
        })()}
      </div>
    </>
  );
};

export { ConfigScreen };
