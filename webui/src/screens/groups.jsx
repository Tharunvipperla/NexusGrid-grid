/* Groups — server-rail layout: a narrow avatar rail on the left (one circle
 * per group, image avatars supported), the selected group in the middle with
 * a header + subtabs (Members / Roles / Relays / Invites). All Phase-9
 * functionality is preserved: join-by-link, mint invites, invite friends,
 * role editor, role assignment, kick, pending approvals, pause/resume, and
 * the W66 relay content-share controls. Avatars are set by role:assign
 * holders (downscaled in-browser, synced to members via group.meta). */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Avatar, Pill, CardHead, Chk, Field, Modal, RadioTile, Verified } from "../components.jsx";
import { mentionTargets, suggestTargets, MentionText, MentionComposer, mentionsMe } from "../mentions.jsx";
import { AttachControl, AttachmentView } from "../attachments.jsx";
import { markRead, fmtBadge, fmtAgo } from "../notify.js";

const roleTone = (r) => r === "founder" ? "purple" : r === "admin" ? "blue" : r === "member" ? "emerald" : "cyan";
const short = (s) => s ? (s.length > 16 ? s.slice(0, 10) + "…" + s.slice(-6) : s) : "";

/* One download button; click to choose the format. */
const DownloadMenu = ({ onPick, formats = ["csv", "json"] }) => {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className="btn ghost sm" title="Download" onClick={() => setOpen(o => !o)}><I.download size={13}/></button>
      {open && (
        <div className="dl-menu">
          {formats.map(f => (
            <button key={f} onClick={() => { setOpen(false); onPick(f); }}>{f.toUpperCase()}</button>
          ))}
        </div>
      )}
    </div>
  );
};

const ALL_PERMS = [
  "group:read", "group:invite", "group:approve",
  "member:kick", "member:mute", "role:assign",
  "service:list", "service:host",
  "relay:host", "relay:use", "relay:share_content",
  "task:run",
];

const Danger = ({ label, confirmLabel = "Sure?", onFire }) => {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 3500);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <button className={"btn sm " + (armed ? "accent" : "ghost")}
            onClick={() => { if (armed) { setArmed(false); onFire(); } else setArmed(true); }}>
      {armed ? confirmLabel : label}
    </button>
  );
};

/* Image avatar when the group has one, letter avatar otherwise. */
const GAvatar = ({ group, size = 40, color = "#a78bfa" }) => (
  group && group.avatar
    ? <img src={group.avatar} alt="" style={{ width: size, height: size, borderRadius: "50%", objectFit: "cover", display: "block", border: "1px solid var(--br)" }}/>
    : <Avatar name={(group && group.name) || "?"} color={color} size={size}/>
);

/* Downscale a picked image to a small square data URL (≤64 KB target). */
const fileToAvatar = (file) => new Promise((resolve, reject) => {
  const img = new Image();
  const url = URL.createObjectURL(file);
  img.onload = () => {
    URL.revokeObjectURL(url);
    const c = document.createElement("canvas");
    c.width = c.height = 128;
    const ctx = c.getContext("2d");
    const s = Math.min(img.width, img.height);
    ctx.drawImage(img, (img.width - s) / 2, (img.height - s) / 2, s, s, 0, 0, 128, 128);
    let out = c.toDataURL("image/png");
    if (out.length > 60000) out = c.toDataURL("image/jpeg", 0.82);
    if (out.length > 60000) return reject(new Error("image too complex — try a simpler one"));
    resolve(out);
  };
  img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("not a readable image")); };
  img.src = url;
});

/* ── In-page panels (join / role editor / mint / invite friends) ── */
const JoinPanel = ({ onDone, onCancel }) => {
  const [link, setLink] = React.useState("");
  const [parsed, setParsed] = React.useState(null);
  const [message, setMessage] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const parse = async () => {
    setStatus("Checking link…"); setParsed(null);
    try {
      const p = await api.post("/local/groups/parse_join_link", { join_link: link.trim() });
      setParsed(p);
      const rly = (p.relay_urls || []).length;
      setStatus(`Link OK · group ${(p.group_id || "").slice(0, 8)}… · ` +
        (rly ? `${rly} relay${rly === 1 ? "" : "s"}` : "no relays — LAN-only (won't reach a NAT'd founder)"));
    } catch (e) { setStatus("Invalid link: " + (e.detail || e.message)); }
  };

  const join = async () => {
    setBusy(true); setStatus("Contacting admin…");
    try {
      const r = await api.post("/local/groups/join", {
        admin_address: parsed.admin_address || "",
        invite_token: parsed.signed_invite_hex ? "" : (parsed.invite_token || ""),
        signed_invite_hex: parsed.signed_invite_hex || "",
        message,
        admin_node_id: parsed.admin_node_id || "",
        relay_urls: parsed.relay_urls || [],
        grid_key: parsed.grid_key || "",
      });
      if (r.status === "pending") {
        setStatus(`Request submitted to ${r.group_name || "the group"} — waiting for an admin to approve.`);
        setTimeout(onDone, 1800);
      } else {
        setStatus(`Joined ${r.group_name} as ${r.my_role}.`);
        setTimeout(() => onDone(r.group_id), 900);
      }
    } catch (e) { setStatus("Failed: " + (e.detail || e.message)); }
    finally { setBusy(false); }
  };

  return (
    <Modal title="Join a group" icon={<I.link size={14}/>} tone="cyan" onClose={onCancel}
           foot={<>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" disabled={!parsed || busy} onClick={join}><I.check size={14}/> Request to join</button>
           </>}>
      <div className="row" style={{ gap: 10 }}>
        <input className="input mono" placeholder="Paste a nxg://join#… link" value={link} autoFocus
               onChange={e => setLink(e.target.value)} style={{ flex: 1, fontSize: 12 }}/>
        <button className="btn ghost" disabled={!link.trim()} onClick={parse}>Check link</button>
      </div>
      {parsed && (
        <div style={{ marginTop: 12 }}>
          <Field label="Message to the admin (optional)" hint="shown alongside your join request">
            <input className="input" maxLength={200} value={message} onChange={e => setMessage(e.target.value)}/>
          </Field>
        </div>
      )}
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
    </Modal>
  );
};

/* Create a group with the full classic options: name, privacy, relay. */
const CreateGroupPanel = ({ onDone, onCancel }) => {
  const [name, setName] = React.useState("");
  const [privacy, setPrivacy] = React.useState("open");
  const [relayUrl, setRelayUrl] = React.useState("");
  const [suggested, setSuggested] = React.useState("");
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    api.get("/local/relay/status").then(r => {
      const url = (r && (r.suggested_url || "")) || "";
      setSuggested(url);
      setRelayUrl(prev => prev || url);
    }).catch(() => {});
  }, []);

  const create = async () => {
    setBusy(true); setStatus("Creating…");
    try {
      const r = await api.post("/local/groups", {
        name: name.trim(),
        privacy_mode: privacy,
        relay_urls: relayUrl.trim() ? [relayUrl.trim()] : [],
      });
      onDone(r && r.id);
    } catch (e) { setStatus("Create failed: " + (e.detail || e.message)); setBusy(false); }
  };

  return (
    <Modal title="Create a group" icon={<I.plus size={14}/>} tone="purple" onClose={onCancel}
           foot={<>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" disabled={!name.trim() || busy} onClick={create}><I.check size={14}/> Create group</button>
           </>}>
      <Field label="Group name">
        <input className="input" maxLength={128} autoFocus value={name}
               onChange={e => setName(e.target.value)}
               onKeyDown={e => { if (e.key === "Enter" && name.trim()) create(); }}/>
      </Field>
      <div className="label" style={{ margin: "14px 0 8px" }}>Join policy</div>
      <div className="field-row">
        <RadioTile on={privacy === "open"} title="Open" sub="anyone with an invite link joins instantly" onClick={() => setPrivacy("open")}/>
        <RadioTile on={privacy === "private"} title="Private" sub="join requests wait for an admin's approval" onClick={() => setPrivacy("private")}/>
      </div>
      <div style={{ marginTop: 14 }}>
        <Field label="Relay to attach (optional)"
               hint={suggested ? "prefilled with your running relay — clear it for LAN-only" : "ws:// or wss:// — leave blank for LAN-only (you can bind one later)"}>
          <input className="input mono" placeholder="wss://relay.example.com" value={relayUrl}
                 onChange={e => setRelayUrl(e.target.value)}/>
        </Field>
      </div>
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
    </Modal>
  );
};

