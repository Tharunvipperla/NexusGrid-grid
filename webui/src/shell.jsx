/* Sidebar + Topbar + global Shell. Wired to live node data via props. */
import React from "react";
import { I } from "./icons.jsx";
import { api } from "./api.js";
import { Avatar, Bar, Field, Modal } from "./components.jsx";
import { fmtBadge, fmtAgo } from "./notify.js";
import { getToastLog, clearToastLog, toast } from "./toast.jsx";

const BELL_SNOOZE_KEY = "nexus-bell-snooze";

/* Gate before applying an auto-update. A release flagged `breaking` (schema/data
 * changes) gets a strong "back up first" warning; any update gets a lighter
 * reminder. Returns false if the user cancels. */
const confirmUpdate = (info) => {
  info = info || {};
  if (info.breaking) {
    return window.confirm(
      "⚠ This update may make breaking changes" +
        (info.breaking_note ? " — " + info.breaking_note : "") + ".\n\n" +
        "Before continuing, download a FULL backup (Local Config → Backup & restore) " +
        "and keep a copy of any custom plugins, in case something needs restoring.\n\n" +
        "Continue with the update?"
    );
  }
  return window.confirm(
    "The node will download the update and restart.\n\n" +
      "Tip: download a backup first (Local Config → Backup & restore) so your data " +
      "and plugins are safe.\n\nContinue?"
  );
};

/* This node's uploaded avatar (GET /local/avatar), letter fallback when none.
 * `bust` cache-busts after an upload. */
const NodeAvatar = ({ name, size = 28, bust = 0 }) => {
  const [ok, setOk] = React.useState(true);
  React.useEffect(() => { setOk(true); }, [bust]);
  return ok
    ? <img src={"/local/avatar?_=" + bust} alt="" onError={() => setOk(false)}
           style={{ width: size, height: size, borderRadius: "50%", objectFit: "cover", display: "block", border: "1px solid var(--br)" }}/>
    : <Avatar name={name || "node"} color="#60a5fa" size={size}/>;
};

/* ── Unified notification bell (FUTURE_NOTIFICATION_BELL.md v1) ──
 * Aggregates unattended incoming events: storage offers, pairing requests,
 * evictions on your deposits, tripwire alerts, chat invitations, and
 * service-access requests. Auto-mark-read on open (seen ids in
 * localStorage, garbage-collected when the event resolves). Clicking an
 * item routes to the owning screen. Toasts/action feedback stay separate. */
const BELL_SEEN_KEY = "nexus-bell-seen";
const BELL_DISMISSED_KEY = "nexus-bell-dismissed";
const bellStore = (key) => {
  try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch (_) { return {}; }
};
const bellSeen = () => bellStore(BELL_SEEN_KEY);
const bellDismissed = () => bellStore(BELL_DISMISSED_KEY);

/* B2 — "What's new". The bell only flags the newest release until you open the
 * panel (then it clears). The panel itself lives in the profile menu and is the
 * permanent home: every release we've ever seen is archived in localStorage so
 * old versions stay viewable across updates, and the user can delete notes. */
const WHATSNEW_SEEN_KEY = "nexus-whatsnew-seen";   // newest version acknowledged (clears the bell)
const WHATSNEW_LOG_KEY = "nexus-whatsnew-log";     // {version: {version,date,highlights}} — local archive
const WHATSNEW_DEL_KEY = "nexus-whatsnew-deleted"; // [version,...] — user-deleted, never re-added
const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
const whatsNewSeen = () => { try { return localStorage.getItem(WHATSNEW_SEEN_KEY) || ""; } catch (_) { return ""; } };
const markWhatsNewSeen = (v) => {
  try { localStorage.setItem(WHATSNEW_SEEN_KEY, String(v || "")); } catch (_) {}
  window.dispatchEvent(new Event("nexus-whatsnew"));   // tell the bell to drop its flag now
};
/* Newest-first version compare (numeric dotted parts). */
const verCmp = (a, b) => {
  const pa = String(a).split("."), pb = String(b).split(".");
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (parseInt(pb[i], 10) || 0) - (parseInt(pa[i], 10) || 0);
    if (d) return d;
  }
  return 0;
};
const readArchive = () => {
  const log = lsGet(WHATSNEW_LOG_KEY, {});
  const del = new Set(lsGet(WHATSNEW_DEL_KEY, []));
  return Object.values(log).filter(e => e && !del.has(e.version)).sort((a, b) => verCmp(a.version, b.version));
};
/* "2026-06-20" -> "20 Jun 2026"; passes anything non-ISO through unchanged. */
const WN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtRelDate = (s) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ""));
  if (!m) return s || "";
  const mo = +m[2];
  return mo >= 1 && mo <= 12 ? `${+m[3]} ${WN_MONTHS[mo - 1]} ${m[1]}` : s;
};

/* Release-notes panel — archives /local/whats_new locally. Shows ONE release's
 * full patch notes at a time; a dropdown at the bottom switches to older
 * versions. Notes are deletable (per-version or all). In the profile Settings. */
