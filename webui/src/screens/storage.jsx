/* Foreign Storage — host encrypted deposits for peers / deposit on theirs.
 * Full parity port: new deposit (native file picker, auto-pick best-fit /
 * most-space, fan-out with group restriction), offer accept (in-page host-
 * terms consent) / decline, and the complete depositor action set per status
 * (cancel, resume, download, evict-to-cloud, share-view, delete) plus host-
 * side eviction scheduling. The host never sees plaintext; Share View is
 * PERMANENT by design (no revoke button — see project decision). */
import React from "react";
import { I } from "../icons.jsx";
import { api, subscribeEvents } from "../api.js";
import { Pill, CardHead, Bar, Chk, Field, Toggle, Modal, Disclosure } from "../components.jsx";
import { notify } from "../toast.jsx";

const gb = (v) => (v == null ? "—" : Number(v).toFixed(2) + " GB");
const bytes = (n) => {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
};

// "how long ago" for a deposit timestamp (ISO string).
const fmtAgo = (iso) => {
  const t = Date.parse(iso || "");
  if (!t || isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
};

// TTL window: expiry is ttl_at if set, else created_at + ttl_days. `near` once
// under two days remain (so we can warn before the host can purge it).
const ttlInfo = (dep) => {
  let exp = 0;
  if (dep.ttl_at) exp = Date.parse(dep.ttl_at);
  else if (dep.created_at && dep.ttl_days) exp = Date.parse(dep.created_at) + dep.ttl_days * 86400000;
  if (!exp || isNaN(exp)) return null;
  const rem = exp - Date.now();
  return { rem, near: rem > 0 && rem <= 2 * 86400000, expired: rem <= 0 };
};
const fmtRemain = (rem) => {
  if (rem <= 0) return "expired";
  const d = Math.floor(rem / 86400000), h = Math.floor((rem % 86400000) / 3600000);
  if (d >= 1) return `${d}d ${h}h left`;
  const m = Math.floor((rem % 3600000) / 60000);
  return h >= 1 ? `${h}h ${m}m left` : `${m}m left`;
};
// Live transfer progress (sending a deposit / downloading one). Speed + ETA
// come straight off the `storage_transfer_progress` SSE events.
const fmtSpeed = (bps) => (bps > 0 ? bytes(bps) + "/s" : "—");
const fmtEta = (sec) => {
  if (!sec || sec <= 0 || !isFinite(sec)) return "";
  if (sec < 60) return `${Math.ceil(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${Math.round(sec / 3600)}h`;
};
const ProgressLine = ({ p, totalBytes }) => {
  if (!p || Date.now() - p.ts > 12000) return null;   // hide once events stop
  const verb = p.role === "depositor" ? "uploading" : "downloading";
  const remBytes = Math.max(0, (totalBytes || 0) - (p.bytesNow || 0));
  const eta = p.speed > 0 ? fmtEta(remBytes / p.speed) : "";
  return (
    <div style={{ marginTop: 6, minWidth: 150 }}>
      <Bar value={p.pct} color="var(--cyan)"/>
      <div className="hint mono" style={{ marginTop: 3, fontSize: 11 }}>
        {p.pct}% · {verb} · {fmtSpeed(p.speed)}{eta ? ` · ${eta} left` : ""}
      </div>
    </div>
  );
};

const TERMINAL = ["withdrawn", "deleted", "expired", "failed", "evicted", "completed"];
const statusTone = (s) => {
  if (typeof s === "string" && s.startsWith("paused_")) return "amber";
  return {
    completed: "emerald", active: "emerald", accepted: "emerald", hosting: "emerald", stored: "emerald",
    pending: "amber", offered: "amber", withdrawn: "amber", paused: "amber", transferring: "amber",
    queued_offline: "amber", offering_multi: "amber", evicting_to_cloud: "amber",
    rescued_encrypted: "cyan",
    eviction_requested: "rose", in_db_grace: "rose",
    failed: "rose", declined: "rose", evicted: "rose",
  }[s] || "ghost";
};

/* Same password policy as classic fsValidateDepositPassword. */
const pwError = (pw) => {
  if (!pw || pw.length < 10) return "Password must be at least 10 characters.";
  if (!/[A-Z]/.test(pw)) return "Password must include an uppercase letter.";
  if (!/[a-z]/.test(pw)) return "Password must include a lowercase letter.";
  if (!/[0-9]/.test(pw)) return "Password must include a digit.";
  if (!/[^A-Za-z0-9]/.test(pw)) return "Password must include a symbol (e.g. !@#$%).";
  return "";
};

/* Two-step destructive button. Resting state can be icon-only (with a
 * tooltip); arming always shows the explicit confirm text so a click can't
 * destroy anything silently. */
const Danger = ({ label, confirmLabel = "Sure?", onFire, accent = false, icon = null, title = null }) => {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <button className={"btn sm " + (armed || accent ? "accent" : "ghost")}
            title={title || label}
            onClick={() => { if (armed) { setArmed(false); onFire(); } else setArmed(true); }}>
      {armed ? confirmLabel : (icon || label)}
    </button>
  );
};

/* Storage action panels render as centered modals — uniform with every
 * other dialog in the app, not cards pushed to the top of the page. */
const PanelShell = ({ icon, tone, title, onClose, children }) => (
  <Modal title={title} icon={icon} tone={tone} width={680} onClose={onClose}>
    {children}
  </Modal>
);