const RolePanel = ({ gid, role, onDone, onCancel }) => {
  const [name, setName] = React.useState(role ? role.name : "");
  const [perms, setPerms] = React.useState(new Set(role ? role.permissions || [] : []));
  const [err, setErr] = React.useState("");
  const toggle = (p) => {
    const next = new Set(perms);
    next.has(p) ? next.delete(p) : next.add(p);
    setPerms(next);
  };
  const save = async () => {
    try {
      await api.post(`/local/groups/${encodeURIComponent(gid)}/roles`, { name: name.trim(), permissions: [...perms] });
      onDone();
    } catch (e) { setErr("Save failed: " + (e.detail || e.message)); }
  };
  return (
    <Modal title={role ? `Edit role: ${role.name}` : "New role"} icon={<I.key size={14}/>} tone="amber" onClose={onCancel}
           foot={<>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" disabled={!name.trim()} onClick={save}><I.check size={14}/> Save role</button>
           </>}>
      {!role && (
        <Field label="Role name">
          <input className="input" maxLength={40} autoFocus value={name} onChange={e => setName(e.target.value)}/>
        </Field>
      )}
      <div className="label" style={{ margin: "12px 0 8px" }}>Permissions</div>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        {ALL_PERMS.map(p => (
          <div key={p} className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }} onClick={() => toggle(p)}>
            <Chk on={perms.has(p)}/>
            <span className="mono" style={{ fontSize: 12 }}>{p}</span>
          </div>
        ))}
      </div>
      {err && <div className="hint" style={{ marginTop: 10, color: "var(--rose, #fb7185)" }}>{err}</div>}
    </Modal>
  );
};