const WhatsNew = ({ onClose }) => {
  const [entries, setEntries] = React.useState(readArchive);
  const [current, setCurrent] = React.useState("");
  const [sel, setSel] = React.useState("");
  React.useEffect(() => {
    let dead = false;
    api.get("/local/whats_new").then(d => {
      if (dead || !d) return;
      const del = new Set(lsGet(WHATSNEW_DEL_KEY, []));
      const log = lsGet(WHATSNEW_LOG_KEY, {});
      for (const e of (d.entries || [])) if (e && !del.has(e.version)) log[e.version] = e;  // archive locally
      lsSet(WHATSNEW_LOG_KEY, log);
      setCurrent(d.current || "");
      if (d.latest) markWhatsNewSeen(d.latest);   // opening = acknowledged
      const list = readArchive();
      setEntries(list);
      // Default to the installed release if we have its notes, else the newest.
      setSel(list.some(e => e.version === d.current) ? d.current : (list[0] ? list[0].version : ""));
    }).catch(() => {});
    return () => { dead = true; };
  }, []);
  const del = (version) => {
    const d = new Set(lsGet(WHATSNEW_DEL_KEY, [])); d.add(version); lsSet(WHATSNEW_DEL_KEY, [...d]);
    const log = lsGet(WHATSNEW_LOG_KEY, {}); delete log[version]; lsSet(WHATSNEW_LOG_KEY, log);
    const next = entries.filter(e => e.version !== version);
    setEntries(next);
    if (sel === version) setSel(next[0] ? next[0].version : "");
  };
  const clearAll = () => {
    const d = new Set(lsGet(WHATSNEW_DEL_KEY, [])); entries.forEach(e => d.add(e.version)); lsSet(WHATSNEW_DEL_KEY, [...d]);
    lsSet(WHATSNEW_LOG_KEY, {}); setEntries([]); setSel("");
  };
  const e = entries.find(x => x.version === sel) || entries[0] || null;
  return (
    <Modal title="What's new" icon={<I.zap size={15}/>} tone="cyan" width={600} onClose={onClose}>
      {!e && <div className="hint">No release notes saved. New releases will appear here.</div>}
      {e && (
        <>
          {/* the selected release's actual patch notes */}
          <div className="row" style={{ gap: 8, alignItems: "baseline", marginBottom: 4 }}>
            <span className="mono" style={{ fontWeight: 700, fontSize: 15 }}>v{e.version}</span>
            {e.date && <span className="hint">{fmtRelDate(e.date)}</span>}
            {current === e.version && <span className="hint" style={{ color: "var(--emerald, #34d399)" }}>installed</span>}
            <button className="icon-btn" style={{ width: 24, height: 24, marginLeft: "auto" }}
                    title="Delete this version's notes" onClick={() => del(e.version)}><I.trash size={12}/></button>
          </div>
          <ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>
            {(e.highlights || []).map((h, i) => (
              <li key={i} style={{ fontSize: 12.5, lineHeight: 1.55, marginBottom: 4 }}>{h}</li>
            ))}
          </ul>

          {/* version picker at the bottom */}
          <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 18,
                                        paddingTop: 12, borderTop: "1px solid var(--br)" }}>
            <span className="label" style={{ margin: 0 }}>Other releases</span>
            <select className="input" style={{ width: 200 }} value={sel} onChange={ev => setSel(ev.target.value)}>
              {entries.map(o => (
                <option key={o.version} value={o.version}>
                  v{o.version}{o.version === current ? " (installed)" : ""}
                </option>
              ))}
            </select>
            <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={clearAll}
                    title="Delete all saved release notes">Clear all</button>
          </div>
        </>
      )}
    </Modal>
  );
};