/* ── New deposit ── */
const DepositPanel = ({ onDone, onCancel, flash }) => {
  const [peers, setPeers] = React.useState([]);        // trusted peers (manual pick)
  const [target, setTarget] = React.useState("");      // internal_ip (manual) or peer_uuid (auto-pick)
  const [autoPicked, setAutoPicked] = React.useState(false);
  const [fanout, setFanout] = React.useState(false);
  const [groups, setGroups] = React.useState([]);
  const [selGroups, setSelGroups] = React.useState([]);
  const [filePath, setFilePath] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [hint, setHint] = React.useState("");
  const [ttl, setTtl] = React.useState(30);
  const [window, setWindow] = React.useState("");
  const [ackT, setAckT] = React.useState("");
  const [retries, setRetries] = React.useState("");
  const [offerT, setOfferT] = React.useState("");
  const [transport, setTransport] = React.useState("stream");
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    api.get("/local/peers").then(d => setPeers(((d && d.peers) || []).filter(p =>
      ["trusted", "trusted_pending_in", "trusted_pending_out"].includes(p.status)))).catch(() => {});
    api.get("/local/groups").then(d => setGroups((d && d.groups) || [])).catch(() => {});
  }, []);

  const pickFile = async () => {
    setStatus("Opening the file picker on this machine…");
    try {
      const d = await api.get("/local/foreign_storage/pick_file");
      if (d.path) { setFilePath(d.path); setStatus(""); }
      else setStatus("No file selected.");
    } catch (e) { setStatus("Picker failed: " + (e.detail || e.message)); }
  };

  const autoPick = async (strategy) => {
    setStatus("Querying peers…");
    let sizeGb = 0;
    if (filePath.trim()) {
      try {
        const info = await api.post("/local/file_info", { file_path: filePath.trim() });
        if (!info.exists) { setStatus("File not found."); return; }
        if (!info.is_file) { setStatus("Path is not a file (folders not supported)."); return; }
        sizeGb = (info.size_bytes || 0) / (1024 ** 3);
      } catch (e) { setStatus("File lookup failed: " + (e.detail || e.message)); return; }
    }
    try {
      const d = await api.get("/local/foreign_storage/peer_capacities");
      const cands = ((d && d.peers) || []).filter(p => p.available && p.accepting && (sizeGb === 0 || p.free_gb >= sizeGb));
      if (!cands.length) { setStatus(sizeGb ? `No peer has ${sizeGb.toFixed(2)} GB free.` : "No available peers accepting offers."); return; }
      const pick = [...cands].sort((a, b) => strategy === "best_fit" ? a.free_gb - b.free_gb : b.free_gb - a.free_gb)[0];
      setTarget(pick.peer_uuid); setAutoPicked(true);
      setStatus(`Picked ${pick.display_name || pick.peer_uuid} (${pick.free_gb.toFixed(2)} GB free)`);
    } catch (e) { setStatus("Peer query failed: " + (e.detail || e.message)); }
  };

  const submit = async () => {
    const file_path = filePath.trim();
    if ((!fanout && !target) || !file_path || !password) { setStatus("Host (or fan-out), file path, and password are required."); return; }
    const pe = pwError(password);
    if (pe) { setStatus(pe); return; }
    setBusy(true); setStatus("Sending offer…");
    try {
      const out = await api.post("/local/foreign_storage/deposit", {
        target_peer: fanout ? "auto" : target,
        file_path, password, ttl_days: Number(ttl) || 30, transport,
        window_chunks: Number(window) || 0,
        ack_timeout_sec: Number(ackT) || 0,
        transit_retries: Number(retries) || 0,
        offer_timeout_sec: Number(offerT) || 0,
        password_hint: hint,
        // Only an explicitly-picked host may queue-on-offline (user spec):
        queue_if_offline: !fanout && !autoPicked,
        target_groups: fanout ? selGroups : [],
      });
      if (out.status === "queued_offline") flash(`Target offline — offer queued (24h TTL). id=${out.deposit_id}`);
      else if (out.status === "offering_multi") flash(`Fan-out offer sent to ${(out.candidates || []).length} candidates — first to accept wins.`);
      else flash(`Offer sent — ${out.chunk_count} chunks.`);
      onDone();
    } catch (e) { setStatus("Deposit failed: " + (e.detail || e.message)); }
    finally { setBusy(false); }
  };

  return (
    <PanelShell onClose={onCancel} icon={<I.upload size={14}/>} tone="cyan" title="New deposit">
      <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 12 }}>
        <Chk on={fanout} onChange={v => { setFanout(v); if (v) { setTarget(""); setAutoPicked(false); } }}/>
        <span style={{ fontSize: 13 }}>Fan-out (auto)</span>
        <span className="hint">offer the top-3 hosts by free space; first to accept wins</span>
      </div>

      {!fanout && (
        <>
          <Field label="Host" hint="a trusted peer that will store your encrypted file">
            <select className="input" value={target} onChange={e => { setTarget(e.target.value); setAutoPicked(false); }}>
              <option value="">— pick a host —</option>
              {peers.map(p => {
                const id = p.internal_ip || p.ip;
                return <option key={id} value={id}>{p.display_name ? `${p.display_name} (${id})` : id}</option>;
              })}
            </select>
          </Field>
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            <button className="btn ghost sm" onClick={() => autoPick("best_fit")}>Auto-pick: best fit</button>
            <button className="btn ghost sm" onClick={() => autoPick("most_space")}>Auto-pick: most space</button>
          </div>
        </>
      )}
      {fanout && groups.length > 0 && (
        <div style={{ marginBottom: 4 }}>
          <div className="label" style={{ marginBottom: 6 }}>Restrict to groups (optional)</div>
          <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
            {groups.map(g => (
              <div key={g.id} className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }}
                   onClick={() => setSelGroups(selGroups.includes(g.id) ? selGroups.filter(x => x !== g.id) : [...selGroups, g.id])}>
                <Chk on={selGroups.includes(g.id)}/>
                <span style={{ fontSize: 13 }}>{g.name || g.id}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <Field label="File on this machine" hint="absolute path — Browse opens a native picker on the node">
          <div className="row" style={{ gap: 8 }}>
            <input className="input mono" style={{ flex: 1, minWidth: 0 }} placeholder="C:\\data\\backup.zip" value={filePath} onChange={e => setFilePath(e.target.value)}/>
            <button className="btn ghost" style={{ flexShrink: 0 }} onClick={pickFile}><I.search size={14}/> Browse…</button>
          </div>
        </Field>
      </div>

      <div className="field-row tri" style={{ marginTop: 12 }}>
        <Field label="Encryption password" help="10+ chars with upper+lower case, a digit and a symbol. The file is encrypted with it before leaving this machine — the host can never read it.">
          <input className="input mono" type="password" value={password} onChange={e => setPassword(e.target.value)}/>
        </Field>
        <Field label="Password hint (optional)" help="Shown back to you if you mistype the password later. Only you ever see it.">
          <input className="input" maxLength={120} value={hint} onChange={e => setHint(e.target.value)}/>
        </Field>
        <Field label="TTL (days)" help="How long the host keeps your deposit before it may be purged. You can download it back any time before the TTL expires.">
          <input className="input" type="number" min={1} value={ttl} onChange={e => setTtl(e.target.value)}/>
        </Field>
      </div>
      <Disclosure id="deposit-advanced" label="Advanced — transport & transfer tuning">
      <div className="field-row" style={{ marginTop: 12 }}>
        <Field label="Transport" hint="stream = direct, encrypted P2P"
               help="stream sends encrypted chunks straight to the host; cloud_url stages the encrypted file on your linked cloud and hands the host a link.">
          <select className="input" value={transport} onChange={e => setTransport(e.target.value)}>
            <option value="stream">stream (P2P)</option>
            <option value="cloud_url">cloud_url</option>
          </select>
        </Field>
        <Field label="Transfer window (chunks)" hint="blank = node default"
               help="How many encrypted chunks may be in flight (sent but not yet acknowledged) for THIS deposit. Raise it on a fast link for more throughput; lower it to be gentle on memory. Each deposit can use its own value.">
          <input className="input" type="number" min={2} max={128} placeholder="node default"
                 value={window} onChange={e => setWindow(e.target.value)}/>
        </Field>
      </div>
      <div className="field-row tri" style={{ marginTop: 12 }}>
        <Field label="Chunk ack timeout (s)" hint="blank = node default"
               help="How long this deposit's sender waits for the host to acknowledge a chunk before pausing the transfer for retry. 5–300.">
          <input className="input" type="number" min={5} max={300} placeholder="node default"
                 value={ackT} onChange={e => setAckT(e.target.value)}/>
        </Field>
        <Field label="Transit retries" hint="blank = node default"
               help="How many automatic resume attempts this deposit gets after pauses or failures before it's marked failed-in-transit. 1–20.">
          <input className="input" type="number" min={1} max={20} placeholder="node default"
                 value={retries} onChange={e => setRetries(e.target.value)}/>
        </Field>
        <Field label="Offer timeout (s)" hint="fan-out only · blank = node default"
               help="In fan-out mode, how long to wait for any candidate host to accept this offer before it's withdrawn. 30–86400.">
          <input className="input" type="number" min={30} max={86400} placeholder="node default"
                 value={offerT} onChange={e => setOfferT(e.target.value)}/>
        </Field>
      </div>
      </Disclosure>

      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" disabled={busy} onClick={submit}><I.send size={14}/> Send offer</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

/* ── Password-gated action panels (download / resume / share-view) ── */
const DownloadPanel = ({ dep, onDone, onCancel }) => {
  const [password, setPassword] = React.useState("");
  const [path, setPath] = React.useState("");
  const [delAfter, setDelAfter] = React.useState(false);
  const [status, setStatus] = React.useState("");
  const pickSave = async () => {
    try {
      const d = await api.get("/local/foreign_storage/pick_save_file");
      if (d.path) setPath(d.path);
    } catch (e) { setStatus("Picker failed: " + (e.detail || e.message)); }
  };
  const go = async () => {
    if (!password || !path.trim()) { setStatus("Password and save path are required."); return; }
    try {
      await api.post(`/local/foreign_storage/retrieve/${encodeURIComponent(dep.deposit_id)}`,
        { password, save_to_path: path.trim(), delete_after_download: delAfter });
      try { localStorage.setItem("fsDownloaded:" + dep.deposit_id, "1"); } catch (_) {}
      onDone(`Download started → ${path.trim()}${delAfter ? " (delete-after enabled)" : ""}`);
    } catch (e) {
      const hint = e.status === 401 && dep.password_hint ? ` — your hint: “${dep.password_hint}”` : "";
      setStatus("Download failed: " + (e.detail || e.message) + hint);
    }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.download size={14}/>} tone="emerald" title={`Download — ${dep.filename || dep.deposit_id}`}>
      <div className="field-row">
        <Field label="Password"><input className="input mono" type="password" value={password} onChange={e => setPassword(e.target.value)}/></Field>
        <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
          <Field label="Save to (path on this machine)">
            <input className="input mono" style={{ minWidth: 260 }} value={path} onChange={e => setPath(e.target.value)}/>
          </Field>
          <button className="btn ghost" onClick={pickSave}><I.search size={14}/> Browse…</button>
        </div>
      </div>
      <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center" }}>
        <Chk on={delAfter} onChange={setDelAfter}/>
        <span style={{ fontSize: 13 }}>Delete from the host after a successful download</span>
      </div>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" onClick={go}><I.download size={14}/> Download</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

/* Decrypt a deposit that auto-rescue pulled to local disk as ciphertext. */
const DecryptPanel = ({ dep, onDone, onCancel }) => {
  const [password, setPassword] = React.useState("");
  const [path, setPath] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const pickSave = async () => {
    try {
      const d = await api.get("/local/foreign_storage/pick_save_file");
      if (d.path) setPath(d.path);
    } catch (e) { setStatus("Picker failed: " + (e.detail || e.message)); }
  };
  const go = async () => {
    if (!password) { setStatus("Password is required."); return; }
    setBusy(true);
    try {
      const r = await api.post(`/local/foreign_storage/decrypt_rescued/${encodeURIComponent(dep.deposit_id)}`,
        { password, save_to_path: path.trim() });
      onDone(`Decrypted → ${r.path || path.trim() || "rescue folder"}`);
    } catch (e) {
      const hint = e.status === 401 && dep.password_hint ? ` — your hint: “${dep.password_hint}”` : "";
      setStatus("Decrypt failed: " + (e.detail || e.message) + hint);
      setBusy(false);
    }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.lock size={14}/>} tone="cyan" title={`Decrypt — ${dep.filename || dep.deposit_id}`}>
      <div className="hint" style={{ marginBottom: 12 }}>
        This deposit was auto-rescued to local disk as an encrypted file. Enter its password to decrypt it.
      </div>
      <div className="field-row">
        <Field label="Password"><input className="input mono" type="password" value={password} onChange={e => setPassword(e.target.value)}/></Field>
        <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
          <Field label="Save to (blank = rescue folder)">
            <input className="input mono" style={{ minWidth: 260 }} value={path} onChange={e => setPath(e.target.value)}/>
          </Field>
          <button className="btn ghost" onClick={pickSave}><I.search size={14}/> Browse…</button>
        </div>
      </div>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" disabled={busy} onClick={go}><I.lock size={14}/> {busy ? "Decrypting…" : "Decrypt"}</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

/* Per-deposit auto-recovery override — the full Settings set for one deposit,
 * pre-filled from the effective config (this deposit's override merged over
 * the node defaults). Every element overrides the node default. */
const AutoRescuePanel = ({ dep, onDone, onCancel }) => {
  const ar = dep.auto_rescue || {};
  const [enabled, setEnabled] = React.useState(ar.enabled !== false);
  const [mode, setMode] = React.useState(ar.mode || "folder_then_cloud");
  const [trigger, setTrigger] = React.useState(ar.trigger || "eviction");
  const [days, setDays] = React.useState(ar.days || 2);
  const [cloudCred, setCloudCred] = React.useState(ar.cloud_cred || "");
  const [targets, setTargets] = React.useState((ar.rclone_targets || []).join("\n"));
  const [dir, setDir] = React.useState(ar.dir || "");
  const [creds, setCreds] = React.useState([]);
  const [status, setStatus] = React.useState("");
  React.useEffect(() => {
    api.get("/local/foreign_storage/cloud_credentials").then(r => setCreds((r && r.credentials) || [])).catch(() => {});
  }, []);
  const usesFolder = mode !== "cloud_only";
  const usesCloud = mode !== "folder_only";
  const save = async (useDefault) => {
    try {
      const body = useDefault ? { use_default: true } : {
        enabled, mode, trigger, days: +days, cloud_cred: cloudCred,
        rclone_targets: targets.split("\n").map(s => s.trim()).filter(Boolean),
        dir: dir.trim(),
      };
      await api.post(`/local/foreign_storage/auto_rescue_config/${encodeURIComponent(dep.deposit_id)}`, body);
      onDone(useDefault ? "Reverted to node default" : "Auto-recovery settings saved");
    } catch (e) { setStatus("Save failed: " + (e.detail || e.message || "")); }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.shield size={14}/>} tone="amber" title={`Auto-recovery — ${dep.filename || dep.deposit_id}`}>
      <div className="hint" style={{ marginBottom: 12 }}>
        {ar.is_override
          ? "This deposit uses its own custom auto-recovery settings (it ignores the node default in Settings)."
          : "This deposit follows the node default from Settings. Change anything below and Save to give it its own settings."}
      </div>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <Toggle on={enabled} onChange={setEnabled}/>
        <span style={{ fontSize: 13 }}>Auto-recover this deposit</span>
      </div>
      {enabled && (
        <div style={{ marginTop: 12 }}>
          <div className="field-row">
            <Field label="When to act">
              <select className="input" value={trigger} onChange={e => setTrigger(e.target.value)}>
                <option value="eviction">When the host starts evicting</option>
                <option value="days">Also before TTL expires</option>
              </select>
            </Field>
            {trigger === "days" && (
              <Field label="Days before TTL" hint="1–30">
                <input className="input" type="number" min={1} max={30} value={days} onChange={e => setDays(+e.target.value)}/>
              </Field>
            )}
          </div>
          <div className="field-row" style={{ marginTop: 12 }}>
            <Field label="Recovery destination & order">
              <select className="input" value={mode} onChange={e => setMode(e.target.value)}>
                <option value="folder_then_cloud">Local folder, then cloud if full</option>
                <option value="cloud_then_folder">Cloud, then local folder if it fails</option>
                <option value="folder_only">Local folder only</option>
                <option value="cloud_only">Cloud only</option>
              </select>
            </Field>
            {usesFolder && (
              <Field label="Rescue folder (blank = node default)">
                <input className="input mono" value={dir} onChange={e => setDir(e.target.value)} placeholder="blank = node default"/>
              </Field>
            )}
          </div>
          {usesCloud && (
            <div style={{ marginTop: 12 }}>
              <Field label="Cloud credential (optional)"
                     hint={creds.length ? "host streams the encrypted bundle to your bucket — no password needed" : "none saved — add one in Foreign Storage, or use rclone below"}>
                <select className="input" value={cloudCred} onChange={e => setCloudCred(e.target.value)} disabled={!creds.length}>
                  <option value="">Use rclone targets below</option>
                  {creds.map(c => <option key={c.id} value={c.id}>{c.label || c.provider} — {c.provider}</option>)}
                </select>
              </Field>
              {!cloudCred && (
                <Field label="Cloud targets — rclone (one per line, fallback order)"
                       hint="streamed straight to your cloud · blank = node default">
                  <textarea className="input mono" rows={3} style={{ resize: "vertical", fontSize: 12 }}
                            placeholder={"gdrive:nexus/rescued\nwasabi:backups/nexus"}
                            value={targets} onChange={e => setTargets(e.target.value)}/>
                </Field>
              )}
            </div>
          )}
        </div>
      )}
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" onClick={() => save(false)}><I.check size={14}/> Save custom settings</button>
        {/* Only meaningful once a custom override exists — clears it so the
            deposit follows the node default again. */}
        {ar.is_override && <button className="btn ghost" onClick={() => save(true)} title="Remove this deposit's custom settings and follow the node default">Reset to node default</button>}
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

const ResumePanel = ({ dep, onDone, onCancel }) => {
  const [password, setPassword] = React.useState("");
  const [path, setPath] = React.useState("");
  const [status, setStatus] = React.useState("");
  const pick = async () => {
    try {
      const d = await api.get("/local/foreign_storage/pick_file");
      if (d.path) setPath(d.path);
    } catch (e) { setStatus("Picker failed: " + (e.detail || e.message)); }
  };
  const go = async () => {
    if (!password || !path.trim()) { setStatus("Password and the original file are required."); return; }
    try {
      await api.post(`/local/foreign_storage/resume/${encodeURIComponent(dep.deposit_id)}`, { password, file_path: path.trim() });
      onDone("Resume armed — only the missing chunks will be re-sent.");
    } catch (e) { setStatus("Resume failed: " + (e.detail || e.message)); }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.refresh size={14}/>} tone="amber" title={`Resume — ${dep.filename || dep.deposit_id}`}>
      <div className="hint" style={{ marginBottom: 10 }}>
        Transfer paused at {dep.transferred_chunks || 0}/{dep.chunk_count || "?"} chunks. Re-enter the password and point at the original file.
      </div>
      <div className="field-row">
        <Field label="Password"><input className="input mono" type="password" value={password} onChange={e => setPassword(e.target.value)}/></Field>
        <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
          <Field label="Original file path">
            <input className="input mono" style={{ minWidth: 260 }} value={path} onChange={e => setPath(e.target.value)}/>
          </Field>
          <button className="btn ghost" onClick={pick}><I.search size={14}/> Browse…</button>
        </div>
      </div>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" onClick={go}><I.refresh size={14}/> Resume</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

const SharePanel = ({ dep, onDone, onCancel }) => {
  const [password, setPassword] = React.useState("");
  const [status, setStatus] = React.useState("");
  const go = async () => {
    if (!password) { setStatus("Password is required."); return; }
    try {
      await api.post(`/local/foreign_storage/grant_view/${encodeURIComponent(dep.deposit_id)}`, { password });
      onDone("Share view sent to the host.");
    } catch (e) {
      const hint = e.status === 401 && dep.password_hint ? ` — your hint: “${dep.password_hint}”` : "";
      setStatus("Share failed: " + (e.detail || e.message) + hint);
    }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.eye size={14}/>} tone="amber" title={`Share view — ${dep.filename || dep.deposit_id}`}>
      <div className="banner info" style={{ marginBottom: 12 }}>
        <I.info size={14}/>
        <span><strong>This is permanent.</strong> The host gains the right to open the file, and opening writes plaintext to their disk. There is no revoke.</span>
      </div>
      <Field label="Password"><input className="input mono" type="password" style={{ maxWidth: 280 }} value={password} onChange={e => setPassword(e.target.value)}/></Field>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" onClick={go}><I.eye size={14}/> Share view permanently</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

const CloudEvictPanel = ({ dep, onDone, onCancel }) => {
  const [creds, setCreds] = React.useState(null);
  const [credId, setCredId] = React.useState("");
  const [dest, setDest] = React.useState("");
  const [status, setStatus] = React.useState("");
  React.useEffect(() => {
    api.get("/local/foreign_storage/cloud_credentials").then(d => {
      const c = (d && d.credentials) || [];
      setCreds(c);
      if (c.length) setCredId(String(c[0].id));
    }).catch(() => setCreds([]));
  }, []);
  const go = async () => {
    try {
      await api.post(`/local/foreign_storage/evict_to_cloud/${encodeURIComponent(dep.deposit_id)}`,
        { credential_id: credId, cloud_dest: dest });
      onDone("Cloud eviction started.");
    } catch (e) { setStatus("Evict failed: " + (e.detail || e.message)); }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.cloud size={14}/>} tone="cyan" title={`Evict to cloud — ${dep.filename || dep.deposit_id}`}>
      {creds === null && <div className="hint">Loading credentials…</div>}
      {creds && creds.length === 0 && <div className="hint">No cloud credentials configured — add one in the Cloud credentials card below first.</div>}
      {creds && creds.length > 0 && (
        <div className="field-row">
          <Field label="Credential">
            <select className="input" value={credId} onChange={e => setCredId(e.target.value)}>
              {creds.map(c => <option key={c.id} value={c.id}>{c.provider} — {c.label || "(no label)"}</option>)}
            </select>
          </Field>
          <Field label="Destination override (optional)">
            <input className="input mono" value={dest} onChange={e => setDest(e.target.value)}/>
          </Field>
        </div>
      )}
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" disabled={!credId} onClick={go}><I.cloud size={14}/> Evict</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

const HostEvictPanel = ({ dep, onDone, onCancel, onMessage }) => {
  const [days, setDays] = React.useState(3);
  const [status, setStatus] = React.useState("");
  const t = ttlInfo(dep);
  const stillInTtl = t && !t.expired;
  const go = async () => {
    const total_days = parseInt(days, 10);
    if (!total_days || total_days < 1) { setStatus("Minimum 1 day."); return; }
    try {
      await api.post(`/local/foreign_storage/eviction/${encodeURIComponent(dep.deposit_id)}`, { total_days });
      onDone(`Eviction scheduled — auto-deletes in ${total_days} day(s); the depositor is notified so they can download first.`);
    } catch (e) { setStatus("Eviction failed: " + (e.detail || e.message)); }
  };
  return (
    <PanelShell onClose={onCancel} icon={<I.trash size={14}/>} tone="rose" title={`Evict hosted deposit — ${dep.filename || dep.deposit_id}`}>
      <div className="hint" style={{ marginBottom: 10 }}>
        Gives the depositor a download window, then the data is purged from this node. Other deposits are unaffected.
      </div>
      {stillInTtl && (
        <div className="banner danger" style={{ marginBottom: 12 }}>
          <I.alertT size={14}/>
          <span>This deposit's TTL hasn't expired yet — the depositor still expects it kept for {fmtRemain(t.rem)}.
            Consider messaging them before evicting early.</span>
        </div>
      )}
      <Field label="Window (days)" hint="minimum 1">
        <input className="input" type="number" min={1} style={{ maxWidth: 140 }} value={days} onChange={e => setDays(e.target.value)}/>
      </Field>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
      <div className="row" style={{ gap: 10, marginTop: 14 }}>
        <button className="btn accent" onClick={go}>Schedule eviction</button>
        {onMessage && dep.depositor_uuid &&
          <button className="btn ghost" onClick={() => onMessage(dep.depositor_uuid)}><I.send size={14}/> Message depositor</button>}
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
    </PanelShell>
  );
};

/* ── Cloud credentials manager (Wave 6) — used by evict-to-cloud. ── */
const CloudCredsCard = ({ flash }) => {
  const [creds, setCreds] = React.useState([]);
  const [adding, setAdding] = React.useState(false);
  const [f, setF] = React.useState({ provider: "gdrive", label: "", credential_json: "", default_folder: "" });
  const load = React.useCallback(() => {
    api.get("/local/foreign_storage/cloud_credentials").then(d => setCreds((d && d.credentials) || [])).catch(() => {});
  }, []);
  React.useEffect(() => { load(); }, [load]);
  const add = async () => {
    try {
      await api.post("/local/foreign_storage/cloud_credentials", f);
      flash("Credential saved ✓");
      setAdding(false); setF({ provider: "gdrive", label: "", credential_json: "", default_folder: "" });
      load();
    } catch (e) { flash("Credential failed: " + (e.detail || e.message || "")); }
  };
  const del = async (id) => {
    try { await api.del(`/local/foreign_storage/cloud_credentials/${encodeURIComponent(id)}`); flash("Credential removed ✓"); load(); }
    catch (e) { flash("Remove failed: " + (e.detail || e.message || "")); }
  };
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <CardHead icon={<I.cloud size={14}/>} tone="cyan" title="Cloud credentials"
                meta={<span className="hint">for evict-to-cloud · secrets encrypted at rest, never shown again</span>}>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => setAdding(!adding)}>
          <I.plus size={13}/> Add credential
        </button>
      </CardHead>
      {adding && (
        <div className="col" style={{ gap: 10, padding: "10px 14px", borderBottom: "1px solid var(--br)" }}>
          <div className="field-row tri">
            <Field label="Provider" hint="gdrive is live; s3/r2/b2 are coming">
              <select className="input" value={f.provider} onChange={e => setF({ ...f, provider: e.target.value })}>
                <option value="gdrive">Google Drive</option>
                <option value="s3">Amazon S3</option>
                <option value="r2">Cloudflare R2</option>
                <option value="b2">Backblaze B2</option>
              </select>
            </Field>
            <Field label="Label"><input className="input" maxLength={60} value={f.label} onChange={e => setF({ ...f, label: e.target.value })}/></Field>
            <Field label="Default folder (optional)"><input className="input mono" value={f.default_folder} onChange={e => setF({ ...f, default_folder: e.target.value })}/></Field>
          </div>
          <Field label="Credential JSON" hint="e.g. a Google service-account key file's contents — validated before saving">
            <textarea className="input mono" rows={4} style={{ resize: "vertical", fontSize: 11 }} value={f.credential_json}
                      onChange={e => setF({ ...f, credential_json: e.target.value })}/>
          </Field>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn accent sm" disabled={!f.credential_json.trim()} onClick={add}><I.check size={13}/> Save</button>
            <button className="btn ghost sm" onClick={() => setAdding(false)}>Cancel</button>
          </div>
        </div>
      )}
      {creds.length === 0 && !adding && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No cloud credentials saved.</div>}
      {creds.length > 0 && (
        <table className="t">
          <thead><tr><th>Provider</th><th>Label</th><th>Folder</th><th>Added</th><th>Last used</th><th></th></tr></thead>
          <tbody>
            {creds.map(c => (
              <tr key={c.id}>
                <td className="mono" style={{ fontSize: 12 }}>{c.provider}</td>
                <td style={{ fontSize: 13 }}>{c.label || "—"}</td>
                <td className="mono dim" style={{ fontSize: 11 }}>{c.default_folder || "—"}</td>
                <td className="mono dim" style={{ fontSize: 11 }}>{(c.created_at || "").slice(0, 10)}</td>
                <td className="mono dim" style={{ fontSize: 11 }}>{(c.last_used_at || "").slice(0, 10) || "never"}</td>
                <td style={{ textAlign: "right" }}>
                  <Danger label="Remove" confirmLabel="Remove credential?" onFire={() => del(c.id)}/>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

const IN_TRANSIT = ["offered", "accepted", "transferring"];

const StorageScreen = ({ initialArg = null, onMessage }) => {
  const [quota, setQuota] = React.useState({});
  const [peers, setPeers] = React.useState([]);
  const [incoming, setIncoming] = React.useState([]);
  const [hosted, setHosted] = React.useState([]);
  const [mine, setMine] = React.useState([]);
  const [histories, setHistories] = React.useState([]);
  const [prog, setProg] = React.useState({});   // deposit_id -> {pct, bytesNow, speed, role, ts}
  const [panel, setPanel] = React.useState(() => initialArg === "new" ? { type: "deposit" } : null); // {type, dep?}

  const load = React.useCallback(async () => {
    const [q, pc, inc, h, my, hist] = await Promise.all([
      api.get("/local/foreign_storage/quota").catch(() => ({})),
      api.get("/local/foreign_storage/peer_capacities").catch(() => ({})),
      api.get("/local/foreign_storage/incoming").catch(() => ({})),
      api.get("/local/foreign_storage/hosted").catch(() => ({})),
      api.get("/local/foreign_storage/my_deposits").catch(() => ({})),
      api.get("/local/foreign_storage/histories").catch(() => ({})),
    ]);
    setQuota(q || {});
    setPeers((pc && pc.peers) || []);
    setIncoming((inc && inc.offers) || []);
    setHosted((h && h.deposits) || []);
    setMine((my && my.deposits) || []);
    setHistories((hist && hist.histories) || []);
  }, []);

  React.useEffect(() => {
    load();
    const id = setInterval(load, 7000);
    return () => clearInterval(id);
  }, [load]);

  // Live progress: update a bar + ETA per deposit off the SSE event stream.
  React.useEffect(() => {
    const unsub = subscribeEvents((ev) => {
      const type = String((ev && ev.type) || "");
      if (type === "storage_transfer_progress" && ev.deposit_id) {
        const total = ev.total || 0;
        const idx = (ev.sent_idx != null ? ev.sent_idx : ev.received_idx) || 0;
        setProg(p => ({
          ...p,
          [ev.deposit_id]: {
            pct: total ? Math.min(100, Math.round((idx / total) * 100)) : 0,
            bytesNow: (ev.bytes_sent != null ? ev.bytes_sent : ev.bytes_received) || 0,
            speed: ev.speed_bps || 0,
            role: ev.role || "",
            ts: Date.now(),
          },
        }));
      } else if (type === "storage_deposit_completed" && ev.deposit_id) {
        setProg(p => { const n = { ...p }; delete n[ev.deposit_id]; return n; });
        load();
      }
    });
    return unsub;
  }, [load]);

  const flash = notify;   // bell-only — no toast popups (project rule)
  const act = async (label, fn) => {
    try { await fn(); flash(label + " ✓"); await load(); }
    catch (e) { flash(label + " failed: " + (e.detail || e.message || "")); }
  };
  const panelDone = (text) => { setPanel(null); flash(text || "Done ✓"); load(); };

  const usedPct = quota.total_gb ? Math.min(100, (quota.used_gb || 0) / quota.total_gb * 100) : 0;
  const availHosts = peers.filter(p => p.available && p.accepting);
  const otherHosts = peers.filter(p => !(p.available && p.accepting));

  /* Status-aware depositor actions. Compact: icon buttons with tooltips for
   * the common verbs (download / decrypt / evict / share / message / delete),
   * so the row stays readable. Destructive actions still confirm on first tap. */
  const iconBtn = (key, icon, title, onClick, accent = false) => (
    <button key={key} className={"icon-btn" + (accent ? " accent" : "")} title={title} onClick={onClick}>{icon}</button>
  );
  const mineActions = (d) => {
    const s = String(d.status || "");
    const out = [];
    const did = encodeURIComponent(d.deposit_id);
    const cancel = <Danger key="c" icon={<I.x size={14}/>} title="Cancel deposit" label="Cancel" confirmLabel="Withdraw & purge?" onFire={() => act("Cancelled", () => api.post(`/local/foreign_storage/delete/${did}`))}/>;
    const ar = d.auto_rescue || {};
    const autoBtn = iconBtn("ar",
      <I.shield size={14} style={{ color: ar.enabled ? "var(--emerald)" : "var(--t-mute)" }}/>,
      `Auto-recovery: ${ar.enabled ? "on" : "off"}${ar.is_override ? " (custom)" : ""} — click to configure`,
      () => setPanel({ type: "autoRescue", dep: d }));
    if (s === "queued_offline" || s === "offering_multi" || IN_TRANSIT.includes(s)) {
      out.push(cancel);
    } else if (s.startsWith("paused_")) {
      out.push(iconBtn("r", <I.play size={14}/>, "Resume transfer", () => setPanel({ type: "resume", dep: d })), cancel);
    } else if (s === "eviction_requested" || s === "in_db_grace") {
      out.push(iconBtn("dl", <I.download size={14}/>, "Download now", () => setPanel({ type: "download", dep: d }), true));
      out.push(autoBtn);
      if (onMessage && d.host_uuid) out.push(iconBtn("msg", <I.send size={14}/>, "Message host", () => onMessage(d.host_uuid)));
    } else if (s === "rescued_encrypted") {
      out.push(iconBtn("dec", <I.lock size={14}/>, "Decrypt rescued copy", () => setPanel({ type: "decrypt", dep: d }), true));
      out.push(<Danger key="del" icon={<I.trash size={14}/>} title="Delete" confirmLabel="Discard the rescued copy?" onFire={() => act("Deleted", () => api.post(`/local/foreign_storage/delete/${did}`))}/>);
    } else {
      out.push(iconBtn("dl", <I.download size={14}/>, "Download", () => setPanel({ type: "download", dep: d })));
      out.push(autoBtn);
      out.push(iconBtn("ev", <I.cloud size={14}/>, "Evict to cloud", () => setPanel({ type: "cloudEvict", dep: d })));
      const stamp = Number(d.host_view_granted_at || 0);
      if (stamp > 0) out.push(<Pill key="sh" tone="cyan">shared</Pill>);
      else if (stamp === -1) out.push(<Pill key="sh" tone="amber">share pending</Pill>);
      else out.push(iconBtn("sh", <I.share size={14}/>, "Share view with host", () => setPanel({ type: "share", dep: d })));
      let downloaded = false;
      try { downloaded = localStorage.getItem("fsDownloaded:" + d.deposit_id) === "1"; } catch (_) {}
      out.push(<Danger key="del" icon={<I.trash size={14}/>} title="Delete" confirmLabel={downloaded ? "Delete permanently?" : "Never downloaded — destroy?"}
                       onFire={() => act("Deleted", () => api.post(`/local/foreign_storage/delete/${did}`))}/>);
    }
    return out;
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Foreign storage</div>
          <div className="page-sub">Host encrypted deposits for paired peers, or deposit yours on theirs. The host never sees plaintext.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
          <button className="btn accent" onClick={() => setPanel({ type: "deposit" })}><I.plus size={14}/> New deposit</button>
        </div>
      </div>

      {(() => {
        const warn = [...mine, ...hosted]
          .filter(d => !TERMINAL.includes(d.status))
          .map(d => ({ d, t: ttlInfo(d) }))
          .filter(x => x.t && (x.t.near || x.t.expired));
        if (!warn.length) return null;
        return (
          <div className="banner danger" style={{ marginBottom: 16 }}>
            <I.alertT size={14}/>
            <span>{warn.length === 1
              ? `“${warn[0].d.filename}” ${warn[0].t.expired ? "has reached its TTL" : fmtRemain(warn[0].t.rem)} — download or evict it before the host purges it.`
              : `${warn.length} deposits are at or near their TTL — download or evict them before the host purges them.`}</span>
          </div>
        );
      })()}

      {panel && panel.type === "deposit" && <DepositPanel flash={flash} onDone={() => { setPanel(null); load(); }} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "download" && <DownloadPanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "decrypt" && <DecryptPanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "autoRescue" && <AutoRescuePanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "resume" && <ResumePanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "share" && <SharePanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "cloudEvict" && <CloudEvictPanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)}/>}
      {panel && panel.type === "hostEvict" && <HostEvictPanel dep={panel.dep} onDone={panelDone} onCancel={() => setPanel(null)} onMessage={onMessage}/>}

      <div className="split-2" style={{ marginBottom: 16 }}>
        <div className="card pad-lg">
          <div className="row" style={{ marginBottom: 14 }}>
            <div className="ico-tile emerald" style={{ width: 30, height: 30 }}><I.hdd size={15}/></div>
            <div className="grow" style={{ flex: 1 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Hosted quota</div>
              <div className="hint">per-depositor cap {gb(quota.per_depositor_gb)}</div>
            </div>
            <div className="mono name" style={{ fontSize: 15 }}>{gb(quota.used_gb)} / {gb(quota.total_gb)}</div>
          </div>
          <Bar value={usedPct} color="var(--cyan)" lg/>
          <div className="row" style={{ marginTop: 12, justifyContent: "space-between" }}>
            <span className="hint">{hosted.length} hosted · {gb(quota.free_gb)} free</span>
            <Pill tone={quota.accepting ? "emerald" : "amber"} dot>{quota.accepting ? "accepting offers" : "not accepting"}</Pill>
          </div>
        </div>

        <div className="card">
          <CardHead icon={<I.send size={14}/>} tone="amber" title="Incoming offers"
                    meta={<Pill tone={incoming.length ? "amber" : "ghost"}>{incoming.length} pending</Pill>}/>
          {incoming.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No pending offers.</div>}
          {incoming.map((o, i) => (
            <div key={i} className="rail-item">
              <div className="rail-icon"><I.user size={14}/></div>
              <div className="rail-text">
                <div className="mono" style={{ fontSize: 12 }}>{o.depositor_display_name || o.depositor_uuid} → you</div>
                <div className="rail-sub">{o.filename} · {bytes(o.total_bytes)} · {o.chunk_count} chunks{o.ttl_days ? " · ttl " + o.ttl_days + "d" : ""}</div>
              </div>
              <div className="row" style={{ gap: 6 }}>
                <Danger label="Decline" onFire={() => act("Declined", () => api.post(`/local/foreign_storage/respond/${encodeURIComponent(o.deposit_id)}`, { action: "decline" }))}/>
                <Danger accent label="Accept" confirmLabel="Sign host terms & store?"
                        onFire={() => act("Accepted — receiving", () => api.post(`/local/foreign_storage/respond/${encodeURIComponent(o.deposit_id)}`, { action: "accept", host_tc_signed: true }))}/>
              </div>
            </div>
          ))}
          {incoming.length > 0 && (
            <div className="hint" style={{ padding: "4px 16px 12px" }}>
              Accepting signs the host terms: you store the encrypted bytes until the TTL and can't read the content.
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.shield size={14}/>} tone="emerald" title="Available hosts" meta={<span>{availHosts.length} accepting · sorted by free space</span>}/>
        {availHosts.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No peers are currently accepting deposits.</div>}
        {availHosts.length > 0 && (
          <table className="t">
            <thead><tr><th>Peer</th><th>Free</th><th>Pledge</th><th>Status</th></tr></thead>
            <tbody>
              {availHosts.map((p, i) => (
                <tr key={i}>
                  <td className="mono name">{p.display_name || p.peer_uuid}</td>
                  <td className="mono name">{gb(p.free_gb)}</td>
                  <td className="mono">{gb(p.pledge_gb)}</td>
                  <td><Pill tone="emerald" dot>online</Pill></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {otherHosts.length > 0 && (
          <div className="hint" style={{ padding: "10px 16px", borderTop: "1px solid var(--br-mute)" }}>
            {otherHosts.length} other peer{otherHosts.length === 1 ? "" : "s"} offline or opted-out (not selectable).
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.upload size={14}/>} tone="cyan" title="My deposits (outgoing)" meta={<span>{mine.length}</span>}/>
        {mine.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No active deposits — click <strong>New deposit</strong> above.</div>}
        {mine.length > 0 && (
          <table className="t">
            <thead><tr><th>Deposit</th><th>Host</th><th>Status</th><th>Bytes</th><th>TTL</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
            <tbody>
              {mine.map((dpt, i) => {
                const t = TERMINAL.includes(dpt.status) ? null : ttlInfo(dpt);
                return (
                <tr key={i}>
                  <td className="name">{dpt.filename}<div className="hint mono" style={{ fontWeight: 400 }}>deposited {fmtAgo(dpt.created_at)}</div></td>
                  <td className="mono">{dpt.host_display_name || ""}</td>
                  <td>
                    <Pill tone={statusTone(dpt.status)} dot>{dpt.status}</Pill>
                    {(dpt.status === "eviction_requested" || dpt.status === "in_db_grace") &&
                      <div className="hint" style={{ marginTop: 4, color: "var(--rose, #fb7185)" }}>host is evicting — download before the window closes</div>}
                    <ProgressLine p={prog[dpt.deposit_id]} totalBytes={dpt.total_bytes}/>
                  </td>
                  <td className="mono">{bytes(dpt.total_bytes)}</td>
                  <td className="mono" style={t && (t.near || t.expired) ? { color: "var(--rose, #fb7185)" } : undefined}>
                    {t ? fmtRemain(t.rem) : "—"}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>{mineActions(dpt)}</div>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <CardHead icon={<I.box size={14}/>} tone="purple" title="Hosting (incoming)" meta={<span>{hosted.length}</span>}/>
          {hosted.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No deposits hosted.</div>}
          {hosted.length > 0 && (
            <table className="t">
              <thead><tr><th>Deposit</th><th>Depositor</th><th>Status</th><th>Bytes</th><th>TTL</th><th></th></tr></thead>
              <tbody>
                {hosted.map((dpt, i) => {
                  const t = TERMINAL.includes(dpt.status) ? null : ttlInfo(dpt);
                  return (
                  <tr key={i}>
                    <td className="name">{dpt.filename}<div className="hint mono" style={{ fontWeight: 400 }}>accepted {fmtAgo(dpt.created_at)}</div></td>
                    <td className="mono">{dpt.depositor_display_name || ""}</td>
                    <td>
                      <Pill tone={statusTone(dpt.status)} dot>{dpt.status}</Pill>
                      <ProgressLine p={prog[dpt.deposit_id]} totalBytes={dpt.total_bytes}/>
                    </td>
                    <td className="mono">{bytes(dpt.total_bytes)}</td>
                    <td className="mono" style={t && (t.near || t.expired) ? { color: "var(--rose, #fb7185)" } : undefined}>
                      {t ? fmtRemain(t.rem) : "—"}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <div className="row" style={{ gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                        {Number(dpt.host_view_granted_at || 0) > 0 && (
                          <>
                            <button className="btn ghost sm" title="Depositor shared view — write the plaintext to disk and open the folder"
                                    onClick={() => act("Files opened", async () => {
                                      await api.post(`/local/foreign_storage/materialize_view/${encodeURIComponent(dpt.deposit_id)}`);
                                      await api.post(`/local/foreign_storage/open_shared_folder/${encodeURIComponent(dpt.deposit_id)}`);
                                    })}><I.eye size={13}/> Open files</button>
                            <Danger label="Delete plaintext" confirmLabel="Remove decrypted copy?"
                                    onFire={() => act("Plaintext removed", () => api.post(`/local/foreign_storage/delete_view_decrypted/${encodeURIComponent(dpt.deposit_id)}`))}/>
                          </>
                        )}
                        {(dpt.status === "eviction_requested" || dpt.status === "in_db_grace")
                          ? <Danger label="Cancel eviction" confirmLabel="Keep hosting?" onFire={() => act("Eviction cancelled", () => api.post(`/local/foreign_storage/cancel_eviction/${encodeURIComponent(dpt.deposit_id)}`))}/>
                          : <button className="btn ghost sm" onClick={() => setPanel({ type: "hostEvict", dep: dpt })}>Evict…</button>}
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          )}
      </div>

      <CloudCredsCard flash={flash}/>

      <div className="card">
        <CardHead icon={<I.list size={14}/>} tone="solid" title="Histories" meta={<span>terminal records</span>}/>
        {histories.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No history yet.</div>}
        {histories.length > 0 && (
          <table className="t">
            <thead><tr><th>Deposit</th><th>Role</th><th>Counterparty</th><th>Status</th><th>Bytes</th><th></th></tr></thead>
            <tbody>
              {histories.map((h, i) => (
                <tr key={i}>
                  <td className="name">{h.filename}</td>
                  <td className="mono">{h.role}</td>
                  <td className="mono">{h.counterparty_display_name || ""}</td>
                  <td><Pill tone={statusTone(h.status)} dot>{h.status}</Pill></td>
                  <td className="mono">{bytes(h.total_bytes)}</td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                      {h.role === "depositor" && ["declined", "failed_in_transit"].includes(h.status) &&
                        <button className="btn ghost sm" onClick={() => act("Offer re-sent", () => api.post(`/local/foreign_storage/resend_offer/${encodeURIComponent(h.deposit_id)}`))}>Re-send</button>}
                      <Danger label="Forget" onFire={() => act("Forgotten", () => api.post(`/local/foreign_storage/purge/${encodeURIComponent(h.deposit_id)}`))}/>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
};

export { StorageScreen };