const MintPanel = ({ gid, hasRelay, onDone, onCancel }) => {
  const [days, setDays] = React.useState(7);
  const [uses, setUses] = React.useState(60);
  const [result, setResult] = React.useState(null);
  const [err, setErr] = React.useState("");
  const mint = async () => {
    try {
      const r = await api.post(`/local/groups/${encodeURIComponent(gid)}/secure_link`, {
        expires_in_days: Math.max(1, Math.min(90, Number(days) || 7)),
        max_uses: Math.max(1, Math.min(1000, Number(uses) || 60)),
      });
      setResult(r);
    } catch (e) { setErr("Mint failed: " + (e.detail || e.message)); }
  };
  return (
    <Modal title="Mint invite link" icon={<I.share size={14}/>} tone="purple" onClose={result ? onDone : onCancel}
           foot={!result ? <>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" onClick={mint}><I.check size={14}/> Mint</button>
           </> : <button className="btn accent" onClick={onDone}>Done</button>}>
      {!hasRelay && (
        <div className="banner info" style={{ marginBottom: 12 }}>
          <I.info size={14}/>
          <span>No relay bound to this group — the link will be LAN-only. Bind a relay for cross-region invites.</span>
        </div>
      )}
      {!result ? (
        <>
          <div className="field-row">
            <Field label="Expires in (days)" hint="1–90"><input className="input" type="number" min={1} max={90} value={days} onChange={e => setDays(e.target.value)}/></Field>
            <Field label="Max uses" hint="1–1000"><input className="input" type="number" min={1} max={1000} value={uses} onChange={e => setUses(e.target.value)}/></Field>
          </div>
          {err && <div className="hint" style={{ marginTop: 10, color: "var(--rose, #fb7185)" }}>{err}</div>}
        </>
      ) : (
        <>
          <div className="hint" style={{ marginBottom: 8 }}>
            Invite created — expires {(result.expires_at || "").slice(0, 19)} · max uses {result.max_uses}.
            Copy it now: for security the link is only shown once.
          </div>
          <div className="row" style={{ gap: 10, alignItems: "center" }}>
            <span className="mono" style={{ fontSize: 11, background: "rgba(255,255,255,0.04)", border: "1px solid var(--br)", borderRadius: 6, padding: "6px 10px", maxWidth: 440, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{result.link}</span>
            <button className="btn ghost sm" onClick={() => navigator.clipboard.writeText(result.link || "")}><I.copy size={13}/> Copy</button>
          </div>
        </>
      )}
    </Modal>
  );
};

const InviteFriendsPanel = ({ gid, onDone, onCancel }) => {
  const [peers, setPeers] = React.useState(null);
  const [picked, setPicked] = React.useState([]);
  const [results, setResults] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    api.get("/local/peers").then(d => setPeers((d.peers || []).filter(p =>
      ["trusted", "trusted_pending_in", "trusted_pending_out"].includes(p.status)))).catch(() => setPeers([]));
  }, []);
  const send = async () => {
    setBusy(true);
    try {
      const r = await api.post(`/local/groups/${encodeURIComponent(gid)}/invite_friends`, { peer_ips: picked });
      setResults(r.results || []);
    } catch (e) { setResults([{ ok: false, target_peer_label: "request", detail: e.detail || e.message }]); }
    finally { setBusy(false); }
  };
  return (
    <Modal title="Invite friends" icon={<I.users size={14}/>} tone="emerald" onClose={results ? onDone : onCancel}
           foot={!results ? <>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" disabled={!picked.length || busy} onClick={send}><I.send size={14}/> Send invites</button>
           </> : <button className="btn accent" onClick={onDone}>Done</button>}>
      <div className="hint" style={{ marginBottom: 10 }}>Trusted peers get the invite pushed directly — they accept from their Messages screen.</div>
      {peers === null && <div className="hint">Loading peers…</div>}
      {peers && peers.length === 0 && <div className="hint">No trusted peers yet — pair with one in the Network screen first.</div>}
      {peers && peers.length > 0 && !results && (
        <div className="col" style={{ gap: 8 }}>
          {peers.map(p => {
            const key = p.internal_ip || p.ip;
            return (
              <div key={key} className="row" style={{ gap: 8, alignItems: "center", cursor: "pointer" }}
                   onClick={() => setPicked(picked.includes(key) ? picked.filter(x => x !== key) : [...picked, key])}>
                <Chk on={picked.includes(key)}/>
                <Avatar name={p.display_name || p.ip} color="#34d399" size={22}/>
                <span style={{ fontSize: 13 }}>{p.display_name || p.ip}</span>
                {p.display_name && <span className="hint mono">{p.ip}</span>}
              </div>
            );
          })}
        </div>
      )}
      {results && (
        <div className="col" style={{ gap: 6 }}>
          {results.map((r, i) => (
            <div key={i} className="row" style={{ gap: 8, alignItems: "center" }}>
              <span style={{ fontSize: 13 }}>{r.target_peer_label || r.peer_ip}</span>
              {r.ok ? <Pill tone="emerald">delivered</Pill> : <Pill tone="rose">{r.detail || "failed"}</Pill>}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
};

/* In-group chat pane with @mentions (members, roles, @all). */
const GroupChat = ({ gid, detail }) => {
  const [thread, setThread] = React.useState([]);
  const [draft, setDraft] = React.useState("");
  const [att, setAtt] = React.useState(null);
  const [err, setErr] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const endRef = React.useRef(null);

  const targets = mentionTargets(detail);          // full sets — rendering
  const composeTargets = suggestTargets(detail);   // no self, no solo roles
  const me = (detail.members || []).find(m => m.pubkey === detail.my_pubkey);
  const myName = (me && me.display_name) || "";
  const myRoles = (me && me.roles) || [];
  const muted = !!(me && me.muted);

  const load = React.useCallback(async () => {
    try {
      const r = await api.get(`/local/groups/${encodeURIComponent(gid)}/messages?limit=200`);
      setThread(Array.isArray(r) ? r : (r.messages || []));
      markRead(gid); // the thread is on screen — clear its badge
    } catch (_) {}
  }, [gid]);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);
  React.useEffect(() => { if (endRef.current) endRef.current.scrollIntoView({ block: "end" }); }, [thread]);

  const send = async () => {
    const body = draft.trim();
    if ((!body && !att) || sending) return;
    setSending(true); setErr("");
    try {
      await api.post(`/local/groups/${encodeURIComponent(gid)}/messages`, {
        body,
        ...(att ? { attach_data: att.data, attach_name: att.name, attach_mime: att.mime } : {}),
      });
      setDraft(""); setAtt(null); await load();
    } catch (e) { setErr("Send failed: " + (e.detail || e.message || "")); }
    setSending(false);
  };
  const delMsg = async (msgId) => {
    try { await api.del(`/local/groups/${encodeURIComponent(gid)}/messages/${encodeURIComponent(msgId)}`); await load(); }
    catch (e) { setErr("Delete failed: " + (e.detail || e.message || "")); }
  };

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", height: "min(560px, calc(100vh - 320px))" }}>
      <div style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        {thread.length === 0 && <div className="dim" style={{ margin: "auto", fontSize: 12 }}>No messages yet — say hello. Use @ to mention a member, a role, or @all.</div>}
        {thread.map((m, i) => {
          const mine = m.mine || m.is_self || m.self || m.sender_pubkey === detail.my_pubkey || false;
          const sys = m.sender_pubkey === "system";
          const body = m.body ?? m.text ?? "";
          if (sys) return <div key={m.msg_id || i} className="dim" style={{ alignSelf: "center", fontSize: 11 }}>{body}</div>;
          if (m.deleted) return <div key={m.msg_id || i} className="dim" style={{ alignSelf: mine ? "flex-end" : "flex-start", fontSize: 11, fontStyle: "italic" }}>message deleted</div>;
          const pingsMe = !mine && mentionsMe(body, targets, myName, myRoles);
          const t = m.ts || m.created_at || m.sent_at;
          const dt = t ? new Date(typeof t === "number" ? (t > 1e12 ? t : t * 1000) : t) : null;
          return (
            <div key={m.msg_id || m.id || i} style={{ alignSelf: mine ? "flex-end" : "flex-start", maxWidth: "76%" }}>
              {!mine && <div className="dim mono" style={{ fontSize: 10, marginBottom: 2 }}>{m.sender_name || m.sender || "member"}</div>}
              <div style={{
                background: mine ? "var(--accent-w)" : "var(--bg-card-2)",
                border: "1px solid " + (pingsMe ? "var(--amber, #fbbf24)" : mine ? "var(--accent)" : "var(--br)"),
                borderLeft: pingsMe ? "3px solid var(--amber, #fbbf24)" : undefined,
                borderRadius: 10, padding: "7px 11px", fontSize: 13, wordBreak: "break-word",
              }}>
                {body && <MentionText text={body} targets={targets}/>}
                {m.attach_kind && m.msg_id && (
                  <AttachmentView m={m} url={`/local/groups/${encodeURIComponent(gid)}/messages/${encodeURIComponent(m.msg_id)}/attachment`}/>
                )}
              </div>
              <div className="dim" style={{ fontSize: 10, marginTop: 2, textAlign: mine ? "right" : "left" }}>
                {pingsMe && <Pill tone="amber">mentions you</Pill>} {dt && !isNaN(dt) ? dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
                {mine && m.msg_id && (
                  <span style={{ cursor: "pointer", marginLeft: 6, display: "inline-flex", verticalAlign: "middle", opacity: 0.7 }}
                        title="Delete for everyone" onClick={() => delMsg(m.msg_id)}><I.trash size={11}/></span>
                )}
              </div>
            </div>
          );
        })}
        <div ref={endRef}/>
      </div>
      {err && <div className="hint" style={{ padding: "4px 12px", color: "var(--rose, #fb7185)" }}>{err}</div>}
      {muted && <div className="hint" style={{ padding: "4px 12px", color: "var(--amber, #fbbf24)" }}>You are muted in this group — an admin must unmute you before you can send.</div>}
      <div className="row" style={{ gap: 8, padding: 12, borderTop: "1px solid var(--br)" }}>
        <AttachControl att={att} setAtt={setAtt} onError={setErr}/>
        <MentionComposer value={draft} onChange={setDraft} onSend={send} targets={composeTargets}/>
        <button className="btn accent" disabled={(!draft.trim() && !att) || sending || muted} onClick={send}><I.send size={14}/> Send</button>
      </div>
    </div>
  );
};

/* ── Relays tab — full classic parity: probe, bind/unbind, per-relay
 * label/region/priority, W41 state machine + transition timeline,
 * fingerprint freeze/propose/accept governance, pull-from-founder, and the
 * W66 content-share with an in-page consent warning. */
const RELAY_STATES = {
  online:       { label: "online",       tone: "emerald", sev: 0 },
  syncing:      { label: "syncing",      tone: "amber",   sev: 1 },
  validating:   { label: "validating",   tone: "amber",   sev: 1 },
  starting:     { label: "starting",     tone: "amber",   sev: 1 },
  reconnecting: { label: "reconnecting", tone: "amber",   sev: 2 },
  offline:      { label: "offline",      tone: "rose",    sev: 3 },
  retired:      { label: "retired",      tone: "ghost",   sev: 4 },
};
const relayState = (r) => RELAY_STATES[String(r.state || "").toLowerCase()]
  || (r.status === "active" ? RELAY_STATES.online : RELAY_STATES.offline);
const rttColor = (ms) => ms < 100 ? "var(--emerald, #34d399)" : ms < 300 ? "var(--amber, #fbbf24)" : "var(--rose, #fb7185)";

const RelaysTab = ({ gid, d, has, act, flash }) => {
  const [relays, setRelays] = React.useState(d.relays || []);
  const [probing, setProbing] = React.useState(false);
  const [bindUrl, setBindUrl] = React.useState("");
  const [open, setOpen] = React.useState(null);       // {url, mode: edit|timeline|share}
  const [cfg, setCfg] = React.useState({ label: "", region: "", priority: 0 });
  const [timeline, setTimeline] = React.useState(null);
  const [proposals, setProposals] = React.useState([]);

  React.useEffect(() => { setRelays(d.relays || []); }, [d]);

  const isFounder = d.founder_pubkey === d.my_pubkey;
  const canHost = has("relay:host");
  const canShare = has("relay:share_content");
  const base = `/local/groups/${encodeURIComponent(gid)}`;

  React.useEffect(() => {
    if (!isFounder) { setProposals([]); return; }
    api.get(`${base}/relays/code_fingerprint/proposals`)
      .then(r => setProposals(r.proposals || [])).catch(() => setProposals([]));
  }, [base, isFounder, d.relay_code_fingerprint]);

  const names = {};
  (d.members || []).forEach(m => { if (m.pubkey && m.display_name) names[m.pubkey] = m.display_name; });

  const hostRoles = new Set((d.roles || []).filter(r => (r.permissions || []).includes("relay:host")).map(r => r.name));
  const hosts = (d.members || []).filter(m => (m.roles || []).some(rn => hostRoles.has(rn))).length;
  const online = relays.filter(r => relayState(r).label === "online").length;
  const offline = relays.filter(r => relayState(r).label === "offline").length;
  const recovering = relays.length - online - offline;

  const probe = async () => {
    setProbing(true);
    try { const r = await api.post(`${base}/relays/probe`); setRelays(r.relays || []); }
    catch (e) { flash("Probe failed: " + (e.detail || e.message || "")); }
    setProbing(false);
  };
  const bind = () => {
    const url = bindUrl.trim();
    if (!url) return;
    act("Relay bound", () => api.post(`${base}/relays`, { relay_url: url })).then(() => setBindUrl(""));
  };
  const showTimeline = async (url) => {
    if (open && open.url === url && open.mode === "timeline") { setOpen(null); return; }
    setOpen({ url, mode: "timeline" }); setTimeline(null);
    try {
      const r = await api.get(`${base}/relays/timeline?relay_url=${encodeURIComponent(url)}&limit=20`);
      setTimeline(r.events || []);
    } catch (_) { setTimeline([]); }
  };
  const startEdit = (r) => {
    setOpen({ url: r.relay_url, mode: "edit" });
    setCfg({ label: r.label || "", region: r.region || "", priority: r.priority || 0 });
  };
  const saveCfg = (url) => act("Relay configured", () => api.post(`${base}/relays/config`, {
    relay_url: url, label: cfg.label, region: cfg.region,
    priority: Math.max(-100, Math.min(100, Number(cfg.priority) || 0)),
  })).then(() => setOpen(null));

  /* Fingerprint freeze — founder sets directly, relay:host admins propose. */
  const frozen = (d.relay_code_fingerprint || "").trim();
  const freeze = async () => {
    let fp = "";
    try { fp = ((await api.get("/local/relay/status")).code_fingerprint || "").trim(); } catch (_) {}
    if (!fp) { flash("Freeze failed: no local relay running — start yours so its fingerprint can be read"); return; }
    if (isFounder) await act("Fingerprint frozen", () => api.post(`${base}/relays/code_fingerprint`, { fingerprint: fp }));
    else await act("Freeze proposed to the founder", () => api.post(`${base}/relays/code_fingerprint/propose`, { fingerprint: fp }));
  };
  const decide = (id, decision) => act(decision === "accept" ? "Proposal accepted" : "Proposal rejected", () =>
    api.post(`${base}/relays/code_fingerprint/accept/${encodeURIComponent(id)}?decision=${decision}`));

  const pull = () => act("Synced from founder", async () => {
    const r = await api.post(`${base}/relays/pull_from_founder`);
    if (!r.ok) throw { detail: r.reason || ("status " + (r.status || "?")) };
  });

  return (
    <>
      <div className="card">
        <div className="row" style={{ padding: "12px 14px", gap: 10, alignItems: "center", flexWrap: "wrap", borderBottom: "1px solid var(--br)" }}>
          <div style={{ flex: 1, minWidth: 220, fontSize: 12 }}>
            {relays.length === 0 ? <span className="dim">No relays bound — this group is LAN-direct only.</span>
              : online > 0 && offline === 0 && recovering === 0 ? <span style={{ color: "var(--emerald, #34d399)" }}>Group fully connected — {online}/{relays.length} relay{relays.length === 1 ? "" : "s"} online</span>
              : online > 0 ? <span style={{ color: "var(--amber, #fbbf24)" }}>Routable through {online}/{relays.length} relay{online === 1 ? "" : "s"}{recovering ? `, ${recovering} recovering` : ""}{offline ? `, ${offline} offline` : ""}</span>
              : recovering > 0 ? <span style={{ color: "var(--amber, #fbbf24)" }}>{recovering} relay{recovering === 1 ? "" : "s"} recovering — short outage expected</span>
              : <span style={{ color: "var(--rose, #fb7185)" }}>All {relays.length} relays offline — group traffic is LAN-direct until one recovers</span>}
            <span className="dim"> · {hosts} member{hosts === 1 ? "" : "s"} can host relays</span>
          </div>
          {!isFounder && <button className="btn ghost sm" title="Backfill the relay list from the founder" onClick={pull}><I.refresh size={13}/> Sync from founder</button>}
          <button className="btn ghost sm" disabled={probing || relays.length === 0} onClick={probe}><I.pulse size={13}/> {probing ? "Probing…" : "Probe now"}</button>
        </div>

        {canHost && (
          <div className="row" style={{ padding: "10px 14px", gap: 8, borderBottom: relays.length ? "1px solid var(--br)" : "none" }}>
            <input className="input mono" style={{ flex: 1 }} placeholder="wss://relay.example.com — bind another relay"
                   value={bindUrl} onChange={e => setBindUrl(e.target.value)}
                   onKeyDown={e => { if (e.key === "Enter") bind(); }}/>
            <button className="btn accent sm" disabled={!bindUrl.trim()} onClick={bind}><I.plus size={13}/> Bind</button>
          </div>
        )}

        {relays.length > 0 && (
          <table className="t">
            <thead><tr><th>Relay</th><th>Operator</th><th>Region</th><th>Prio</th><th>State</th><th>RTT</th><th></th></tr></thead>
            <tbody>
              {relays.map((r, i) => {
                const st = relayState(r);
                const opName = names[r.operator_pubkey];
                const isOpen = open && open.url === r.relay_url;
                return (
                  <React.Fragment key={r.relay_url || i}>
                    <tr>
                      <td style={{ maxWidth: 280 }}>
                        {r.label && <div style={{ fontWeight: 600, fontSize: 12 }}>{r.label}</div>}
                        <code className="mono" style={{ fontSize: 10.5, wordBreak: "break-all" }}>{r.relay_url}</code>
                        <div style={{ fontSize: 10.5, marginTop: 2, display: "flex", alignItems: "center", gap: 4, color: r.content_share ? "var(--amber, #fbbf24)" : "var(--t-mute)" }}>
                          {r.content_share ? <I.unlock size={10}/> : <I.lock size={10}/>}
                          {r.content_share ? `content-readable${names[r.content_share_by] ? " · authorized by " + names[r.content_share_by] : ""}` : "E2E-blind"}
                        </div>
                      </td>
                      <td style={{ fontSize: 12 }}>{opName || (r.operator_pubkey ? <span className="mono dim">{short(r.operator_pubkey)}</span> : "—")}</td>
                      <td style={{ fontSize: 12 }}>{r.region || <span className="dim">—</span>}</td>
                      <td className="mono" style={{ fontSize: 12 }}>{r.priority || 0}</td>
                      <td>
                        <span style={{ cursor: "pointer" }} title="Click for transition history" onClick={() => showTimeline(r.relay_url)}>
                          <Pill tone={st.tone} dot>{st.label}</Pill>
                        </span>
                      </td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {st.label === "offline" || r.last_rtt_ms == null ? <span className="dim">—</span>
                          : <span style={{ color: rttColor(r.last_rtt_ms) }}>{r.last_rtt_ms} ms</span>}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <div className="row" style={{ gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                          {canHost && <button className="btn ghost sm" onClick={() => isOpen && open.mode === "edit" ? setOpen(null) : startEdit(r)}>Edit</button>}
                          {canShare && (r.content_share
                            ? <button className="btn ghost sm" onClick={() => act("Content access revoked", () => api.post(`${base}/relays/content_revoke`, { relay_url: r.relay_url }))}>Revoke content</button>
                            : <button className="btn ghost sm" onClick={() => setOpen(isOpen && open.mode === "share" ? null : { url: r.relay_url, mode: "share" })}>Authorize content…</button>)}
                          {canHost && <Danger label="Unbind" confirmLabel="Unbind relay?" onFire={() => act("Relay unbound", () => api.del(`${base}/relays?relay_url=${encodeURIComponent(r.relay_url)}`))}/>}
                        </div>
                      </td>
                    </tr>
                    {isOpen && open.mode === "edit" && (
                      <tr><td colSpan={7} style={{ padding: "8px 14px", background: "rgba(255,255,255,0.02)" }}>
                        <div className="row" style={{ gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
                          <Field label="Label"><input className="input" style={{ width: 160 }} maxLength={60} value={cfg.label} onChange={e => setCfg({ ...cfg, label: e.target.value })}/></Field>
                          <Field label="Region" hint="e.g. us-east, home-LAN"><input className="input" style={{ width: 140 }} maxLength={40} value={cfg.region} onChange={e => setCfg({ ...cfg, region: e.target.value })}/></Field>
                          <Field label="Priority" hint="-100…100, higher = preferred"><input className="input" type="number" min={-100} max={100} style={{ width: 90 }} value={cfg.priority} onChange={e => setCfg({ ...cfg, priority: e.target.value })}/></Field>
                          <button className="btn accent sm" onClick={() => saveCfg(r.relay_url)}><I.check size={13}/> Save</button>
                          <button className="btn ghost sm" onClick={() => setOpen(null)}>Cancel</button>
                        </div>
                      </td></tr>
                    )}
                    {isOpen && open.mode === "share" && (
                      <tr><td colSpan={7} style={{ padding: "10px 14px", background: "rgba(245,158,11,0.06)" }}>
                        <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                          <span style={{ fontSize: 12, color: "var(--amber, #fbbf24)", flex: 1, minWidth: 260 }}>
                            This relay's host will be able to read <strong>all group messages</strong> sent through it.
                            The authorization is recorded and visible to every member.
                          </span>
                          <button className="btn accent sm" onClick={() => act("Relay authorized to read content", () => api.post(`${base}/relays/content_share`, { relay_url: r.relay_url })).then(() => setOpen(null))}>Confirm authorize</button>
                          <button className="btn ghost sm" onClick={() => setOpen(null)}>Cancel</button>
                        </div>
                      </td></tr>
                    )}
                    {isOpen && open.mode === "timeline" && (
                      <tr><td colSpan={7} style={{ padding: "8px 14px", background: "rgba(255,255,255,0.02)" }}>
                        {timeline === null && <span className="hint">Loading transitions…</span>}
                        {timeline && timeline.length === 0 && <span className="hint">No state transitions recorded yet for this relay.</span>}
                        {timeline && timeline.length > 0 && (
                          <div className="col" style={{ gap: 3 }}>
                            {timeline.map((e, j) => (
                              <div key={j} className="mono" style={{ fontSize: 11 }}>
                                <span className="dim" title={e.ts ? new Date(parseFloat(e.ts) * 1000).toLocaleString() : ""}>{fmtAgo(parseFloat(e.ts || 0) * 1000)}</span>
                                {"  "}{e.transition || "?"}{e.reason ? <span className="dim">  ({e.reason})</span> : null}
                              </div>
                            ))}
                          </div>
                        )}
                      </td></tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* fingerprint governance — relay code the group requires (W41/W63) */}
      {(isFounder || canHost) && (
        <div className="card">
          <CardHead icon={<I.shield size={14}/>} tone="purple" title="Relay code fingerprint"
                    meta={frozen
                      ? <span className="mono" style={{ color: "var(--emerald, #34d399)" }}>frozen · {frozen.slice(0, 12)}…</span>
                      : <span style={{ color: "var(--amber, #fbbf24)" }}>not frozen — any relay code accepted</span>}/>
          <div className="col" style={{ gap: 10, padding: "10px 14px 14px" }}>
            <div className="hint">
              Freezing pins the relay build members may host for this group: binding a relay whose code
              doesn't match is rejected. {isFounder ? "As founder you set it directly." : "You can propose a change — the founder decides."}
            </div>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <button className="btn ghost sm" onClick={freeze}>
                <I.shield size={13}/> {isFounder ? (frozen ? "Re-freeze to my local relay code" : "Freeze to my local relay code") : (frozen ? "Propose change to my code" : "Propose freeze to my code")}
              </button>
              {isFounder && frozen && (
                <Danger label="Clear freeze" confirmLabel="Accept any code?" onFire={() => act("Freeze cleared", () => api.post(`${base}/relays/code_fingerprint`, { fingerprint: "" }))}/>
              )}
            </div>
            {isFounder && proposals.length > 0 && (
              <div className="col" style={{ gap: 6 }}>
                <div className="hint">Pending proposals:</div>
                {proposals.map(p => (
                  <div key={p.id} className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <code className="mono" style={{ fontSize: 11 }}>{(p.proposed_fingerprint || "").slice(0, 16)}…</code>
                    <span className="hint">by {names[p.proposed_by_pubkey] || short(p.proposed_by_pubkey || "")} · {(p.proposed_at || "").slice(0, 19)}</span>
                    <button className="btn accent sm" onClick={() => decide(p.id, "accept")}>Accept</button>
                    <button className="btn ghost sm" onClick={() => decide(p.id, "reject")}>Reject</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* W67: copy + run the relay build this group requires */}
      <RelayCodeCopyCard gid={gid} has={has} act={act} flash={flash}/>
    </>
  );
};

/* ── W67 relay-code copy — obtain the relay build a group froze, run it
 * sandboxed, and (founder/admin) publish your build so members can copy. */
const RelayCodeCopyCard = ({ gid, has, act, flash }) => {
  const base = `/local/groups/${encodeURIComponent(gid)}`;
  const [st, setSt] = React.useState(null);
  const [run, setRun] = React.useState({ open: false, port: 9100, agreed: false });
  const [lastUrl, setLastUrl] = React.useState("");
  const canHost = has("relay:host");
  const canPublish = has("role:assign");

  const load = React.useCallback(() => {
    api.get(`${base}/relay_code/status`).then(setSt).catch(() => setSt(null));
  }, [base]);
  React.useEffect(() => { load(); }, [load]);

  // Only meaningful once the group froze a build (W41). When unfrozen any
  // code is accepted, so there's nothing specific to copy.
  if (!st || !st.frozen_fingerprint) return null;
  const have = st.have_local_module;                 // "" | "default" | "grp_…"
  const copied = have && have.startsWith("grp_");    // an obtained custom build
  const fp = (st.frozen_fingerprint || "").slice(0, 12);

  const showCopy = canHost && !have;
  const showRun = canHost && copied;
  const showPublish = canPublish && have && have !== "default";
  if (!showCopy && !showRun && !showPublish) return null;

  const obtain = () => act("Relay code copied into this node", async () => {
    const r = await api.post(`${base}/relay_code/obtain`);
    return r;
  }).then(load);

  const publish = () => act("Relay code published to the group", () =>
    api.post(`${base}/relay_code/publish`, { module: have }));

  const doRun = () => act("Relay started (sandboxed)", async () => {
    const r = await api.post(`/local/relay/sandbox`, {
      module: have, port: Number(run.port) || 9100, runner: "raw", agreed: true,
    });
    if (!r.ok) throw { detail: r.error || "run failed" };
    return r;
  }).then((r) => {
    const url = (r.relay && r.relay.url) || `ws://127.0.0.1:${Number(run.port) || 9100}`;
    setLastUrl(url);
    setRun({ ...run, open: false, agreed: false });
    flash(`Relay running at ${url} — paste it into Bind above to route this group through it.`);
  });

  return (
    <div className="card">
      <CardHead icon={<I.refresh size={14}/>} tone="purple" title="This group's relay build"
                meta={<span className="mono dim">fingerprint {fp}…</span>}/>
      <div className="col" style={{ gap: 10, padding: "10px 14px 14px" }}>
        {showCopy && (
          <>
            <div className="hint">
              This group runs a custom relay build you don't have yet. Copy it (channel copy, or
              pulled from a current relay host) so you can host this group's relay too. Copying only
              saves the code — running it is a separate, sandboxed step you confirm below.
            </div>
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <button className="btn accent sm" onClick={obtain}><I.refresh size={13}/> Copy relay code</button>
            </div>
          </>
        )}

        {showRun && (
          <>
            <div className="row" style={{ gap: 6, alignItems: "center", fontSize: 12 }}>
              <I.check size={13} style={{ color: "var(--emerald, #34d399)" }}/>
              You have this group's relay build (module <code className="mono">{have}</code>).
            </div>
            {!run.open ? (
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <button className="btn ghost sm" onClick={() => setRun({ ...run, open: true })}><I.pulse size={13}/> Run this relay…</button>
                {lastUrl && <code className="mono dim" style={{ fontSize: 11 }}>last started: {lastUrl}</code>}
              </div>
            ) : (
              <div className="col" style={{ gap: 8, padding: "10px 12px", background: "rgba(245,158,11,0.06)", borderRadius: 8 }}>
                <span style={{ fontSize: 12, color: "var(--amber, #fbbf24)" }}>
                  You're about to run relay code authored by this group, out-of-process in a sandbox.
                  Only do this if you trust the group's relay build.
                </span>
                <div className="row" style={{ gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
                  <Field label="Port" hint="1024–65535"><input className="input" type="number" min={1024} max={65535} style={{ width: 110 }} value={run.port} onChange={e => setRun({ ...run, port: e.target.value })}/></Field>
                  <label className="row" style={{ gap: 6, fontSize: 12, alignItems: "center" }}>
                    <input type="checkbox" checked={run.agreed} onChange={e => setRun({ ...run, agreed: e.target.checked })}/>
                    I understand and want to run this relay
                  </label>
                  <button className="btn accent sm" disabled={!run.agreed} onClick={doRun}><I.pulse size={13}/> Run sandboxed</button>
                  <button className="btn ghost sm" onClick={() => setRun({ ...run, open: false, agreed: false })}>Cancel</button>
                </div>
              </div>
            )}
          </>
        )}

        {showPublish && (
          <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap", borderTop: (showCopy || showRun) ? "1px solid var(--br)" : "none", paddingTop: (showCopy || showRun) ? 10 : 0 }}>
            <span className="hint" style={{ flex: 1, minWidth: 220 }}>
              Publish your matching relay build (<code className="mono">{have}</code>) into the group so members can copy it{st.channel_copy_available ? " — a copy is already published" : ""}.
            </span>
            <button className="btn ghost sm" onClick={publish}><I.shield size={13}/> {st.channel_copy_available ? "Re-publish" : "Publish to group"}</button>
          </div>
        )}
      </div>
    </div>
  );
};

/* ── presence dot — green when seen inside the online window ── */
const PRESENCE_WINDOW_MS = 150000;
const Presence = ({ lastSeen, isMe }) => {
  let label = "offline", on = false;
  if (isMe) { label = "online"; on = true; }
  else {
    const t = lastSeen ? Date.parse(lastSeen) : NaN;
    if (!isNaN(t)) {
      const age = Date.now() - t;
      if (age < PRESENCE_WINDOW_MS) { label = "online"; on = true; }
      else { const days = Math.floor(age / 86400000); label = days < 1 ? "offline" : days >= 30 ? "offline 30+d" : `offline ${days}d`; }
    }
  }
  return (
    <span title={label} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: on ? "var(--emerald, #34d399)" : "var(--t-mute)" }}/>
      <span className="dim" style={{ fontSize: 10 }}>{label}</span>
    </span>
  );
};

const fmtBytes = (n) => {
  n = Number(n) || 0;
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
};

/* ── peer profile modal (Wave 50/51/52 data) ── */
const PeerProfileModal = ({ nodeId, name, onClose }) => {
  const [p, setP] = React.useState(null);
  const [err, setErr] = React.useState("");
  React.useEffect(() => {
    api.get(`/local/peers/${encodeURIComponent(nodeId)}/profile`)
      .then(setP).catch(e => setErr(e.detail || e.message || "unreachable"));
  }, [nodeId]);
  const u = (p && p.global_usage) || {};
  const ex = (p && p.exchange_with_you) || {};
  const rel = (p && p.reliability_with_you) || {};
  return (
    <Modal title={(p && p.display_name) || name || "Peer profile"} icon={<I.users size={14}/>} tone="blue" onClose={onClose}
           foot={<button className="btn ghost" onClick={onClose}>Close</button>}>
      {!p && !err && <div className="hint">Loading profile…</div>}
      {err && <div className="banner danger"><I.info size={14}/><span>Could not load profile: {err}</span></div>}
      {p && (
        <div className="col" style={{ gap: 12 }}>
          {p.about_me ? <div style={{ fontSize: 13 }}>{p.about_me}</div> : <div className="hint">No about-me set.</div>}
          {(p.hosted_services || []).length > 0 && (
            <div>
              <div className="hint" style={{ marginBottom: 4 }}>Services they host</div>
              <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                {(p.hosted_services || []).map((s, i) => (
                  <Pill key={i} tone="cyan">{s.name}{s.version ? ` ${s.version}` : ""} · {s.access || "free"}</Pill>
                ))}
              </div>
            </div>
          )}
          <div>
            <div className="hint" style={{ marginBottom: 4 }}>Global usage<Verified/></div>
            <div className="mono" style={{ fontSize: 11 }}>
              contributed {u.tasks_contributed || 0} task{(u.tasks_contributed || 0) === 1 ? "" : "s"} / {u.compute_secs_contributed || 0}s compute · consumed {u.tasks_consumed || 0} / {u.compute_secs_consumed || 0}s
              <br/>hosting {fmtBytes(u.storage_bytes_hosted)} for others · using {fmtBytes(u.storage_bytes_used)} · helped {u.peers_helped || 0} peer{(u.peers_helped || 0) === 1 ? "" : "s"}
            </div>
          </div>
          <div>
            <div className="hint" style={{ marginBottom: 4 }}>Between you two<Verified/></div>
            <div className="mono" style={{ fontSize: 11 }}>
              they gave you {ex.they_gave_compute_secs || 0}s compute · you gave {ex.you_gave_compute_secs || 0}s
              <br/>they host {fmtBytes(ex.they_hosted_bytes)} of yours · you host {fmtBytes(ex.you_hosted_bytes)} of theirs
              {rel.success_rate != null && <><br/>reliability on your tasks: {rel.success_rate}% ({rel.ok || 0} ok / {rel.failed || 0} failed)</>}
            </div>
          </div>
          {(p.groups_in_common || []).length > 0 && (
            <div className="hint">Groups in common: {(p.groups_in_common || []).map(g => g.name || g).join(", ")}</div>
          )}
        </div>
      )}
    </Modal>
  );
};

/* ── Pool subtab — receipt-verified per-member totals + this node's
 * time-bucketed history (Wave 43.D3 / 48 / 49). ── */
const PoolTab = ({ gid }) => {
  const [stats, setStats] = React.useState(null);
  const [range, setRange] = React.useState("7d");
  const [buckets, setBuckets] = React.useState(null);
  const base = `/local/groups/${encodeURIComponent(gid)}`;
  React.useEffect(() => {
    api.get(`${base}/pool_stats`).then(r => setStats(r.members || [])).catch(() => setStats([]));
  }, [base]);
  React.useEffect(() => {
    setBuckets(null);
    api.get(`${base}/pool_usage?range=${encodeURIComponent(range)}`).then(r => setBuckets(r.buckets || [])).catch(() => setBuckets([]));
  }, [base, range]);
  const exportPool = async (fmt) => {
    try {
      const res = await fetch(`${base}/pool_usage/export?format=${fmt}`, { headers: { "X-Local-Token": api.token } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `pool-${gid.slice(0, 8)}-${new Date().toISOString().slice(0, 10)}.${fmt}`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (_) {}
  };
  const rows = (stats || []).slice().sort((a, b) =>
    (b.tasks_contributed - a.tasks_contributed) || (b.tasks_consumed - a.tasks_consumed));
  const hist = (buckets || []).slice().sort((a, b) => (b.bucket_start || "").localeCompare(a.bucket_start || ""));
  return (
    <>
      <div className="card">
        <CardHead icon={<I.cpu size={14}/>} tone="cyan" title="Pool usage per member"
                  meta={<Verified/>}/>
        {stats === null && <div className="hint" style={{ padding: 14 }}>Loading…</div>}
        {stats !== null && (
          <table className="t">
            <thead><tr><th>Member</th><th style={{ textAlign: "right" }}>Contributed</th><th style={{ textAlign: "right" }}>Consumed</th></tr></thead>
            <tbody>
              {rows.map((m, i) => (
                <tr key={m.pubkey || i}>
                  <td style={{ fontSize: 13 }}>{m.display_name || short(m.pubkey)}</td>
                  <td className="mono" style={{ textAlign: "right", fontSize: 12 }}>{m.tasks_contributed || 0}</td>
                  <td className="mono" style={{ textAlign: "right", fontSize: 12 }}>{m.tasks_consumed || 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <div className="card">
        <div className="row" style={{ padding: "10px 14px", gap: 8, alignItems: "center", borderBottom: "1px solid var(--br)" }}>
          <div style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>Your usage history in this group</div>
          <div className="seg">
            {["24h", "7d", "30d"].map(r => <button key={r} className={range === r ? "on" : ""} onClick={() => setRange(r)}>{r}</button>)}
          </div>
          <DownloadMenu onPick={exportPool}/>
        </div>
        {buckets === null && <div className="hint" style={{ padding: 14 }}>Loading…</div>}
        {buckets !== null && hist.length === 0 && <div className="dim" style={{ padding: 14, fontSize: 12 }}>No activity in this range.</div>}
        {hist.length > 0 && (
          <table className="t">
            <thead><tr><th>Bucket</th><th>Start</th><th>Tasks ↗/↙</th><th>Compute s ↗/↙</th><th>Storage</th></tr></thead>
            <tbody>
              {hist.slice(0, 200).map((b, i) => (
                <tr key={i}>
                  <td style={{ fontSize: 12 }}>{b.bucket_kind || ""}</td>
                  <td className="mono dim" style={{ fontSize: 11 }}>{(b.bucket_start || "").slice(0, 16)}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{b.tasks_contributed || 0} / {b.tasks_consumed || 0}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{b.compute_secs_contributed || 0} / {b.compute_secs_consumed || 0}</td>
                  <td className="mono" style={{ fontSize: 12 }}>{fmtBytes(b.storage_bytes_used)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
};

const GroupsScreen = ({ badges = {}, onMessage, initialGid = null }) => {
  const [groups, setGroups] = React.useState([]);
  const [selId, setSelId] = React.useState(initialGid === "new" ? null : initialGid);
  const [detail, setDetail] = React.useState(null);
  const [creating, setCreating] = React.useState(initialGid === "new");
  const [msg, setMsg] = React.useState("");
  const [panel, setPanel] = React.useState(null);
  const [tab, setTab] = React.useState("members");
  const [invites, setInvites] = React.useState([]);
  const [trustedPeers, setTrustedPeers] = React.useState([]);
  // A group member may carry an empty node_id (e.g. the founder's own record),
  // so match a member to a trusted peer on any stable key: node id, network
  // address (peer_address ↔ resolved_ip), or display name.
  const isTrusted = (m) => trustedPeers.some(p =>
    (m.node_id && (p.peer_uuid === m.node_id || p.internal_ip === m.node_id)) ||
    (m.peer_address && (p.resolved_ip === m.peer_address || p.internal_ip === m.peer_address)) ||
    (m.display_name && p.display_name && p.display_name === m.display_name));
  const [viewProfile, setViewProfile] = React.useState(null); // {nodeId, name}
  const [pending, setPending] = React.useState([]);
  const [assigning, setAssigning] = React.useState(null);
  const avatarInput = React.useRef(null);

  const loadList = React.useCallback(async () => {
    const g = await api.get("/local/groups").catch(() => ({}));
    const list = Array.isArray(g) ? g : (g.groups || []);
    setGroups(list);
    const full = list.filter(x => (x.kind || "full") !== "chat");
    setSelId(prev => prev || (full[0] && full[0].id) || null);
  }, []);

  const loadDetail = React.useCallback(async (id) => {
    if (!id) { setDetail(null); return; }
    try {
      const d = await api.get(`/local/groups/${encodeURIComponent(id)}`);
      setDetail(d);
      const perms = d.my_permissions || [];
      if (perms.includes("group:invite")) {
        api.get(`/local/groups/${encodeURIComponent(id)}/secure_links`).then(r => setInvites(r.invites || [])).catch(() => setInvites([]));
      } else setInvites([]);
      if (perms.includes("group:approve")) {
        api.get(`/local/groups/${encodeURIComponent(id)}/pending_requests`).then(r => setPending(r.requests || [])).catch(() => setPending([]));
      } else setPending([]);
    } catch (_) { setDetail(null); }
  }, []);

  React.useEffect(() => { loadList(); }, [loadList]);
  React.useEffect(() => { setPanel(null); setAssigning(null); setTab("chat"); loadDetail(selId); }, [selId, loadDetail]);
  React.useEffect(() => {
    api.get("/local/peers").then(p =>
      setTrustedPeers((p.peers || []).filter(x => (x.status || "").startsWith("trusted")))).catch(() => {});
  }, [selId]);

  const flash = (t) => { setMsg(t); setTimeout(() => setMsg(""), 4000); };
  const act = async (label, fn) => {
    try { await fn(); flash(label + " ✓"); await loadDetail(selId); await loadList(); }
    catch (e) { flash(label + " failed: " + (e.detail || e.message || "")); }
  };

  const uploadAvatar = async (file) => {
    if (!file) return;
    try {
      const dataUrl = await fileToAvatar(file);
      await act("Group picture updated", () =>
        api.post(`/local/groups/${encodeURIComponent(selId)}/avatar`, { avatar: dataUrl }));
    } catch (e) { flash("Picture failed: " + (e.message || e)); }
  };

  const d = detail || {};
  const perms = d.my_permissions || [];
  const has = (p) => perms.indexOf(p) >= 0;
  const canPause = has("relay:host") || d.founder_pubkey === d.my_pubkey;
  const hasActiveRelay = (d.relays || []).some(r => r.status === "active" || r.state === "online");
  const myRole = (() => {
    const me = (d.members || []).find(m => m.pubkey === d.my_pubkey);
    return (me && me.roles && me.roles[0]) || (d.founder_pubkey === d.my_pubkey ? "founder" : "member");
  })();

  const saveAssign = () => act("Roles assigned", () =>
    api.post(`/local/groups/${encodeURIComponent(selId)}/members/${encodeURIComponent(assigning.pubkey)}/roles`,
             { roles: [...assigning.roles] })).then(() => setAssigning(null));

  const isFounder = d.founder_pubkey === d.my_pubkey;
  const founderM = (d.members || []).find(m => m.pubkey === d.founder_pubkey);
  const founderName = (founderM && founderM.display_name) || short(d.founder_pubkey);
  const connect = (m) => {
    const addr = (m.peer_address || "").trim();
    if (!addr) { flash("Connect failed: this member is only reachable via relay — no direct address to pair with"); return; }
    const fd = new FormData();
    fd.append("target_ip", addr);
    act("Pair request sent", () => api.post("/local/request_peer", fd));
  };

  const TABS = [
    ["chat", "Chat"],
    ["members", `Members (${(d.members || []).length})`],
    ["roles", `Roles (${(d.roles || []).length})`],
    ["relays", `Relays (${(d.relays || []).length})`],
    ["pool", "Pool"],
    ...(has("group:invite") ? [["invites", `Invites${invites.length ? ` (${invites.length})` : ""}`]] : []),
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Groups</div>
          <div className="page-sub">Durable communities bound to one or more relays. Chat lives in Messages.</div>
        </div>
      </div>

      {msg && <div className={"banner " + (msg.includes("failed") ? "danger" : "info")} style={{ marginBottom: 14 }}>
        <I.info size={14}/><span>{msg}</span>
      </div>}

      {creating && (
        <CreateGroupPanel onCancel={() => setCreating(false)}
                          onDone={(gid) => { setCreating(false); flash("Group created ✓"); loadList().then(() => gid && setSelId(gid)); }}/>
      )}
      {panel && panel.type === "join" && (
        <JoinPanel onCancel={() => setPanel(null)}
                   onDone={(gid) => { setPanel(null); loadList().then(() => gid && setSelId(gid)); }}/>
      )}
      {panel && panel.type === "role" && (
        <RolePanel gid={selId} role={panel.role}
                   onCancel={() => setPanel(null)}
                   onDone={() => { setPanel(null); flash("Role saved ✓"); loadDetail(selId); }}/>
      )}
      {panel && panel.type === "mint" && (
        <MintPanel gid={selId} hasRelay={hasActiveRelay}
                   onCancel={() => setPanel(null)}
                   onDone={() => { setPanel(null); loadDetail(selId); }}/>
      )}
      {panel && panel.type === "friends" && (
        <InviteFriendsPanel gid={selId}
                            onCancel={() => setPanel(null)}
                            onDone={() => { setPanel(null); loadDetail(selId); }}/>
      )}
      {viewProfile && (
        <PeerProfileModal nodeId={viewProfile.nodeId} name={viewProfile.name}
                          onClose={() => setViewProfile(null)}/>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "84px 1fr", gap: 14, alignItems: "start", marginBottom: 24 }}>
        {/* ── avatar rail ── */}
        <div className="card" style={{ padding: "10px 0", display: "flex", flexDirection: "column", alignItems: "center", gap: 10, position: "sticky", top: 14 }}>
          {groups.filter(g => (g.kind || "full") !== "chat").map(g => (
            <div key={g.id} title={g.name || g.id}
                 onClick={() => setSelId(g.id)}
                 style={{
                   cursor: "pointer", position: "relative", borderRadius: "50%",
                   outline: selId === g.id ? "2px solid var(--accent)" : "2px solid transparent",
                   outlineOffset: 3, transition: "outline-color .15s",
                 }}>
              <GAvatar group={g} size={44}/>
              {(g.relay_active_count || 0) > 0 && (
                <span style={{ position: "absolute", right: -1, bottom: -1, width: 11, height: 11, borderRadius: "50%", background: "var(--emerald, #34d399)", border: "2px solid var(--bg-card)" }}/>
              )}
              {badges[g.id] && badges[g.id].n > 0 && (
                <span style={{
                  position: "absolute", top: -6, right: -8, minWidth: 18, height: 18, padding: "0 4px",
                  borderRadius: 9, display: "grid", placeItems: "center",
                  fontSize: 9.5, fontWeight: 700, fontFamily: "var(--f-mono)",
                  background: badges[g.id].mention ? "rgba(245,158,11,0.95)" : "var(--accent)",
                  color: badges[g.id].mention ? "#1a1206" : "#fff",
                  border: "2px solid var(--bg-card)",
                }}>{fmtBadge(badges[g.id].n)}</span>
              )}
            </div>
          ))}
          <div style={{ width: 36, borderTop: "1px solid var(--br)", margin: "2px 0" }}/>
          <button className="btn ghost sm" title="Create a group" style={{ width: 44, height: 44, borderRadius: "50%", padding: 0, justifyContent: "center" }}
                  onClick={() => { setCreating(!creating); setPanel(null); }}>
            <I.plus size={18}/>
          </button>
          <button className="btn ghost sm" title="Join with an invite link" style={{ width: 44, height: 44, borderRadius: "50%", padding: 0, justifyContent: "center" }}
                  onClick={() => { setPanel({ type: "join" }); setCreating(false); }}>
            <I.link size={16}/>
          </button>
        </div>

        {/* ── main column ── */}
        <div className="col" style={{ gap: 14, minWidth: 0 }}>
          {!detail && (
            <div className="card pad-lg">
              {groups.length === 0 ? (
                <>
                  <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 6 }}>No groups yet</div>
                  <div className="dim" style={{ fontSize: 12.5, marginBottom: 12 }}>
                    A group is a private mesh: shared compute pool, encrypted chat, relays, and verified usage accounting.
                  </div>
                  <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                    <button className="btn accent sm" onClick={() => { setCreating(true); setPanel(null); }}><I.plus size={13}/> Create a group</button>
                    <button className="btn ghost sm" onClick={() => { setPanel({ type: "join" }); setCreating(false); }}><I.link size={13}/> Join with an invite link</button>
                  </div>
                </>
              ) : <span className="dim">Select a group from the rail.</span>}
            </div>
          )}

          {detail && (
            <>
              {/* header */}
              <div className="card pad-lg">
                <div className="row" style={{ gap: 14, alignItems: "center", flexWrap: "wrap" }}>
                  <div style={{ position: "relative", cursor: has("role:assign") ? "pointer" : "default" }}
                       title={has("role:assign") ? "Change group picture" : ""}
                       onClick={() => has("role:assign") && avatarInput.current && avatarInput.current.click()}>
                    <GAvatar group={d} size={56}/>
                    {has("role:assign") && (
                      <span style={{ position: "absolute", right: -4, bottom: -4, width: 20, height: 20, borderRadius: "50%", background: "var(--bg-card-2, #1b1d22)", border: "1px solid var(--br)", display: "grid", placeItems: "center" }}>
                        <I.upload size={11}/>
                      </span>
                    )}
                  </div>
                  <input ref={avatarInput} type="file" accept="image/*" style={{ display: "none" }}
                         onChange={e => { uploadAvatar(e.target.files && e.target.files[0]); e.target.value = ""; }}/>
                  <div style={{ flex: 1, minWidth: 180 }}>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      <span style={{ fontWeight: 700, fontSize: 16 }}>{d.name}</span>
                      <Pill tone={roleTone(myRole)}>{myRole}</Pill>
                      {d.privacy_mode && <Pill tone="ghost">{d.privacy_mode}</Pill>}
                      {d.paused ? <Pill tone="amber" dot>paused</Pill> : null}
                    </div>
                    <div className="mono dim" style={{ fontSize: 11, marginTop: 3 }}>
                      founder {founderName} · {(d.members || []).length} member{(d.members || []).length === 1 ? "" : "s"} · {(d.relays || []).length} relay{(d.relays || []).length === 1 ? "" : "s"}
                    </div>
                  </div>
                  <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    {has("group:invite") && <button className="btn ghost sm" onClick={() => setPanel({ type: "friends" })}><I.users size={13}/> Invite friends</button>}
                    {has("group:invite") && <button className="btn accent sm" onClick={() => setPanel({ type: "mint" })}><I.share size={13}/> Mint invite</button>}
                    {canPause && (d.paused
                      ? <button className="btn ghost sm" onClick={() => act("Resume", () => api.post(`/local/groups/${selId}/resume`))}><I.play size={13}/> Resume</button>
                      : <button className="btn ghost sm" onClick={() => act("Pause", () => api.post(`/local/groups/${selId}/pause`))}><I.pause size={13}/> Pause</button>)}
                    {has("role:assign") && (
                      <button className="btn ghost sm" title="Switch between open (anyone with a link can request) and private"
                              onClick={() => act("Privacy changed", () => api.post(`/local/groups/${selId}/privacy`, { privacy_mode: d.privacy_mode === "private" ? "open" : "private" }))}>
                        <I.eye size={13}/> Make {d.privacy_mode === "private" ? "open" : "private"}
                      </button>
                    )}
                    {!isFounder && (
                      <Danger label="Leave group" confirmLabel="Leave & lose access?"
                              onFire={() => act("Left group", async () => { await api.post(`/local/groups/${selId}/leave`); setSelId(null); })}/>
                    )}
                    {isFounder && (
                      <Danger label="Delete group" confirmLabel="Delete for everyone?"
                              onFire={() => act("Group deleted", async () => { await api.del(`/local/groups/${selId}`); setSelId(null); })}/>
                    )}
                  </div>
                </div>
                <div className="seg" style={{ marginTop: 14 }}>
                  {TABS.map(([id, label]) => (
                    <button key={id} className={tab === id ? "on" : ""} onClick={() => setTab(id)}>{label}</button>
                  ))}
                </div>
              </div>

              {/* pending approvals — always surfaced when present */}
              {has("group:approve") && pending.length > 0 && (
                <div className="card">
                  <CardHead icon={<I.bell size={14}/>} tone="amber" title="Pending join requests" meta={<span>{pending.length}</span>}/>
                  <div className="col" style={{ gap: 8, padding: 8 }}>
                    {pending.map((r, i) => (
                      <div key={r.request_id || i} className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                        <Avatar name={r.display_name || r.requester_pubkey || "?"} seed={r.requester_pubkey} color="#fbbf24" size={26}/>
                        <div style={{ flex: 1, minWidth: 160 }}>
                          <div style={{ fontSize: 13, fontWeight: 600 }}>{r.display_name || short(r.requester_pubkey || "")}</div>
                          <div className="hint" style={{ fontSize: 11 }}>{r.message || "no message"} · {(r.created_at || "").slice(0, 19)}</div>
                        </div>
                        {r.status && r.status !== "pending"
                          ? <Pill tone="ghost">{r.status}</Pill>
                          : <div className="row" style={{ gap: 6 }}>
                              <button className="btn accent sm" onClick={() => act("Approved", () => api.post(`/local/groups/${selId}/pending_requests/${encodeURIComponent(r.request_id)}/approve`))}>Approve</button>
                              <Danger label="Reject" onFire={() => act("Rejected", () => api.post(`/local/groups/${selId}/pending_requests/${encodeURIComponent(r.request_id)}/reject`))}/>
                            </div>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* chat */}
              {tab === "chat" && <GroupChat gid={selId} detail={d}/>}

              {/* members */}
              {tab === "members" && (
                <div className="card">
                  <table className="t">
                    <tbody>
                      {(d.members || []).map((m, i) => {
                        const isMe = m.pubkey === d.my_pubkey;
                        const isFounderRow = m.pubkey === d.founder_pubkey;
                        return (
                        <React.Fragment key={i}>
                          <tr>
                            <td>
                              <div className="row">
                                <Avatar name={m.display_name || m.pubkey} seed={m.pubkey} color="#60a5fa" size={26}/>
                                <div>
                                  <div className="name">{m.display_name || short(m.pubkey)}{isMe ? " (you)" : ""}{m.muted ? <Pill tone="rose"> muted</Pill> : null}</div>
                                  <div className="mono dim" style={{ fontSize: 10 }}>{short(m.pubkey)}</div>
                                </div>
                              </div>
                            </td>
                            <td><Presence lastSeen={m.last_seen_at} isMe={isMe}/></td>
                            <td>
                              <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                                {(m.roles || []).map((r, j) => <Pill key={j} tone={roleTone(r)}>{r}</Pill>)}
                              </div>
                            </td>
                            <td className="mono dim" style={{ fontSize: 11 }} title={(m.joined_at || "").slice(0, 19)}>
                              {m.joined_at ? `${Math.max(0, Math.floor((Date.now() - Date.parse(m.joined_at)) / 86400000))}d` : "—"}
                            </td>
                            <td style={{ textAlign: "right" }}>
                              <div className="row" style={{ gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                                {has("role:assign") && !isFounderRow && (
                                  <button className="btn ghost sm" onClick={() => setAssigning(assigning && assigning.pubkey === m.pubkey ? null : { pubkey: m.pubkey, roles: new Set(m.roles || []) })}>
                                    Roles…
                                  </button>
                                )}
                                {has("member:mute") && !isFounderRow && !isMe && (
                                  <button className="btn ghost sm" onClick={() => act(m.muted ? "Unmuted" : "Muted", () => api.post(`/local/groups/${selId}/members/${encodeURIComponent(m.pubkey)}/mute`, { muted: !m.muted }))}>
                                    {m.muted ? "Unmute" : "Mute"}
                                  </button>
                                )}
                                {!isMe && (m.peer_address || m.node_id) && (
                                  isTrusted(m)
                                    ? <button className="btn ghost sm" disabled title="Already a trusted peer">Connected</button>
                                    : <button className="btn ghost sm" onClick={() => connect(m)}>Connect</button>
                                )}
                                {!isMe && m.node_id && onMessage && (
                                  <button className="btn ghost sm" onClick={() => onMessage(m.node_id)}><I.send size={12}/> Message</button>
                                )}
                                {!isMe && m.node_id && (
                                  <button className="btn ghost sm" onClick={() => setViewProfile({ nodeId: m.node_id, name: m.display_name || short(m.pubkey) })}>Profile</button>
                                )}
                                {has("member:kick") && !isMe && !isFounderRow && (
                                  <Danger label="Kick" confirmLabel="Rotate key & kick?" onFire={() => act("Member removed", () => api.post(`/local/groups/${selId}/members/${encodeURIComponent(m.pubkey)}/kick`))}/>
                                )}
                              </div>
                            </td>
                          </tr>
                          {assigning && assigning.pubkey === m.pubkey && (
                            <tr>
                              <td colSpan={5} style={{ padding: "8px 14px" }}>
                                <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                                  <span className="hint">Roles for {m.display_name || short(m.pubkey)}:</span>
                                  {(d.roles || []).map(r => (
                                    <div key={r.name} className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }}
                                         onClick={() => {
                                           const next = new Set(assigning.roles);
                                           next.has(r.name) ? next.delete(r.name) : next.add(r.name);
                                           setAssigning({ ...assigning, roles: next });
                                         }}>
                                      <Chk on={assigning.roles.has(r.name)}/>
                                      <span className="mono" style={{ fontSize: 12 }}>{r.name}</span>
                                    </div>
                                  ))}
                                  <button className="btn accent sm" onClick={saveAssign}><I.check size={13}/> Save</button>
                                  <button className="btn ghost sm" onClick={() => setAssigning(null)}>Cancel</button>
                                </div>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {/* roles */}
              {tab === "roles" && (
                <div className="card">
                  {has("role:assign") && (
                    <div className="row" style={{ padding: "10px 14px 0", justifyContent: "flex-end" }}>
                      <button className="btn ghost sm" onClick={() => setPanel({ type: "role" })}><I.plus size={13}/> New role</button>
                    </div>
                  )}
                  <div className="col" style={{ gap: 8, padding: 12 }}>
                    {(d.roles || []).map((r, i) => (
                      <div key={i} className="row" style={{ gap: 10, alignItems: "flex-start" }}>
                        <Pill tone={roleTone(r.name)}>{r.name}</Pill>
                        <div className="row" style={{ gap: 5, flexWrap: "wrap", flex: 1 }}>
                          {(r.permissions || []).map((p, j) => <span key={j} className="pill ghost" style={{ fontSize: 10 }}>{p}</span>)}
                        </div>
                        {has("role:assign") && !["founder", "admin", "member"].includes(r.name) && (
                          <div className="row" style={{ gap: 6 }}>
                            <button className="btn ghost sm" onClick={() => setPanel({ type: "role", role: r })}>Edit</button>
                            <Danger label="Delete" onFire={() => act("Role deleted", () => api.del(`/local/groups/${selId}/roles/${encodeURIComponent(r.name)}`))}/>
                          </div>
                        )}
                        {has("role:assign") && ["admin", "member"].includes(r.name) && (
                          <button className="btn ghost sm" onClick={() => setPanel({ type: "role", role: r })}>Edit</button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* relays */}
              {tab === "relays" && <RelaysTab gid={selId} d={d} has={has} act={act} flash={flash}/>}

              {/* pool */}
              {tab === "pool" && <PoolTab gid={selId}/>}

              {/* invites */}
              {tab === "invites" && (
                <div className="card">
                  {invites.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No invites issued — use <strong>Mint invite</strong> above.</div>}
                  {invites.length > 0 && (
                    <table className="t">
                      <thead><tr><th>Issued</th><th>Expires</th><th>Status</th><th>Used</th></tr></thead>
                      <tbody>
                        {invites.map((inv, i) => (
                          <tr key={inv.invite_id || i}>
                            <td className="mono" style={{ fontSize: 11 }}>{(inv.issued_at || inv.created_at || "").slice(0, 19)}</td>
                            <td className="mono" style={{ fontSize: 11 }}>{(inv.expires_at || "").slice(0, 19)}</td>
                            <td><Pill tone={inv.status === "active" ? "emerald" : "ghost"}>{inv.status || "?"}</Pill></td>
                            <td className="mono" style={{ fontSize: 11 }}>{inv.used_count != null ? `${inv.used_count}/${inv.max_uses}` : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  <div className="hint" style={{ padding: "6px 14px 10px" }}>Links are shown only once at mint time — mint a fresh one if it's lost.</div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
};

export { GroupsScreen, PeerProfileModal };