const NotifyBell = ({ setRoute, onOpenWhatsNew }) => {
  const [items, setItems] = React.useState([]);
  const [open, setOpen] = React.useState(false);
  const [seenTick, setSeenTick] = React.useState(0);
  const [, setLogTick] = React.useState(0);
  const [wn, setWn] = React.useState(null);        // /local/whats_new payload (for the unseen flag)
  const wrapRef = React.useRef(null);

  React.useEffect(() => {
    const on = () => setLogTick(t => t + 1);
    window.addEventListener("nexus-toast-log", on);
    window.addEventListener("nexus-ui-settings-changed", on);
    return () => { window.removeEventListener("nexus-toast-log", on); window.removeEventListener("nexus-ui-settings-changed", on); };
  }, []);

  const refresh = React.useCallback(async () => {
    const out = [];
    const [inc, peers, mine, inv, svc, audit, upd, wnRes] = await Promise.all([
      api.get("/local/foreign_storage/incoming").catch(() => ({})),
      api.get("/local/peers").catch(() => ({})),
      api.get("/local/foreign_storage/my_deposits").catch(() => ({})),
      api.get("/local/invitations/incoming").catch(() => ({})),
      api.get("/local/service_requests").catch(() => ({})),
      api.get("/local/audit?limit=100").catch(() => ({})),
      api.get("/local/update/check").catch(() => ({})),
      api.get("/local/whats_new").catch(() => ({})),
    ]);
    for (const o of (inc.offers || [])) {
      // privacy: never surface password_hint here
      out.push({ id: "fs:" + o.deposit_id, route: "storage", icon: "box",
                 text: `Storage offer from ${o.depositor_display_name || o.depositor_uuid || "a peer"} — ${o.filename || "file"}` });
    }
    for (const p of (peers.peers || [])) {
      if (p.status === "trusted_pending_in") {
        out.push({ id: "pair:" + (p.peer_uuid || p.ip), route: "network", icon: "users",
                   text: `Pairing request from ${p.display_name || p.ip}` });
      }
    }
    for (const d of (mine.deposits || [])) {
      if (d.status === "eviction_requested" || d.status === "in_db_grace") {
        out.push({ id: "evict:" + d.deposit_id, route: "storage", icon: "alertT",
                   text: `Host is evicting “${d.filename || d.deposit_id}” — download before the window closes` });
      }
      if (d.status === "rescued_encrypted") {
        out.push({ id: "rescued:" + d.deposit_id, route: "storage", icon: "lock", tone: "cyan",
                   text: `Recovered “${d.filename || d.deposit_id}” to local disk — enter its password to decrypt` });
      }
    }
    for (const e of (audit.events || [])) {
      if (e.action === "storage.unauthorized_access_detected") {
        out.push({ id: `trip:${e.task_id}:${e.ts}`, route: "storage", icon: "shield", tone: "rose",
                   text: `Unauthorized access detected on deposit ${(e.task_id || "").slice(0, 8)}…` });
      }
      if (e.action === "storage.auto_rescue_failed") {
        // detail looks like "file=<name> reason=<slug> msg=<hint>"
        const det = String(e.detail || "");
        const fm = det.match(/file=([^ ]+)/);
        const rm = det.match(/reason=([^ ]+)/);
        const fname = fm ? fm[1] : (e.task_id || "a deposit");
        const reason = rm ? rm[1] : "";
        const why = reason.startsWith("cloud") ? "cloud eviction failed"
          : reason === "no_space" ? "not enough free disk space"
          : "rescue couldn't complete";
        out.push({ id: `rescue:${e.task_id}:${e.ts}`, route: "storage", icon: "alertT", tone: "rose",
                   text: `Couldn't auto-recover “${fname}” — ${why}. Act now or the file may be lost.` });
      }
    }
    for (const o of (inv.offers || [])) {
      if (!o.status || o.status === "pending") {
        out.push({ id: "inv:" + o.token, route: "messages", icon: "send",
                   text: `Chat invitation: “${o.group_name || o.group_id}”` });
      }
    }
    for (const r of (svc.requests || [])) {
      out.push({ id: "svc:" + (r.grant_id || r.id), route: "services", icon: "key",
                 text: `${r.consumer_name || (r.consumer_pubkey || "a peer").slice(0, 10)} requests access to ${r.service_name || "a service"}` });
    }
    if (upd && upd.available) {
      out.unshift({ id: "update:" + upd.latest, icon: "download",
                    text: `App update available — v${upd.latest}. Open your profile menu to update now.` });
    }
    if (wnRes && Array.isArray(wnRes.entries) && wnRes.entries.length) {
      setWn(wnRes);
      if (whatsNewSeen() !== wnRes.latest) {
        const n = (wnRes.entries[0].highlights || []).length;
        out.unshift({ id: "whatsnew:" + wnRes.latest, icon: "zap", tone: "cyan", whatsnew: true,
                      text: `What's new in v${wnRes.latest} — ${n} update${n === 1 ? "" : "s"}` });
      }
    }
    setItems(out.slice(0, 30));
    // GC seen/dismissed ids whose event resolved, so localStorage doesn't grow.
    const live = new Set(out.map(i => i.id));
    for (const key of [BELL_SEEN_KEY, BELL_DISMISSED_KEY]) {
      const cur = bellStore(key);
      const next = {};
      for (const id of Object.keys(cur)) if (live.has(id)) next[id] = 1;
      try { localStorage.setItem(key, JSON.stringify(next)); } catch (_) {}
    }
  }, []);

  React.useEffect(() => {
    refresh();
    const id = setInterval(refresh, 12000);
    window.addEventListener("nexus-whatsnew", refresh);   // panel opened -> drop the flag now
    return () => { clearInterval(id); window.removeEventListener("nexus-whatsnew", refresh); };
  }, [refresh]);

  React.useEffect(() => {
    if (!open) return;
    const handler = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Once you've opened the bell and closed it, the ephemeral "Recent activity"
  // (toasts like a shutdown notice) has been seen — clear it so it doesn't pile
  // up. Action items stay (they reflect live state and clear when resolved).
  const wasOpen = React.useRef(false);
  React.useEffect(() => {
    if (wasOpen.current && !open) clearToastLog();
    wasOpen.current = open;
  }, [open]);

  const seen = bellSeen();
  const dismissed = bellDismissed();
  const toastSeen = Number(localStorage.getItem("nexus-bell-toast-seen") || 0);
  const snoozeUntil = Number(localStorage.getItem(BELL_SNOOZE_KEY) || 0);
  const isSnoozed = snoozeUntil > Date.now();
  // Recent toasts replayed from the in-memory log (info/activity, no route).
  const logItems = getToastLog().map(t => ({
    id: "toast:" + t.id, kind: "toast", ts: t.ts,
    icon: t.tone === "danger" ? "alertT" : "check",
    tone: t.tone === "danger" ? "rose" : "",
    text: t.text,
  }));
  const notifOff = uiSettings().notifications === false;
  const visible = items.filter(i => !dismissed[i.id]);
  const unread = (isSnoozed || notifOff) ? 0
    : visible.filter(i => !seen[i.id]).length + logItems.filter(t => t.ts > toastSeen).length;
  const toggle = () => {
    if (!open) {
      // auto-mark-read on open (both action items and recent activity)
      const next = bellSeen();
      for (const i of items) next[i.id] = 1;
      try { localStorage.setItem(BELL_SEEN_KEY, JSON.stringify(next)); } catch (_) {}
      try { localStorage.setItem("nexus-bell-toast-seen", String(Date.now())); } catch (_) {}
      setSeenTick(t => t + 1);
    }
    setOpen(!open);
  };
  const dismiss = (id) => {
    const next = bellDismissed();
    next[id] = 1;
    try { localStorage.setItem(BELL_DISMISSED_KEY, JSON.stringify(next)); } catch (_) {}
    setSeenTick(t => t + 1);
  };
  const clearAll = () => {
    const nd = bellDismissed();
    for (const i of items) nd[i.id] = 1;
    try { localStorage.setItem(BELL_DISMISSED_KEY, JSON.stringify(nd)); } catch (_) {}
    clearToastLog();
    setSeenTick(t => t + 1);
  };
  const snooze = () => {
    try { localStorage.setItem(BELL_SNOOZE_KEY, String(Date.now() + 3600000)); } catch (_) {}
    setSeenTick(t => t + 1);
  };
  const unsnooze = () => {
    try { localStorage.removeItem(BELL_SNOOZE_KEY); } catch (_) {}
    setSeenTick(t => t + 1);
  };

  return (
    <div ref={wrapRef} style={{ position: "relative" }}>
      <button className="icon-btn" title={unread ? `${unread} new notification${unread === 1 ? "" : "s"}` : "Notifications"} onClick={toggle}>
        <I.bell size={15}/>
        {unread > 0 && (
          <span style={{ position: "absolute", top: -6, right: -6, minWidth: 18, height: 18, padding: "0 5px",
                         borderRadius: 999, display: "grid", placeItems: "center", fontSize: 10.5, fontWeight: 700,
                         lineHeight: 1, fontVariantNumeric: "tabular-nums", letterSpacing: "-0.02em",
                         background: "var(--accent)", color: "#fff",
                         boxShadow: "0 0 0 2px var(--bg-card), 0 1px 3px rgba(0,0,0,0.45)" }}>
            {fmtBadge(unread)}
          </span>
        )}
      </button>
      {open && (
        <div className="profile-menu" style={{ width: 360, right: 0, padding: 0, overflow: "hidden" }}>
          <div className="row" style={{ padding: "10px 14px 6px", alignItems: "center" }}>
            <span className="profile-section-label grow" style={{ padding: 0 }}>Notifications</span>
            {isSnoozed
              ? <button className="btn ghost sm" onClick={unsnooze} title={"Snoozed until " + new Date(snoozeUntil).toLocaleTimeString()}>Snoozed · resume</button>
              : <button className="btn ghost sm" onClick={snooze} title="Mute pop-ups for 1 hour">Snooze 1h</button>}
            {(visible.length > 0 || logItems.length > 0) && <button className="btn ghost sm" onClick={clearAll}>Clear</button>}
          </div>
          {visible.length === 0 && logItems.length === 0 && <div className="hint" style={{ padding: "4px 14px 12px" }}>Nothing waiting on you.</div>}
          <div style={{ maxHeight: 380, overflowY: "auto" }}>
            {visible.map(i => {
              const Icon = I[i.icon] || I.bell;
              return (
                <div key={i.id}
                     style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "10px 14px",
                              borderTop: "1px solid var(--br)", cursor: "pointer",
                              background: seen[i.id] ? "transparent" : "rgba(99,102,241,0.06)" }}
                     onClick={() => {
                       setOpen(false);
                       if (i.whatsnew) { onOpenWhatsNew && onOpenWhatsNew(); return; }  // panel marks it seen
                       if (i.route && setRoute) setRoute(i.route);
                     }}>
                  <span style={{ marginTop: 1, color: i.tone === "rose" ? "var(--rose, #fb7185)" : "var(--t-mute)" }}><Icon size={14}/></span>
                  <span style={{ fontSize: 12, lineHeight: 1.45, flex: 1 }}>{i.text}</span>
                  <button className="icon-btn" title="Dismiss" style={{ width: 22, height: 22, flexShrink: 0 }}
                          onClick={(e) => { e.stopPropagation(); dismiss(i.id); }}>
                    <I.x size={12}/>
                  </button>
                </div>
              );
            })}
            {logItems.length > 0 && (
              <>
                <div className="profile-section-label" style={{ padding: "10px 14px 4px" }}>Recent activity</div>
                {logItems.map(t => {
                  const Icon = I[t.icon] || I.bell;
                  return (
                    <div key={t.id}
                         style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "8px 14px",
                                  borderTop: "1px solid var(--br)",
                                  background: t.ts > toastSeen ? "rgba(99,102,241,0.06)" : "transparent" }}>
                      <span style={{ marginTop: 1, color: t.tone === "rose" ? "var(--rose, #fb7185)" : "var(--t-mute)" }}><Icon size={13}/></span>
                      <span style={{ fontSize: 12, lineHeight: 1.45, flex: 1 }}>{t.text}</span>
                      <span className="hint mono" style={{ fontSize: 10, flexShrink: 0 }}>{fmtAgo(t.ts)}</span>
                    </div>
                  );
                })}
              </>
            )}
          </div>
          {visible.length > 0 && (
            <div className="hint" style={{ padding: "8px 14px 10px", borderTop: "1px solid var(--br)" }}>
              Click a notification to open the screen where you can act on it.
            </div>
          )}
        </div>
      )}
    </div>
  );
};

/* Node profile editor: avatar (PNG/JPEG ≤2MB → /local/upload_avatar),
 * display name + about-me (PUT /local/profile). */
const ProfileModal = ({ node, onClose, bust, setBust }) => {
  const [name, setName] = React.useState("");
  const [about, setAbout] = React.useState("");
  const [msg, setMsg] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const fileRef = React.useRef(null);
  React.useEffect(() => {
    api.get("/local/profile").then(p => { setName(p.display_name || ""); setAbout(p.about_me || ""); }).catch(() => {});
  }, []);
  const upload = async (file) => {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { setMsg("Image too large — max 2 MB."); return; }
    const fd = new FormData();
    fd.append("file", file);
    try { await api.post("/local/upload_avatar", fd); setBust(Date.now()); setMsg("Picture updated ✓"); }
    catch (e) { setMsg("Picture failed: " + (e.detail || e.message || "")); }
  };
  const save = async () => {
    setBusy(true);
    try { await api.put("/local/profile", { display_name: name.trim(), about_me: about }); onClose(); }
    catch (e) { setMsg("Save failed: " + (e.detail || e.message || "")); setBusy(false); }
  };
  return (
    <Modal title="Node profile" icon={<I.users size={14}/>} tone="blue" onClose={onClose}
           foot={<>
             <button className="btn ghost" onClick={onClose}>Cancel</button>
             <button className="btn accent" disabled={busy} onClick={save}><I.check size={14}/> Save profile</button>
           </>}>
      <div className="row" style={{ gap: 14, alignItems: "center", marginBottom: 14 }}>
        <div style={{ cursor: "pointer" }} title="Change picture" onClick={() => fileRef.current && fileRef.current.click()}>
          <NodeAvatar name={node.name} size={64} bust={bust}/>
        </div>
        <div>
          <button className="btn ghost sm" onClick={() => fileRef.current && fileRef.current.click()}><I.upload size={13}/> Change picture</button>
          <div className="hint" style={{ marginTop: 4 }}>PNG or JPEG, up to 2 MB.</div>
        </div>
        <input ref={fileRef} type="file" accept="image/png,image/jpeg" style={{ display: "none" }}
               onChange={e => { upload(e.target.files && e.target.files[0]); e.target.value = ""; }}/>
      </div>
      <Field label="Display name" hint="shown to connected peers alongside your address">
        <input className="input" maxLength={50} placeholder="Enter your name…" value={name} onChange={e => setName(e.target.value)}/>
      </Field>
      <div style={{ marginTop: 10 }}>
        <Field label="About me" hint="appears on your node profile, visible to peers and co-members">
          <textarea className="input" rows={3} maxLength={1000} style={{ resize: "vertical" }}
                    value={about} onChange={e => setAbout(e.target.value)}/>
        </Field>
      </div>
      {msg && <div className="hint" style={{ marginTop: 10, color: msg.includes("✓") ? "var(--emerald, #34d399)" : "var(--rose, #fb7185)" }}>{msg}</div>}
    </Modal>
  );
};

/* Unread-count bubble: amber when something mentions you, accent otherwise. */
const NavBadge = ({ b }) => {
  if (!b) return null;
  // Warning badge (e.g. Foreign Storage near-TTL / eviction): an alert symbol,
  // not a count — stays until the underlying deposit is resolved.
  if (b.warn) {
    return (
      <span title="Needs attention — a deposit is near its TTL or being evicted"
            style={{ marginLeft: "auto", color: "var(--rose, #fb7185)", display: "inline-flex", alignItems: "center" }}>
        <I.alertT size={15}/>
      </span>
    );
  }
  if (!b.n) return null;
  return (
    <span style={{
      marginLeft: "auto", minWidth: 18, height: 18, padding: "0 5px",
      borderRadius: 9, display: "inline-grid", placeItems: "center",
      fontSize: 10, fontWeight: 700, fontFamily: "var(--f-mono)",
      background: b.mention ? "rgba(245,158,11,0.9)" : "var(--accent)",
      color: b.mention ? "#1a1206" : "#fff",
    }}>{fmtBadge(b.n)}</span>
  );
};

/* Nav grouped by the user's job, not by feature history:
 * "Use the grid" = spend resources, "My people" = who you share with,
 * "My node" = watch and run your own machine. */
const NAV = [
  { group: "My node", items: [
    { id: "overview",    label: "Overview",        icon: <I.layers size={16}/> },
    { id: "topology",    label: "Live Topology",   icon: <I.broadcast size={16}/> },
    { id: "security",    label: "Security Center", icon: <I.shield size={16}/> },
    { id: "diagnostics", label: "Diagnostics",     icon: <I.pulse size={16}/> },
    { id: "config",      label: "Local Config",    icon: <I.cog size={16}/> },
    { id: "plugins",     label: "Plugins",         icon: <I.terminal size={16}/> },
    { id: "api",         label: "API & docs",      icon: <I.book size={16}/> },
  ]},
  { group: "Use the grid", items: [
    { id: "dispatcher", label: "Dispatcher",      icon: <I.zap size={16}/> },
    { id: "telemetry",  label: "Task Telemetry",  icon: <I.list size={16}/> },
    { id: "storage",    label: "Foreign Storage", icon: <I.box size={16}/> },
    { id: "services",   label: "Services",        icon: <I.terminal size={16}/> },
  ]},
  { group: "My people", items: [
    { id: "groups",     label: "Groups",          icon: <I.users size={16}/> },
    { id: "messages",   label: "Messages",        icon: <I.send size={16}/> },
    { id: "network",    label: "Network Web",     icon: <I.share size={16}/> },
  ]},
];

const Sidebar = ({ route, setRoute, collapsed, node, navBadges = {}, onPower, latency = [] }) => {
  const online = !!node.online;
  const pct = (v) => (online && v != null ? Math.round(v) + "%" : "—");
  return (
    <aside className={"sidebar" + (collapsed ? " collapsed" : "") + (online ? " sb-online" : " sb-offline")}>
      {!collapsed && (
        <div className="node-card">
          <div className="node-row">
            <div className="node-status">This node · {online ? "Online" : "Offline"}</div>
          </div>
          <div className="node-name">{node.name || "this node"}</div>
          <div className="node-role">{node.addr || ""}</div>
          <div className="node-bars">
            <div className="node-bar-row"><span>CPU</span><Bar value={online ? (node.cpu || 0) : 0} threshold/><span>{pct(node.cpu)}</span></div>
            <div className="node-bar-row"><span>RAM</span><Bar value={online ? (node.ram || 0) : 0} threshold/><span>{pct(node.ram)}</span></div>
            <div className="node-bar-row"><span>GPU</span><Bar value={online ? (node.gpu || 0) : 0} threshold/><span>{node.gpu == null ? "—" : pct(node.gpu)}</span></div>
          </div>
          {latency.length > 0 && (
            <div className="node-relays">
              {latency.map((l) => (
                <div key={l.url} className="relay-row" title={l.url}>
                  <span className="relay-name">{l.label}</span>
                  <span className="relay-rtt mono">{l.rtt == null ? "—" : Math.round(l.rtt) + "ms"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {collapsed && (
        <div className="node-card-mini" title={(node.name || "this node") + " · " + (online ? "Online" : "Offline")}/>
      )}

      {NAV.map(grp => (
        <div key={grp.group}>
          {!collapsed && <div className="nav-label">{grp.group}</div>}
          {collapsed && <div className="nav-divider"></div>}
          {grp.items.map(it => {
            const b = navBadges[it.id];
            return (
              <div key={it.id}
                   className={"nav-item " + (route === it.id ? "active" : "")}
                   onClick={() => setRoute(it.id)}
                   title={collapsed ? it.label + (b && b.n ? ` (${fmtBadge(b.n)} new)` : "") : undefined}
                   style={{ position: "relative" }}>
                <span className="nav-icon">{it.icon}</span>
                {!collapsed && <span>{it.label}</span>}
                {!collapsed && <NavBadge b={b}/>}
                {collapsed && b && (b.warn || b.n > 0) && (
                  <span style={{ position: "absolute", top: 4, right: 4, width: 8, height: 8, borderRadius: "50%",
                                 background: b.warn ? "var(--rose, #fb7185)" : (b.mention ? "#fbbf24" : "var(--accent)") }}/>
                )}
              </div>
            );
          })}
        </div>
      ))}

      {/* Power control: shutting down stops serving the grid (running local
        * tasks preempt), closes the public tunnel, and stops the bundled
        * relay. Going back online just flips the node back to serving. */}
      <div style={{ marginTop: "auto", paddingTop: 12 }}>
        <button
          className="btn sm"
          style={{
            width: "100%", justifyContent: "center",
            borderColor: online ? "var(--rose)" : "var(--emerald)",
            color: online ? "var(--rose)" : "var(--emerald)",
          }}
          title={online ? "Go offline: stop serving, close the tunnel, stop the local relay" : "Resume serving the grid"}
          onClick={() => onPower && onPower(!online)}>
          <I.power size={13}/>{!collapsed && (online ? " Shut down" : " Go online")}
        </button>
      </div>
    </aside>
  );
};

/* App-wide UI preferences, stored client-side. Screens re-read on the
 * "nexus-ui-settings-changed" window event. */
export const UI_SETTINGS_KEY = "nexus-ui-settings";
export const uiSettings = () => {
  try { return { density: "comfortable", notifications: true, topoMaxNodes: 100, ...(JSON.parse(localStorage.getItem(UI_SETTINGS_KEY)) || {}) }; }
  catch (_) { return { density: "comfortable", notifications: true, topoMaxNodes: 100 }; }
};
const saveUiSettings = (patch) => {
  const next = { ...uiSettings(), ...patch };
  try { localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify(next)); } catch (_) {}
  window.dispatchEvent(new Event("nexus-ui-settings-changed"));
  return next;
};

const topoLoad = (n) => n <= 80 ? "light" : n <= 200 ? "moderate" : n <= 400 ? "heavy" : "very heavy";

const UiSettingsModal = ({ theme, setTheme, onClose, onWhatsNew }) => {
  const [s, setS] = React.useState(uiSettings);
  const upd = (patch) => setS(saveUiSettings(patch));
  const [topoN, setTopoN] = React.useState(s.topoMaxNodes || 100);
  const [topoConfirm, setTopoConfirm] = React.useState(false);
  const applyTopo = () => {
    const n = Math.max(10, Math.min(2000, Math.round(Number(topoN) || 100)));
    if (n > 200 && !topoConfirm) { setTopoConfirm(true); return; }   // heavy → ask first
    upd({ topoMaxNodes: n });
    setTopoN(n);
    setTopoConfirm(false);
  };
  const [ver, setVer] = React.useState("");
  const [up, setUp] = React.useState(null);     // {current, latest, available, notes_url}
  const [applying, setApplying] = React.useState(false);
  React.useEffect(() => {
    fetch("/health").then(r => r.json()).then(d => setVer(d.version || "")).catch(() => {});
    api.get("/local/update/check").then(setUp).catch(() => {});
  }, []);
  const doUpdate = async () => {
    if (!confirmUpdate(up)) return;
    setApplying(true);
    try { await api.post("/local/update/apply"); toast("Updating — downloading the new version; the node will restart.", "info"); }
    catch (e) { toast("Update failed: " + (e.detail || e.message || ""), "danger"); setApplying(false); }
  };
  return (
    <Modal title="Interface settings" icon={<I.cog size={15}/>} tone="cyan" width={520} onClose={onClose}>
      <div className="label" style={{ marginBottom: 8 }}>Theme</div>
      <div className="seg" style={{ marginBottom: 16 }}>
        <button className={theme === "dark" ? "on" : ""} onClick={() => setTheme("dark")}>Dark</button>
        <button className={theme === "light" ? "on" : ""} onClick={() => setTheme("light")}>Light</button>
      </div>
      <div className="label" style={{ marginBottom: 8 }}>Density</div>
      <div className="seg">
        <button className={s.density !== "compact" ? "on" : ""} onClick={() => upd({ density: "comfortable" })}>Comfortable</button>
        <button className={s.density === "compact" ? "on" : ""} onClick={() => upd({ density: "compact" })}>Compact</button>
      </div>
      <div className="hint" style={{ marginTop: 6 }}>Compact tightens tables and cards — more rows on screen for ops-heavy sessions.</div>
      <div className="label" style={{ marginTop: 16, marginBottom: 8 }}>Notifications</div>
      <div className="seg">
        <button className={s.notifications !== false ? "on" : ""} onClick={() => upd({ notifications: true })}>On</button>
        <button className={s.notifications === false ? "on" : ""} onClick={() => upd({ notifications: false })}>Off</button>
      </div>
      <div className="hint" style={{ marginTop: 6 }}>Off silences the bottom-right pop-ups and the bell badge.</div>

      <div className="label" style={{ marginTop: 16, marginBottom: 8 }}>Topology — nodes to draw</div>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <input className="input" type="number" min={10} max={2000} style={{ width: 110 }}
               value={topoN} onChange={e => { setTopoN(e.target.value); setTopoConfirm(false); }}/>
        <button className="btn accent sm" disabled={Math.round(Number(topoN) || 0) === (s.topoMaxNodes || 100)} onClick={applyTopo}>Apply</button>
        <span className="hint">~{topoLoad(Math.round(Number(topoN) || 0))} render load</span>
      </div>
      {topoConfirm && (
        <div className="banner danger" style={{ marginTop: 10, alignItems: "center" }}>
          <I.alertT size={14}/>
          <span style={{ flex: 1 }}>Drawing {Math.round(Number(topoN) || 0)} nodes is a <b>{topoLoad(Math.round(Number(topoN) || 0))}</b> render load and may make the graph stutter. Apply anyway?</span>
          <button className="btn accent sm" style={{ flexShrink: 0 }} onClick={applyTopo}>Apply anyway</button>
          <button className="btn ghost sm" style={{ flexShrink: 0 }} onClick={() => setTopoConfirm(false)}>Cancel</button>
        </div>
      )}
      <div className="hint" style={{ marginTop: 6 }}>The graph draws the most-relevant N; search and filters reach the rest at any size.</div>

      <div className="label" style={{ marginTop: 16, marginBottom: 8 }}>About</div>
      {up && up.available && (
        <div className={"banner " + (up.breaking ? "danger" : "info")} style={{ alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
          <I.download size={14}/>
          <span style={{ flex: 1 }}>Update available — v{up.latest}.</span>
          {up.notes_url && <a className="btn ghost sm" style={{ flexShrink: 0 }} href={up.notes_url} target="_blank" rel="noreferrer">Patch notes</a>}
          <button className="btn accent sm" style={{ flexShrink: 0 }} disabled={applying} onClick={doUpdate}>{applying ? "Updating…" : "Update now"}</button>
          {up.breaking && <div style={{ flexBasis: "100%", fontSize: 11.5, marginTop: 4 }}>
            <I.alertT size={12}/> May include breaking changes{up.breaking_note ? " — " + up.breaking_note : ""}. Download a Full backup first (Local Config → Backup).
          </div>}
        </div>
      )}
      <div className="row" style={{ alignItems: "center", gap: 10 }}>
        <span className="hint mono">NexusGrid{ver ? " v" + ver : ""}</span>
        <button className="btn ghost sm" style={{ marginLeft: "auto" }}
                onClick={() => { onClose(); onWhatsNew && onWhatsNew(); }}>
          <I.zap size={12}/> What's new
        </button>
      </div>

      <div className="hint" style={{ marginTop: 14 }}>Saved automatically — these only affect this browser.</div>
    </Modal>
  );
};

const ProfileMenu = ({ theme, setTheme, node, onClose, onEditProfile, onUiSettings, bust }) => {
  const menuRef = React.useRef(null);
  const [ver, setVer] = React.useState("");
  const [upd, setUpd] = React.useState(null);     // {current, latest, available, notes_url}
  const [applying, setApplying] = React.useState(false);
  React.useEffect(() => {
    const handler = (e) => { if (menuRef.current && !menuRef.current.contains(e.target)) onClose(); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);
  React.useEffect(() => {
    fetch("/health").then(r => r.json()).then(d => setVer(d.version || "")).catch(() => {});
    api.get("/local/update/check").then(setUpd).catch(() => {});
  }, []);
  const doUpdate = async () => {
    if (!confirmUpdate(upd)) return;
    setApplying(true);
    try {
      await api.post("/local/update/apply");
      toast("Updating — the node is downloading the new version and will restart.", "info");
    } catch (e) {
      toast("Update failed: " + (e.detail || e.message || ""), "danger");
      setApplying(false);
    }
  };
  return (
    <div className="profile-menu" ref={menuRef}>
      <div className="profile-head">
        <NodeAvatar name={node.name} size={36} bust={bust}/>
        <div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{node.name || "this node"}</div>
          <div className="mono dim" style={{ fontSize: 11 }}>{node.addr || ""}</div>
        </div>
      </div>
      <div className="profile-divider"/>
      <div className="profile-row btn-row" onClick={() => { onClose(); onEditProfile(); }}>
        <I.users size={14}/>
        <span>Edit profile…</span>
      </div>
      <div className="profile-row btn-row" onClick={() => { onClose(); onUiSettings(); }}>
        <I.cog size={14}/>
        <span>Settings…</span>
      </div>
      <div className="profile-divider"/>
      <div className="profile-section-label">Appearance</div>
      <div className="profile-row">
        <I.eye size={14}/>
        <span>Theme</span>
        <div className="seg" style={{ marginLeft: "auto" }}>
          <button className={theme === "dark"  ? "on" : ""} onClick={() => setTheme("dark")}>Dark</button>
          <button className={theme === "light" ? "on" : ""} onClick={() => setTheme("light")}>Light</button>
        </div>
      </div>
      <div className="profile-divider"/>
      {upd && upd.available ? (
        <div className="profile-update">
          <div className="row" style={{ gap: 7, alignItems: "center" }}>
            <span className="upd-dot"/>
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>Update available · v{upd.latest}</span>
          </div>
          {upd.breaking && <div className="hint" style={{ marginTop: 6, color: "var(--rose, #fb7185)", fontSize: 11.5 }}>
            <I.alertT size={12}/> May include breaking changes{upd.breaking_note ? " — " + upd.breaking_note : ""}. Back up first (Config → Backup).
          </div>}
          <div className="row" style={{ gap: 8, marginTop: 9 }}>
            <button className="btn accent sm" disabled={applying} onClick={doUpdate}>{applying ? "Updating…" : "Update now"}</button>
            {upd.notes_url && <a className="btn ghost sm" href={upd.notes_url} target="_blank" rel="noreferrer">Patch notes</a>}
          </div>
          <div className="hint mono" style={{ marginTop: 8 }}>NexusGrid{ver ? " v" + ver : ""}</div>
        </div>
      ) : (
        <div className="profile-foot mono">NexusGrid{ver ? " v" + ver : ""}</div>
      )}
    </div>
  );
};

const Topbar = ({ theme, setTheme, collapsed, setCollapsed, node, setRoute }) => {
  const [profileOpen, setProfileOpen] = React.useState(false);
  const [editProfile, setEditProfile] = React.useState(false);
  const [uiSettings, setUiSettings] = React.useState(false);
  const [whatsNew, setWhatsNew] = React.useState(false);
  const [bust, setBust] = React.useState(1);
  return (
    <header className="topbar">
      <div className={"brand" + (collapsed ? " collapsed" : "")} onClick={() => setCollapsed(!collapsed)} style={{ cursor: "pointer" }} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
        <div className="brand-logo">
          {/* Brand mark: a nexus — six peers meshed in a ring, every one wired
           * to the hub. The grid is the peers; the hub is you. */}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="12" x2="20" y2="12"/>
            <line x1="12" y1="12" x2="16" y2="18.93"/>
            <line x1="12" y1="12" x2="8" y2="18.93"/>
            <line x1="12" y1="12" x2="4" y2="12"/>
            <line x1="12" y1="12" x2="8" y2="5.07"/>
            <line x1="12" y1="12" x2="16" y2="5.07"/>
            <path d="M20 12 L16 18.93 L8 18.93 L4 12 L8 5.07 L16 5.07 Z"/>
            <circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none"/>
            <circle cx="20" cy="12" r="1.6" fill="currentColor" stroke="none"/>
            <circle cx="16" cy="18.93" r="1.6" fill="currentColor" stroke="none"/>
            <circle cx="8" cy="18.93" r="1.6" fill="currentColor" stroke="none"/>
            <circle cx="4" cy="12" r="1.6" fill="currentColor" stroke="none"/>
            <circle cx="8" cy="5.07" r="1.6" fill="currentColor" stroke="none"/>
            <circle cx="16" cy="5.07" r="1.6" fill="currentColor" stroke="none"/>
          </svg>
        </div>
        {!collapsed && (
          <div>
            <div className="brand-name">NexusGrid</div>
            <div className="brand-sub">P2P compute &amp; storage</div>
          </div>
        )}
      </div>

      <div className="top-main"></div>

      <div className="top-actions">
        <NotifyBell setRoute={setRoute} onOpenWhatsNew={() => setWhatsNew(true)}/>
        <div className="profile-wrap" style={{ marginLeft: 8, position: "relative" }}>
          <button className="profile-trigger" onClick={() => setProfileOpen(o => !o)} title={node.name || "node"}>
            <NodeAvatar name={node.name} size={28} bust={bust}/>
            <span className="profile-name mono">{node.name || "this node"}</span>
          </button>
          {profileOpen && <ProfileMenu theme={theme} setTheme={setTheme} node={node} bust={bust}
                                       onClose={() => setProfileOpen(false)}
                                       onEditProfile={() => setEditProfile(true)}
                                       onUiSettings={() => setUiSettings(true)}/>}
        </div>
      </div>
      {editProfile && <ProfileModal node={node} bust={bust} setBust={setBust} onClose={() => setEditProfile(false)}/>}
      {uiSettings && <UiSettingsModal theme={theme} setTheme={setTheme} onClose={() => setUiSettings(false)}
                                      onWhatsNew={() => setWhatsNew(true)}/>}
      {whatsNew && <WhatsNew onClose={() => setWhatsNew(false)}/>}
    </header>
  );
};

export { Sidebar, Topbar, NAV };
